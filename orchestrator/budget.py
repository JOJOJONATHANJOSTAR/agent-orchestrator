"""预算账本：累计成本与耗时，提供门控判定。"""
from __future__ import annotations

import time


class Budget:
    """累计成本与耗时账本，并据上限提供门控判定。

    Args:
        budget_usd (float): 累计成本上限（美元）；0 表示不限。
        budget_seconds (int): 累计耗时上限（秒）；0 表示不限。
    """

    def __init__(self, budget_usd: float = 0.0, budget_seconds: int = 0):
        self.budget_usd = budget_usd
        self.budget_seconds = budget_seconds
        self.usd = 0.0
        self.start = time.time()

    def add(self, cost) -> None:
        """累加一次 claude 调用报告的成本（容忍 None / 非数值）。

        Args:
            cost: claude 返回的 total_cost_usd；非 int/float 时静默忽略。
        """
        if isinstance(cost, (int, float)):
            self.usd += cost

    def elapsed(self) -> float:
        """Returns: float — 自创建以来经过的秒数。"""
        return time.time() - self.start

    def ok(self) -> tuple[bool, str]:
        """是否还在成本/耗时预算内。

        Returns:
            tuple[bool, str]: (是否可继续, 超限说明)；未超限时说明为空串。
        """
        if self.budget_usd and self.usd >= self.budget_usd:
            return False, f"成本 ${self.usd:.4f} 已达上限 ${self.budget_usd:.4f}"
        if self.budget_seconds and self.elapsed() >= self.budget_seconds:
            return False, f"耗时 {self.elapsed():.0f}s 已达上限 {self.budget_seconds}s"
        return True, ""
