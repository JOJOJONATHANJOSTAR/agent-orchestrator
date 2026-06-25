"""验收门链：跑测试/lint/类型检查等外部命令，并格式化结果。"""
from __future__ import annotations

import subprocess

from .process import run


class GateRunner:
    """按配置依次跑验收门链。实现 engine 依赖的 Gates 接口（run）。"""

    def __init__(self, cfg):
        self.cfg = cfg

    def run(self) -> tuple[bool, list[dict]]:
        """返回 (是否全通过, 每个门的结果列表)。每个结果为 {name, cmd, passed, log}。
        超时按未通过处理。"""
        results = []
        for name, cmd in self.cfg.gates:
            try:
                r = run(cmd, cwd=self.cfg.repo, shell=True,
                        timeout=self.cfg.gate_timeout, clean=True)
                passed = r.returncode == 0
                log = (r.stdout + r.stderr)[-2000:]
            except subprocess.TimeoutExpired:
                passed, log = False, f"超时（>{self.cfg.gate_timeout}s）"
            results.append({"name": name, "cmd": cmd, "passed": passed, "log": log})
        return all(r["passed"] for r in results), results


def gates_summary(results: list[dict]) -> str:
    """一行汇总：tests ✅ | lint ❌ | types ✅"""
    return " | ".join(f"{r['name']} {'✅' if r['passed'] else '❌'}" for r in results)


def gates_detail(results: list[dict], failed_only: bool = False) -> str:
    """给 agent 看的多门详情文本块。"""
    blocks = []
    for r in results:
        if failed_only and r["passed"]:
            continue
        status = "通过" if r["passed"] else "失败"
        blocks.append(f"[门 {r['name']}（{r['cmd']}）{status}]\n{r['log']}")
    return "\n\n".join(blocks) if blocks else "（无）"
