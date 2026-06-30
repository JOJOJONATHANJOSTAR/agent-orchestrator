"""装配层：解析参数、注入依赖、跑总流程。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .agents import ClaudeClient, CodexClient, JsonAgent, prepare_agent_auth
from .artifacts import ArtifactLog
from .budget import Budget
from .config import build_arg_parser, config_from_args
from .engine import DagEngine, SubtaskRunner
from .fakes import make_fakes
from .gates import GateRunner
from .gitrepo import GitRepo
from .graph import topo_order
from .metrics import MetricsLedger
from .planner import Planner, Reviewer
from .report import render_html, terminal_summary
from .util import setup_console


def main(argv: list[str] | None = None) -> None:
    """编排器入口：解析参数、装配依赖、规划→执行 DAG→收尾。

    Args:
        argv (list[str] | None): 命令行参数；None 时取 sys.argv。

    Returns:
        None
    """
    args = build_arg_parser().parse_args(argv)
    setup_console()
    if not args.dry_run:
        # 托管子会话里为子进程配置独立鉴权；--auth-channel 选订阅额度 / API key（见 prepare_agent_auth）
        prepare_agent_auth(args.auth_channel)
    cfg = config_from_args(args)

    # ---- 基础设施 ----
    budget = Budget(cfg.budget_usd, cfg.budget_seconds)  # 预算账本：累计成本与耗时，提供门控判定
    ledger = MetricsLedger()                             # 度量账本：每次调用的 token/成本/耗时，供收尾报告
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")    # 用于日志目录和 git 标签
    run_dir = Path(cfg.repo) / "runs" / run_id           # 日志目录：每轮产物写到 runs/<时间戳>/
    artifacts = ArtifactLog(run_dir)                     # 日志落盘：把每轮产物写到 runs/<时间戳>/。
    git = GitRepo(cfg.repo, run_id)                      # git 仓库：提供 diff、快照、回滚等功能；非 git 仓库时降级为 no-op

    # ---- 适配器（dry-run 用替身）----
    if cfg.dry_run:
        llm, coder, gates = make_fakes()
    else:
        llm = ClaudeClient(cfg, budget, ledger)
        coder = CodexClient(cfg, artifacts, ledger)
        gates = GateRunner(cfg, ledger)

    # ---- 领域 + 编排（依赖注入装配）----
    agent = JsonAgent(llm, artifacts, cfg.json_retries)
    planner = Planner(agent)
    reviewer = Reviewer(agent)
    runner = SubtaskRunner(cfg, budget, artifacts, git, coder, gates, reviewer, ledger)
    engine = DagEngine(cfg, git, runner)

    print(f"运行 ID：{run_id}  | 仓库：{cfg.repo}  | git：{'是' if git.enabled else '否'} "
          f"| 日志：{run_dir}")
    print(f"验收门链：{' → '.join(n for n, _ in cfg.gates)}")
    artifacts.write("task.txt", args.task)

    # ---- 规划：DAG 模式拆子任务；否则单任务退化为单节点 DAG ----
    ledger.begin("plan")
    if cfg.decompose:
        subtasks = topo_order(planner.decompose(args.task))
        artifacts.write("dag.json", json.dumps(subtasks, ensure_ascii=False, indent=2))
        print("  子任务（拓扑序）：")
        for s in subtasks:
            dep = f" ⟵ {s['deps']}" if s["deps"] else ""
            print(f"    [{s['id']}] {s['title']}{dep}")
    else:
        spec = planner.plan(args.task)
        artifacts.write("plan.json", json.dumps(spec, ensure_ascii=False, indent=2))
        subtasks = [{"id": "main", "title": args.task[:30], "deps": [],
                     "brief": spec["brief"],
                     "acceptance_criteria": spec["acceptance_criteria"]}]

    # ---- 执行 ----
    res = engine.run(subtasks)

    # ---- 报告：度量落盘 + 自包含 HTML + 终端汇总 ----
    status = "全部完成" if res.all_done else ("预算用尽" if res.stopped_budget else "部分未完成")
    artifacts.write("metrics.json", ledger.to_json())
    artifacts.write("report.html", render_html(
        ledger, {"run_id": run_id, "repo": str(cfg.repo), "task": args.task, "status": status}))
    print("\n" + terminal_summary(ledger))
    print(f"  📊 图表报告：{run_dir / 'report.html'}")

    # ---- 收尾 ----
    print("\n===== 总结 =====")
    print(f"  完成：{sorted(res.done) or '无'}")
    if res.failed:
        print(f"  失败/跳过：{sorted(res.failed)}")
    if res.stopped_budget:
        print("  （因预算用尽提前停止）")

    if res.all_done:
        print("🎉 全部子任务完成：验收门全过且评审通过。改动已在工作区，请人工 review 后提交。")
        print(f"   完整日志见：{run_dir}")
        return

    print(f"⚠ 部分子任务未完成，请人工介入。完整日志见：{run_dir}")
    if cfg.rollback_on_fail:
        target = res.best_tag or res.initial_tag
        if git.restore(target):
            print(f"↩ 已把工作区回滚到 {target}（回滚前状态见标签 "
                  f"orch/{run_id}/pre_rollback，可恢复）。")
        else:
            print("↩ 回滚未执行（非 git 仓库或无可用快照）。")
