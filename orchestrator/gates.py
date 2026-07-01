"""验收门链：跑测试/lint/类型检查等外部命令，并格式化结果。"""
from __future__ import annotations

import subprocess
import time

from .process import run


class GateRunner:
    """按配置依次跑验收门链。实现 engine 依赖的 Gates 接口（run）。"""

    def __init__(self, cfg, ledger=None):
        self.cfg = cfg
        self.ledger = ledger

    def run(self, gates: list[tuple[str, str]] | None = None) -> tuple[bool, list[dict]]:
        """依次执行验收门命令。超时按未通过处理。

        Args:
            gates: 本次要跑的门链 ``[(名字, 命令)]``；``None`` 时用全局 ``cfg.gates``。
                传入**空列表**表示「本子任务无客观门」（如非汇点子任务），直接返回
                ``(True, [])``，由评审对照 acceptance_criteria 把关。

        Returns:
            tuple[bool, list[dict]]: (是否全部通过, 各门结果列表)。每个结果形如
            ``{"name", "cmd", "passed", "log"}``，log 取输出末尾 2000 字符。空门链时
            列表为空、判为通过（``all([]) is True``）。
        """
        gates = self.cfg.gates if gates is None else gates
        results = []
        for name, cmd in gates:
            t0 = time.perf_counter()
            try:
                r = run(cmd, cwd=self.cfg.repo, shell=True,
                        timeout=self.cfg.gate_timeout, clean=True)
                passed = r.returncode == 0
                log = (r.stdout + r.stderr)[-2000:]
            except subprocess.TimeoutExpired:
                passed, log = False, f"超时（>{self.cfg.gate_timeout}s）"
            if self.ledger is not None:
                # 门链无 token/成本，只记耗时与通过与否（喂「耗时按阶段」与「门链网格」）
                self.ledger.record("gate", duration_s=time.perf_counter() - t0,
                                   label=name, ok=passed)
            results.append({"name": name, "cmd": cmd, "passed": passed, "log": log})
        return all(r["passed"] for r in results), results


def gates_summary(results: list[dict]) -> str:
    """一行汇总，如 ``tests ✅ | lint ❌ | types ✅``。

    Args:
        results (list[dict]): GateRunner.run 返回的结果列表。

    Returns:
        str: 各门名称加通过/未过图标的一行字符串。
    """
    return " | ".join(f"{r['name']} {'✅' if r['passed'] else '❌'}" for r in results)


def gates_detail(results: list[dict], failed_only: bool = False) -> str:
    """给 agent 看的多门详情文本块（含命令与日志）。

    Args:
        results (list[dict]): GateRunner.run 返回的结果列表。
        failed_only (bool): 为 True 时只渲染未通过的门。

    Returns:
        str: 多门详情文本；无可渲染项时返回 ``（无）``。
    """
    blocks = []
    for r in results:
        if failed_only and r["passed"]:
            continue
        status = "通过" if r["passed"] else "失败"
        blocks.append(f"[门 {r['name']}（{r['cmd']}）{status}]\n{r['log']}")
    return "\n\n".join(blocks) if blocks else "（无）"
