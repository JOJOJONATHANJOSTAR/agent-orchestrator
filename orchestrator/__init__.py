"""Claude 规划 + Codex 实现 编排器（orchestrator-worker）。

分层（依赖方向自上而下，无环）：
  cli            装配层：解析参数、注入依赖、跑总流程
  engine         编排层：SubtaskRunner（单子任务多轮循环）+ DagEngine（DAG 驱动）
  planner        领域层：Planner（规划/拆解）、Reviewer（评审）
  agents/gates   适配层：ClaudeClient/CodexClient/JsonAgent、GateRunner（外部进程）
  fakes          适配层替身：dry-run 用，实现与真实适配器相同的接口
  gitrepo        适配层：GitRepo（git 快照 / 回滚）
  prompts/graph  纯逻辑：系统提示、失败分流、拓扑排序、上下文拼装
  config/budget/artifacts/util  基础设施：配置、预算、日志落盘、控制台/JSON 工具
"""

__all__ = ["cli"]
