"""配置：Config 数据类、默认值、命令行解析。"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

DEFAULTS = {
    "repo": ".",                 # 目标仓库路径
    "test_cmd": "pytest -q",     # 验收门：能客观判定「过/不过」的命令（测试 / 编译 / lint）
    "max_rounds": 3,             # 防死循环：最多几轮 实现→评审
    "model": None,               # 给 claude 的 --model；None 表示用默认
    "codex_model": None,         # 给 codex 的 -m 模型；None 表示用 codex 默认
    "json_retries": 2,           # agent 没吐合法 JSON 时的额外重试次数
    "claude_timeout": 600,       # 单次 claude 调用超时（秒）
    "codex_timeout": 600,        # 单次 codex 调用超时（秒）；卡死时较快失败，而非干等

    "gate_timeout": 1200,        # 单次验收门命令超时（秒）
    "budget_usd": 0.0,           # 累计成本上限（美元）；0 = 不限
    "budget_seconds": 0,         # 累计耗时上限（秒）；0 = 不限
    "rollback_on_fail": False,   # 最终失败时是否把工作区回滚到最佳快照
    "continue_on_fail": False,   # 子任务失败时，是否仍尝试其下游（默认级联跳过）
    "no_plan": False,            # 跳过 Claude 规划：需求原文当 brief、验收门当标准（小任务省开销）
    "no_review": False,          # 跳过 Claude 评审：门全过即完成（Codex+门 纯净模式）
}


@dataclass
class Config:
    """一次运行的不可变设置（除 gates 解析外不在运行期改动）。"""
    repo: str
    test_cmd: str
    gates: list[tuple[str, str]]
    max_rounds: int
    model: str | None
    codex_model: str | None
    codex_config: list[str]
    json_retries: int
    claude_timeout: int
    codex_timeout: int
    gate_timeout: int
    budget_usd: float
    budget_seconds: int
    rollback_on_fail: bool
    continue_on_fail: bool
    decompose: bool
    dry_run: bool
    no_plan: bool
    no_review: bool


def build_arg_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。

    Returns:
        argparse.ArgumentParser: 已注册全部参数的解析器。
    """
    ap = argparse.ArgumentParser(description="Claude 规划 + Codex 实现 的编排器")
    ap.add_argument("task", help="要交给这套框架完成的需求")
    ap.add_argument("--repo", default=DEFAULTS["repo"], help="目标仓库路径")
    ap.add_argument("--test-cmd", default=DEFAULTS["test_cmd"],
                    help="单个验收门命令（未用 --gate 时生效）")
    ap.add_argument("--gate", action="append", default=[], metavar="名字=命令",
                    help="验收门链的一环，可重复，如 --gate tests=pytest --gate lint='ruff check .'")
    ap.add_argument("--max-rounds", type=int, default=DEFAULTS["max_rounds"])
    ap.add_argument("--model", default=DEFAULTS["model"], help="给 claude 的 --model（注意：只控制 claude，不是 codex）")
    ap.add_argument("--codex-model", default=DEFAULTS["codex_model"],
                    help="给 codex 的模型（codex exec -m），如 gpt-5.5-codex")
    ap.add_argument("--codex-config", action="append", default=[], metavar="key=value",
                    help="透传给 codex 的配置覆盖（codex exec -c），可重复，"
                         "如 --codex-config model_reasoning_effort=medium 降低推理强度提速")
    ap.add_argument("--json-retries", type=int, default=DEFAULTS["json_retries"])
    ap.add_argument("--claude-timeout", type=int, default=DEFAULTS["claude_timeout"],
                    help="单次 claude 调用超时（秒）")
    ap.add_argument("--codex-timeout", type=int, default=DEFAULTS["codex_timeout"],
                    help="单次 codex 调用超时（秒）")
    ap.add_argument("--gate-timeout", type=int, default=DEFAULTS["gate_timeout"],
                    help="单次验收门命令超时（秒）")
    ap.add_argument("--budget-usd", type=float, default=DEFAULTS["budget_usd"],
                    help="累计成本上限（美元，按 claude 报告的成本计）；0=不限")
    ap.add_argument("--budget-seconds", type=int, default=DEFAULTS["budget_seconds"],
                    help="累计耗时上限（秒）；0=不限")
    ap.add_argument("--rollback-on-fail", action="store_true",
                    help="最终失败时把工作区回滚到最近一次测试通过的快照（否则起点）")
    ap.add_argument("--continue-on-fail", action="store_true",
                    help="某子任务失败时，仍尝试依赖它的下游子任务（仅告警，不整支跳过）；"
                         "适合下游与失败者无强耦合的情况，避免一个卡点拖垮全站收尾类任务")
    ap.add_argument("--decompose", action="store_true",
                    help="先把需求拆成子任务 DAG，按拓扑序逐个实现（失败只影响其下游）")
    ap.add_argument("--no-plan", action="store_true",
                    help="跳过 Claude 规划：用需求原文当 brief、验收门当验收标准（小/清晰任务省一次规划开销；"
                         "与 --decompose 互斥，后者需要规划，同时给则忽略本项）")
    ap.add_argument("--no-review", action="store_true",
                    help="跳过 Claude 评审：验收门全过即视为完成（Codex+门 纯净模式，适合有可信验收门的小改）。"
                         "注意：默认行为已是「门过才评审」，本项更进一步连最终评审也省掉")
    ap.add_argument("--dry-run", action="store_true",
                    help="用假 agent 走通流程（不真调模型，便于自测）")
    ap.add_argument("--auth-channel", choices=["auto", "subscription", "api"],
                    default="auto",
                    help="托管子会话下 headless claude 的鉴权通道："
                         "subscription=订阅额度(CLAUDE_CODE_OAUTH_TOKEN)、api=API计费(ANTHROPIC_API_KEY)、"
                         "auto=按 CCO_DEFAULT_CHANNEL/唯一可用/二者皆有时优先订阅（默认）")
    return ap


def config_from_args(args: argparse.Namespace) -> Config:
    """把 argparse 结果转成 Config，并构建验收门链。

    Args:
        args (argparse.Namespace): build_arg_parser 解析出的参数。

    Returns:
        Config: 一次运行的不可变配置。

    Raises:
        SystemExit: ``--gate`` 项不符合 ``名字=命令`` 格式。
    """
    if args.gate:
        gates = []
        for g in args.gate:
            if "=" not in g:
                sys.exit(f"--gate 格式应为 名字=命令，收到：{g}")
            name, cmd = g.split("=", 1)
            gates.append((name.strip(), cmd.strip()))
    else:
        gates = [("tests", args.test_cmd)]

    return Config(
        repo=args.repo, test_cmd=args.test_cmd, gates=gates,
        max_rounds=args.max_rounds, model=args.model,
        codex_model=args.codex_model, codex_config=args.codex_config,
        json_retries=args.json_retries,
        claude_timeout=args.claude_timeout, codex_timeout=args.codex_timeout,
        gate_timeout=args.gate_timeout, budget_usd=args.budget_usd,
        budget_seconds=args.budget_seconds, rollback_on_fail=args.rollback_on_fail,
        continue_on_fail=args.continue_on_fail,
        decompose=args.decompose, dry_run=args.dry_run,
        no_plan=args.no_plan, no_review=args.no_review,
    )
