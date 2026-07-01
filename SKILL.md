---
name: agent-orchestrator
description: 【仅手动调用——不要自动触发】运行「Claude 规划 + Codex 实现」自动编排器（orchestrator-worker）来完成或迭代一个编码需求：Claude 拆解需求与验收标准、Codex 改代码、自动跑验收门链（测试/lint/类型）多轮迭代直到通过，可选 --decompose 拆子任务 DAG。仅当用户显式输入 /agent-orchestrator、或明确点名要"跑这个编排器/这个 skill"时才使用；不要根据需求内容自动推断触发——即使用户描述了一个适合本框架的编码需求，只要没有显式点名，也不要使用本 skill。
---

# agent-orchestrator

把一个编码需求交给「Claude（大脑，规划+评审，只读）+ Codex（双手，写代码）」自动协作完成，
多轮迭代直到客观验收门通过且评审通过。底层是仓库的 `orchestrator/` 包，入口脚本 `scripts/run.py`。

## 这是对话式入口——你（助手）负责把意图翻译成调用，用户不写任何命令行

核心定位：**用户只用自然语言说需求，所有 CLI 参数由你推断和装配，用户永远不需要写
`--repo / --gate / --max-rounds` 之类。** 被调用后按下面的流程走，不要直接把命令甩给用户。

### 步骤 1 · 极简三问（开场只问这三件，其余你来推断）
**只问用户才知道的事，别让用户碰任何参数。** 启动后问这三问（其中①常常已经有了）：

1. **需求**：`/agent-orchestrator` 后面跟的文字，或当前对话里的需求即可直接用；没有就问一句
   "要让编排器完成什么需求？"
2. **验收点 / 禁区**："怎样算做完？有没有不能碰的文件/模块？"——⭐ 比预算更关键：它直接决定
   你怎么起草验收门，也圈定 Codex 的改动边界（写进给 Codex 的指令约束，防止重构外溢）。
3. **预算档位**：给档，不让用户报数字——

   | 档 | 适用 | 你装配成 |
   |---|---|---|
   | 小改 / 极简 | 单文件/局部小改、需求已很清晰、且有可信验收门 | `--no-plan --no-review --max-rounds 2 --codex-config model_reasoning_effort=medium` |
   | 省着跑 | 小改动 / 验证想法 | `--budget-usd 1.5 --max-rounds 2 --codex-config model_reasoning_effort=medium` |
   | 标准（默认） | 一般功能 / 重构 | `--budget-usd 4 --max-rounds 3` |
   | 充足 | 大任务 / 要拆子任务 | `--budget-usd 10 --max-rounds 4`（通常配 `--decompose`） |

   参考：wildlife 6 子任务重构实跑 ≈ $2.76；单文件小游戏一轮 ≈ $0.37。`--budget-seconds` 视情况另给。
   **编排重量随任务伸缩**：编排的固定开销是 Claude 规划 + 评审，小任务里占大头。「小改/极简」档用
   `--no-plan`（跳过规划，需求原文当 brief、门当验收标准）+ `--no-review`（门全过即完成，Claude 完全
   退出循环，纯 Codex+门），把小改成本压到最低——**前提是验收门可信**（门兜不住的运行时正确性就没人
   兜了，所以仅在门足够客观时用）。需求模糊或要质量把关 → 别用 `--no-plan`/`--no-review`，走标准档。
   注：**默认已是「门过才评审」**——门没过的轮次自动跳过评审（只在门全绿时评审做最终把关），无需配置。

### 步骤 2 · 推断目标仓库与 git 状态（→ `--repo`，不要问，先猜）
从上下文推断（正在讨论的项目 / 当前目录），不明确才问，并确认绝对路径。
顺手看一眼工作区是否干净、当前在哪个分支——改动不自动提交，但脏工作区会混进 diff 影响评审。

### 步骤 3 · 确立验收门（关键——绝不让用户去写 `--gate`）
编排器必须有客观判过否的门。**由你来确立，不要甩给用户：**
- 先探测现有的：`pytest` / `package.json` 的 scripts / `Makefile` / `ruff` / `mypy` / `tsc` /
  构建命令等。有就直接用它们当门（一个或多个 `--gate 名字=命令`）。
- **没有现成的**（如纯静态站点、空项目）→ **由你按步骤 1 问到的验收点起草一个小验收脚本**
  （如 `check.py`：检查产物存在、关键内容/结构满足、退出码 0/1、打印清晰原因），写进目标仓库，
  一句话告诉用户它查什么，然后拿它当门。这样用户全程不碰 `--gate`。
- 多个维度（如"既要过结构检查、又不能破坏既有页面"）→ 配多个 `--gate`。
- **运行时/交互正确性**：门只保证能 build，运行时对错靠评审兜底。两种补强：
  - 静态站资源 404 这类（构建过、页面却断链）→ 直接用仓库自带的零依赖冒烟门：
    `--gate smoke="python <本skill>/scripts/smoke_static.py <构建产物目录>"`（校验所有
    HTML/CSS 的本地资源引用是否真实存在，正是 wildlife 报告里占位图 404 那类 bug）。
  - 真·DOM/交互/视觉行为 → 需要时再补 jsdom/Playwright 门，并提醒用户这靠 Claude 评审兜底。
- 写门脚本时：判定要客观、输出要能定位问题；编排器已统一处理子进程编码，无需自己折腾 UTF-8。

### 步骤 4 · 推断其余默认（用户没特别要求时，确认时可一句带过）
- **是否拆子任务（`--decompose`）**：需求大、可拆成有依赖的子任务才加；小任务别拆。
  注：`--decompose` 下**整体验收门只在「汇点子任务」（收尾集成节点）上跑**，中间子任务改由
  它自带的廉价小门或 Claude 评审对照各自验收标准把关——所以你照常配一个「整体验收门」即可，
  不必为每个子任务手写门；规划器会自动把最后一个子任务定为集成收尾、整体门落在它身上。
- **取向**：速度/质量已由步骤 1 的预算档给了 `model_reasoning_effort` 与 `--max-rounds`；
  用户特别要快或要稳再单独调。
- **失败处置**：默认保留现场（每轮留可恢复快照）。两个可选开关，按需抛给用户——
  `--rollback-on-fail`（失败时回滚到最近一次门全过的快照）；
  `--continue-on-fail`（某子任务失败时，其下游不整支跳过，仅告警续跑——适合与失败者弱耦合的
  收尾/巡检类子任务，避免一个卡点拖垮全局）。
- `--model` 是给 **claude** 的（不是 codex！），一般不用动；codex 的模型用 `--codex-model`。

### 步骤 5 · 一句话确认
向用户复述将要执行的：仓库、验收门（及其含义）、轮数、预算档、是否拆子任务、失败处置。
等一个简短确认。

### 步骤 6 · 后台执行 + 盯进度 + 回报（默认方式，用户不碰终端）
1. 装配完整命令（见下"内部调用形态"）。`run.py` 会自动从注册表刷新 PATH 并校验 claude/codex，
   所以即使本会话 shell 的 PATH 是旧的也能找到工具；缺工具会明确报错。
2. **用后台方式启动**（shell 工具的 `run_in_background`）——这是个长跑任务（每轮真调 codex 写代码
   + claude 评审，多轮多子任务可能十几分钟），不要前台同步等。
3. **盯 `<repo>/runs/<时间戳>/` 的产物判断进度**：`task.txt` → `plan.json` →
   `<id>_round<N>_instruction.txt` → `<id>_round<N>_codex_stdout.txt` / `.diff` /
   `_gates.json` → `_review_*`。可隔一会儿读一次最新产物给用户同步进度。
4. **跑完回报**：哪些门过了、改了哪些文件（`git diff --stat`）、成功/失败结论。失败就说清原因
   （门没过 / 评审意见 / 卡死超时）并给下一步建议。**全程不要让用户去终端跑命令。**
   - 顺带回报**用量 / 性能**：读 `runs/<时间戳>/metrics.json`（含 summary：总成本 / token /
     耗时 / 调用次数）给用户一句话总结，并指向自包含图表报告 `runs/<时间戳>/report.html`
     （可离线打开 / 分享）。成本仅 claude 上报，codex token 为 best-effort 解析。

> 若用户明确说"把命令给我，我自己终端跑"，再退化为：装配好完整命令，原样给用户粘贴。

## 内部调用形态（供你装配，用户不可见）

```
python "<本skill>/scripts/run.py" "<任务原文>" \
    --repo "<目标仓库>" \
    --gate <名字>=<命令> [--gate ...] \
    [--max-rounds N] [--budget-usd X] [--codex-config model_reasoning_effort=medium] [--decompose]
```

- 参数与 `python -m orchestrator` 一致；完整列表 `python scripts/run.py --help`，细节见仓库 `README.md`。
- 成功后改动**留在工作区**（不自动 commit）；git 仓库内每轮有可恢复标签 `orch/<run-id>/...`。

## 前置条件

Python ≥ 3.10；`claude` 与 `codex` 已安装登录；目标最好是 git 仓库（否则无每轮快照/回滚）。
没装 codex 或只想验证装配时，用 `--dry-run`（注入假 agent，不调真模型）。

**托管子会话下的鉴权（重要）**：当本 skill 在 Claude Code 托管子会话里运行时，宿主登录态是运行时
注入、不落地为 CLI 可读凭据，子进程 `claude -p` 会报 `Not logged in`。入口已会**自动**处理——
据 `--auth-channel` 选定一条独立鉴权通道，注入其凭据并剥离宿主会话变量。支持两条通道，凭据均来自
环境变量或一次性配置文件 `~/.claude_codex_orchestrator.env`：

| 通道 | 凭据（配置文件键） | 计费 | 如何获得 |
|------|------|------|------|
| `subscription` | `CLAUDE_CODE_OAUTH_TOKEN=...` | 吃**订阅额度** | 普通终端跑 `claude setup-token`，复制其输出 |
| `api` | `ANTHROPIC_API_KEY=sk-ant-...` | 按 **API** 计费 | Anthropic 控制台签发 |

`--auth-channel` 取值 `subscription` / `api` / `auto`（默认）。`auto` = 按 `CCO_DEFAULT_CHANNEL` →
唯一可用 → 二者皆有时**优先订阅**。显式指定的通道缺凭据时会 fail-fast 报清楚（不会静默漏到宿主网关）。

**你（助手）该怎么驱动它（贴合 skill 直觉：默认订阅，不每次打扰）：**
- **判断是否已配置：先跑 `python <本skill>/scripts/run.py --check-auth`**（脱敏、不真跑、不需 task），
  它复用真实解析逻辑直接告诉你"会用哪条通道、凭据从哪来、有没有配"。**别再手搓 grep 配置文件或看进程
  环境变量来判断**——凭据是放配置文件里、由入口运行时直接读，**本就不进 shell 环境**，看环境变量为空会
  误判成"没配置"（曾因此白让用户重跑 `setup-token`）。
- **首次/未配置**（`--check-auth` 报未配置时）：引导用户一次性配置。问"用订阅额度还是 API key（可都配）"；
  选订阅 → 让其在普通终端跑 `claude setup-token` 并回贴 token，你写入配置文件
  `CLAUDE_CODE_OAUTH_TOKEN=...`；选 API → 写 `ANTHROPIC_API_KEY=...`。可顺手写 `CCO_DEFAULT_CHANNEL=...`
  定默认，并提示锁文件权限（Windows 用 `icacls`，Mac/Linux 用 `chmod 600`）。**订阅通道绕不开
  `setup-token` 这一步**（宿主订阅态不落地）。
- **两条都配好后**：平时静默走默认（不每次问）。**只在"该选"时主动浮出选项**——大任务 / `--decompose`
  多轮时提示"可能撞订阅限额，建议本次 `--auth-channel api`"；小改默认订阅即可。用户一句"这次用 key /
  用订阅"就装配对应 `--auth-channel` 覆盖单次。
- **只配了一条**：直接用，不问。

## 参数参考（仅当需要覆盖默认时）

| 参数 | 作用 |
|------|------|
| `--repo` | 目标仓库路径 |
| `--gate 名字=命令` | 验收门链一环，可重复；任一不过即未达标 |
| `--max-rounds` | 每个子任务最多几轮（默认 3） |
| `--decompose` | 拆成子任务 DAG，按拓扑序逐个做（失败只影响下游） |
| `--no-plan` | 跳过 Claude 规划：需求原文当 brief、验收门当验收标准（小/清晰任务省一次规划调用；与 `--decompose` 互斥） |
| `--no-review` | 跳过 Claude 评审：门全过即视为完成（Codex+门 纯净模式）。注：默认已是「门过才评审」，本项更进一步连最终评审也省 |
| `--budget-usd` / `--budget-seconds` | 成本/耗时上限，超限提前停止 |
| `--rollback-on-fail` | 失败时回滚工作区到最近一次门全过的快照 |
| `--continue-on-fail` | 子任务失败时仍尝试其下游（仅告警，不整支跳过） |
| `--codex-model` / `--codex-config k=v` | 控制 **codex** 的模型 / 配置（如降推理强度提速） |
| `--model` | 控制 **claude** 的模型（不是 codex） |
| `--auth-channel {auto,subscription,api}` | 托管子会话下 claude 的鉴权通道：订阅额度 / API 计费 / 自动（默认）。详见「托管子会话下的鉴权」 |
| `--check-auth` | 鉴权预检：脱敏报告会用哪条通道、凭据从哪来、有没有配；不真跑、不需 task。启动前先跑它确认，别手搓 grep/看环境变量 |
| `--dry-run` | 不调真模型走通流程，用于自测 |

## 安全

`codex exec --dangerously-bypass-approvals-and-sandbox` 对工作区有完整读写执行权限。仅在
**受控/隔离仓库**运行，提交前人工 review 全部改动；不要指向不信任或不愿被改动的仓库。

## 架构与迭代

底层分层的 `orchestrator/` 包（cli → engine → planner → agents/gates/gitrepo → prompts/graph →
process/config/budget/artifacts/util），依赖单向无环，详见 `README.md` 的「项目结构」。

**部署与分发（自包含，可给任意用户）**：本 skill 自带 `orchestrator/` 包，run.py 靠相对布局自动
发现、**无机器特定写死路径**——把整个 skill 目录拷给别人即开箱即用。
- 部署/更新：在仓库根跑 `python scripts/deploy.py`（默认装到 `~/.claude/skills/agent-orchestrator`，
  也可 `python scripts/deploy.py <目标目录>`），它会带上 orchestrator 包并自检自包含。
- **开发迭代**：改完仓库的 `orchestrator/` 后重新 `deploy.py` 即可；想「改完即生效不必重部署」，
  设环境变量 `AGENT_ORCHESTRATOR_HOME` 指向仓库根（run.py 会优先用它）。
- 新增对外参数时同步更新本文件的参数参考。
