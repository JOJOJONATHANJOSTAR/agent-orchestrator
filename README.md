# Claude + Codex 编排器

**中文** · [English](README.en.md)

最小可用的 **「Claude 规划 + Codex 实现」** 编排器（orchestrator-worker 模式）。
用一个脚本把两个命令行 AI agent 串起来自动协作改代码：Claude 当大脑（只读、规划+评审），
Codex 当双手（写代码），脚本在中间传结构化消息、跑验收门、控制循环、防死循环。

```
需求 ──▶ Claude 规划 ──▶ ┌───────────── 循环（≤ max-rounds 轮）─────────────┐
                         │ Codex 改代码 ──▶ 验收门(测试/lint) ──▶ Claude 评审 │
                         └──── 通过且评审 pass ? ──是──▶ 完成 / 否──▶ 下一轮 ──┘
```

## 角色分工

| 角色 | 职责 | 权限 |
|------|------|------|
| **Claude**（大脑） | 拆需求 → 出实现说明 + 验收标准；评审每轮 diff | 只读 `Read,Grep,Glob`，不动代码 |
| **Codex**（双手） | 按说明改代码 | `--dangerously-bypass-approvals-and-sandbox` |
| **编排器**（本包） | 传结构化消息、跑验收门、控制轮数、预算、快照 | — |

## 项目结构

代码按职责分层拆成 `orchestrator/` 包，依赖方向自上而下、无环；外部 agent 与验收门都抽象成
接口（`LLM` / `Coder` / `Gates`），`--dry-run` 用 `fakes` 注入替身，无任何可变全局：

```
claude_codex_orchestrator.py   # 向后兼容入口（= python -m orchestrator）
orchestrator/
├── cli.py        装配层：解析参数、依赖注入、跑总流程
├── engine.py     编排层：SubtaskRunner（单子任务多轮循环）+ DagEngine（DAG 驱动）
├── planner.py    领域层：Planner（规划/拆解）、Reviewer（评审）
├── agents.py     适配层：LLM/Coder 接口 + ClaudeClient/CodexClient/JsonAgent
├── gates.py      适配层：GateRunner（验收门链）+ 汇总/详情格式化
├── gitrepo.py    适配层：GitRepo（git 快照 / 回滚）
├── fakes.py      适配层替身：dry-run 用，实现同样的接口
├── prompts.py    纯逻辑：系统提示词、评审清单渲染、失败模式分流
├── graph.py      纯逻辑：DAG 拓扑排序、上下文拼装
├── process.py    基础设施：统一子进程执行/解码（强制 UTF-8、回退 GBK、剥离 ANSI，防日志乱码）
├── config.py     基础设施：Config 数据类 + 命令行解析
├── budget.py     基础设施：成本/耗时预算账本
├── artifacts.py  基础设施：每轮产物落盘
└── util.py       基础设施：控制台编码、JSON 配平解析
```

## 前置条件

- Python ≥ 3.10
- 已安装并登录 [`claude`](https://docs.claude.com/claude-code)（Claude Code）和 `codex`（Codex CLI）
- 在目标 **git 仓库**内运行（非 git 仓库也能跑，但会失去每轮快照/回滚能力）
- 工作区最好是干净的，方便用 `git diff` 看本轮改动
- 配好验收门命令（默认 `pytest -q`）

> **托管子会话下的鉴权**：在 Claude Code 托管子会话里跑时，宿主登录态不落地为 CLI 可读凭据，
> 子进程 `claude -p` 会 `Not logged in`。入口会自动处理：据 `--auth-channel` 选一条独立通道，
> 注入凭据并剥离宿主会话变量（`CLAUDE_CODE_*` / `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN`）。
> 两条通道，凭据来自环境变量或配置文件 `~/.claude_codex_orchestrator.env`：
> - `subscription`（订阅额度）：`CLAUDE_CODE_OAUTH_TOKEN=...`（普通终端 `claude setup-token` 获取）
> - `api`（API 计费）：`ANTHROPIC_API_KEY=sk-ant-...`
>
> `--auth-channel` 取 `auto`（默认，按 `CCO_DEFAULT_CHANNEL`/唯一可用/二者皆有时优先订阅）/
> `subscription` / `api`；显式通道缺凭据时 fail-fast。一次性配置即可；该配置文件含密钥，请勿入库。

## 用法

```bash
# 最简
python claude_codex_orchestrator.py "把 user 模块的密码改成 bcrypt 加盐哈希，并补单测"

# 指定仓库 / 验收门 / 轮数 / 模型
python claude_codex_orchestrator.py "需求…" \
    --repo ../app --test-cmd "pytest -q" --max-rounds 4 --model opus

# 验收门链：多个独立的门，逐个跑、分别反馈（任一不过即未达标）
python claude_codex_orchestrator.py "需求…" \
    --gate lint="ruff check ." --gate types="mypy ." --gate tests="pytest -q"

# 子任务 DAG：先把大需求拆成带依赖的子任务，按拓扑序逐个实现
python claude_codex_orchestrator.py "做一个带登录的待办应用" --decompose

# 静态站冒烟门：构建后校验所有 HTML/CSS 的本地资源引用是否真实存在（抓"构建过但运行时 404"）
python claude_codex_orchestrator.py "重构落地页" \
    --gate build="npm run build" --gate smoke="python scripts/smoke_static.py dist"

# 加成本与耗时预算，失败时自动回滚工作区
python claude_codex_orchestrator.py "需求…" \
    --budget-usd 2.0 --budget-seconds 1800 --rollback-on-fail

# 不真调模型，用假 agent 走通整个流程（自测 / 演示用）
python claude_codex_orchestrator.py "随便写点啥" --dry-run
```

### `--dry-run`：离线自测

没装 codex、或不在 git 仓库里时，加 `--dry-run` 会注入**确定性的假 agent**
（假 Claude / 假 Codex / 假验收门），不真调任何模型即可把规划→多轮实现→评审→完成的
**编排骨架**完整跑一遍。用途：

- 改完脚本后快速回归，确认控制流没坏
- 演示这套框架长什么样，而不消耗 token
- 假流程被设计成「第 1 轮 revise、第 2 轮 pass」，所以能看到多轮流转

## 命令行参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `task`（位置参数） | — | 要完成的需求（必填） |
| `--repo` | `.` | 目标仓库路径 |
| `--test-cmd` | `pytest -q` | 单个验收门命令（未用 `--gate` 时生效） |
| `--gate 名字=命令` | — | 验收门链的一环，可重复；任一不过即未达标。会覆盖 `--test-cmd` |
| `--max-rounds` | `3` | 最多几轮 实现→评审，防死循环 |
| `--model` | 默认 | 传给 `claude` 的 `--model`（不是 codex！） |
| `--codex-model` | 默认 | codex 的模型（`codex exec -m`），如 `gpt-5.5-codex` |
| `--codex-config k=v` | — | 透传给 codex 的配置（`codex exec -c`），可重复，如 `model_reasoning_effort=medium` 提速 |
| `--json-retries` | `2` | agent 没吐合法 JSON 时的额外重试次数 |
| `--claude-timeout` | `600` | 单次 claude 调用超时（秒） |
| `--codex-timeout` | `600` | 单次 codex 调用超时（秒） |
| `--gate-timeout` | `1200` | 单次验收门命令超时（秒），超时按未通过处理 |
| `--budget-usd` | `0`（不限） | 累计成本上限（美元，按 claude 报告的成本计），超限提前停止 |
| `--budget-seconds` | `0`（不限） | 累计耗时上限（秒），超限提前停止 |
| `--rollback-on-fail` | 关 | 最终失败时把工作区回滚到最近一次测试通过的快照（否则回到起点） |
| `--continue-on-fail` | 关 | 子任务失败时仍尝试其下游（仅告警，不整支跳过） |
| `--decompose` | 关 | 先把需求拆成子任务 DAG，按拓扑序逐个实现（失败只影响其下游） |
| `--dry-run` | 关 | 用假 agent 走通流程，不真调模型 |

## 产物与可恢复性

- **每轮日志**落盘到 `runs/<时间戳>/`：任务、规划 JSON、每轮的指令 / Codex stdout·stderr /
  diff / 验收门输出 / 评审回复（含 JSON 重试的每次原始回复）。排查问题时直接看这里。
- **用量 / 性能报告**：跑完终端打一张精简汇总表（成本 / token / 阶段耗时占比），并把
  结构化度量写到 `runs/<时间戳>/metrics.json`、自包含图表报告写到 `report.html`（零依赖、
  可离线打开 / 分享，含每轮 token、耗时按阶段、token 按 agent、各轮门链通过 4 张图）。
  成本仅 claude 上报（codex 吃订阅额度不报美元）；codex token 为 best-effort 解析。
- **每轮快照**（仅 git 仓库内）：用 `git stash create` 生成游离 commit 并打标签
  `orch/<run-id>/round<N>_after`，**不影响工作区**。任意一轮都可恢复：
  ```bash
  git stash apply orch/<run-id>/round2_after
  ```
- **自动回滚**（`--rollback-on-fail`）：最终失败时把被追踪文件恢复到“最近一次测试通过”的
  快照；回滚前会再打一个 `orch/<run-id>/pre_rollback` 标签，所以回滚本身也可撤销。

成功结束时，改动**留在工作区**（不自动 commit），请人工 review 后再提交。

## 健壮性设计

- **强制结构化交接**：两个 agent 之间只传 JSON。`extract_json` 用配平括号扫描提取第一个
  合法 JSON 对象，容忍前后夹文字 / markdown 代码块 / 字符串内含 `}` / 多个块；解析失败会
  追加纠正提示**自动重试**（`--json-retries`），而不是直接崩。
- **只读隔离**：Claude 仅 `Read,Grep,Glob`，保证“写代码”这件事只由 Codex 做。
- **验收门链**：`--gate` 把测试 / lint / 类型检查等拆成多个独立的门，逐个跑、分别记录
  通过/失败，任一不过即未达标；只把**失败门**的输出反馈给 Codex，更聚焦。
- **静态站冒烟门**（`scripts/smoke_static.py`）：零依赖（仅标准库）的现成门，遍历构建产物里所有
  HTML/CSS，校验本地资源引用（`img/script/link/source` + `url()` + `srcset`）是否都解析到真实
  文件——补上"build 能过、运行时却 404"的盲区（外链/锚点自动跳过）。当作 `--gate` 用即可。
- **结构化评审**：评审器输出 `findings` 清单（文件 / 定位 / 问题 / 修复指令），渲染成
  逐条指令交给 Codex，而非一段自由文本。
- **失败模式分流**：每轮失败会被归类为 `empty_diff`（没产生改动）/ `gate_failed`（门没过）/
  `review_revise`（门过了但评审要改），分别生成针对性的下一轮指令。
- **子任务 DAG**（`--decompose`）：规划阶段把大需求拆成带 `deps` 的子任务，拓扑排序后逐个跑
  「实现→门链→评审」循环；下游子任务会拿到已完成上游的上下文；某子任务失败时，**只有依赖它
  的下游被跳过**（除非加 `--continue-on-fail`），独立分支照常推进。校验 id 唯一 / 依赖存在 / 无环。
- **最大轮数 + 预算**：双重防死循环 / 防失控成本（预算在整个 DAG 内共享）。
- **跨平台输出**：启动即把 stdout/stderr 重配为 UTF-8（并开行缓冲，重定向到文件时进度实时可见），
  避免 Windows GBK 控制台遇到 `▶ ✅ 🎉` 等字符崩溃。

## 进展 / 后续方向

已完成「能稳定跑通 + 工程安全网 + 多门链 / 结构化评审 / 失败分流 / 子任务 DAG」。
更大的蓝图（呼应仓库名 `agent_corporation_framework`）：

- **第 3 层**：
  - **多 worker 并行**：DAG 里互不依赖的子任务并发实现（现在是拓扑序串行）。
  - **角色可插拔**：Worker 不限于 Codex（可换 Aider / 另一个 Claude / 本地模型）。
  - **共享黑板状态**：用一个 `state.json` 记录任务 / 决策 / 历史，让 agent 真正共享上下文。
  - **人在环（HITL）检查点**：关键节点（合并前、删文件前）暂停等人工确认。
  - **Web/TUI 看板**：可视化每个 agent 当前在做什么、DAG 进度、diff、门链状态。

## 兼容性/已修复问题
- 已修复日志乱码的问题，在当前设置中，强制子进程使用 UTF-8
- 捕获原始字节，UTF-8 优先、失败回退系统区域编码（GBK）

## ⚠️ 安全提示

`codex exec … --dangerously-bypass-approvals-and-sandbox` 会跳过逐步确认、对工作区有完整读写
执行权限。请只在**受控环境 / 隔离仓库**里运行，并在提交前人工 review 所有改动。
