#!/usr/bin/env python3
"""claude_codex_orchestrator.py —— 向后兼容入口。

「Claude 规划 + Codex 实现」编排器。原本的单体脚本已按职责拆分到 orchestrator/ 包：
  - 配置 / 预算 / 日志：orchestrator.config / budget / artifacts
  - 外部 agent 适配：orchestrator.agents（Claude/Codex）、orchestrator.gates（验收门）
  - git 快照/回滚：orchestrator.gitrepo
  - 纯逻辑：orchestrator.prompts（提示词/失败分流）、orchestrator.graph（DAG）
  - 编排：orchestrator.engine（多轮循环 + DAG 驱动）
  - 装配：orchestrator.cli

用法不变：
  python claude_codex_orchestrator.py "需求…" [--decompose] [--gate name=cmd] [--dry-run] …
也可作为模块运行：python -m orchestrator
"""

from orchestrator.cli import main

if __name__ == "__main__":
    main()
