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

### 步骤 1 · 确定任务
任务 = 用户的自然语言需求（`/agent-orchestrator` 后面的文字，或当前对话里的需求）。
没有就问一句："要让编排器完成什么需求？"

### 步骤 2 · 确定目标仓库（→ `--repo`）
从上下文推断（正在讨论的项目 / 当前目录）。不明确就问是哪个仓库，并确认绝对路径。

### 步骤 3 · 确立验收门（关键——绝不让用户去写 `--gate`）
编排器必须有客观判过否的门。**由你来确立，不要甩给用户：**
- 先探测现有的：`pytest` / `package.json` 的 scripts / `Makefile` / `ruff` / `mypy` / `tsc` /
  构建命令等。有就直接用它们当门（一个或多个 `--gate 名字=命令`）。
- **没有现成的**（如纯静态站点、空项目）→ **由你按任务的验收点起草一个小验收脚本**
  （如 `check.py`：检查产物存在、关键内容/结构满足、退出码 0/1、打印清晰原因），写进目标仓库，
  一句话告诉用户它查什么，然后拿它当门。这样用户全程不碰 `--gate`。
- 多个维度（如"既要过结构检查、又不能破坏既有页面"）→ 配多个 `--gate`。
- 写门脚本时：判定要客观、输出要能定位问题；编排器已统一处理子进程编码，无需自己折腾 UTF-8。

### 步骤 4 · 选合理默认（用户没特别要求时）
- `--max-rounds 2~3`；`--budget-usd` 给个安全上限（如 4）；`--budget-seconds` 视情况。
- 想让 codex 快一点：`--codex-config model_reasoning_effort=medium`（默认 xhigh 偏慢）。
- 任务大、可拆成有依赖的子任务时才加 `--decompose`；小任务别拆。
- `--model` 是给 **claude** 的（不是 codex！），一般不用动；codex 的模型用 `--codex-model`。

### 步骤 5 · 一句话确认
向用户复述将要执行的：仓库、验收门（及其含义）、轮数、预算、是否拆子任务。等一个简短确认。

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

## 参数参考（仅当需要覆盖默认时）

| 参数 | 作用 |
|------|------|
| `--repo` | 目标仓库路径 |
| `--gate 名字=命令` | 验收门链一环，可重复；任一不过即未达标 |
| `--max-rounds` | 每个子任务最多几轮（默认 3） |
| `--decompose` | 拆成子任务 DAG，按拓扑序逐个做（失败只影响下游） |
| `--budget-usd` / `--budget-seconds` | 成本/耗时上限，超限提前停止 |
| `--rollback-on-fail` | 失败时回滚工作区到最近一次门全过的快照 |
| `--codex-model` / `--codex-config k=v` | 控制 **codex** 的模型 / 配置（如降推理强度提速） |
| `--model` | 控制 **claude** 的模型（不是 codex） |
| `--dry-run` | 不调真模型走通流程，用于自测 |

## 安全

`codex exec --dangerously-bypass-approvals-and-sandbox` 对工作区有完整读写执行权限。仅在
**受控/隔离仓库**运行，提交前人工 review 全部改动；不要指向不信任或不愿被改动的仓库。

## 架构与迭代

底层分层的 `orchestrator/` 包（cli → engine → planner → agents/gates/gitrepo → prompts/graph →
process/config/budget/artifacts/util），依赖单向无环，详见 `references/architecture.md`。
**迭代只改仓库的 `orchestrator/` 包**，本 skill 入口无需改动；新增对外参数时同步更新本文件的参数参考。
