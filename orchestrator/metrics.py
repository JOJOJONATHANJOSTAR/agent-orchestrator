"""运行度量账本：记录每次子进程调用的 token / 成本 / 耗时 / 门链结果，供收尾报告聚合。

设计为「采集与渲染解耦」：本模块只负责**记录**与**结构化导出**，不关心怎么画图（那是
report.py 的事）。每次真实子进程调用（claude / codex / 验收门）记一条 Event，事件携带当时的
阶段上下文（由 begin 设置），收尾时聚合出各维度的汇总。零依赖、纯标准库。
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass


@dataclass
class Event:
    """一次子进程调用的度量记录。

    Attributes:
        agent (str): ``claude`` | ``codex`` | ``gate``。
        phase (str): ``plan`` | ``impl`` | ``gate`` | ``review``。
        subtask (str | None): 所属子任务 id；规划阶段为 None。
        round (int): 第几轮；规划阶段为 0。
        model (str): 模型名（拿得到时）。
        input_tokens / output_tokens / cache_read / cache_create (int): token 用量。
        cost_usd (float): 该次调用成本（仅 claude 报告；codex 恒 0）。
        duration_s (float): wall-time 秒。
        label (str): 细分标签（门事件存门名）。
        ok (bool | None): 门事件的通过与否；非门事件为 None。
    """
    agent: str
    phase: str
    subtask: str | None
    round: int
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_create: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    label: str = ""
    ok: bool | None = None


# codex exec 输出里的 token 用量合计，best-effort（格式随版本变动）。实测仅给「tokens used\n<数字>」，
# 不含 input/output 拆分；故只可靠解析合计——刻意不猜 input/output（松散正则会误匹配 diff 内的
# <input …> 等，得不偿失）。`\s` 跨行容忍「tokens used」与数字分两行。
_CODEX_TOTAL = re.compile(r"tokens?\s+used[^\d]{0,12}([\d,]+)", re.I)


def _to_int(s: str | None) -> int:
    """把 '12,345' 这类带千分位的串转 int；空/非法返回 0。"""
    if not s:
        return 0
    try:
        return int(s.replace(",", ""))
    except ValueError:
        return 0


def parse_codex_tokens(text: str) -> dict:
    """从 codex exec 的输出里**尽力**抓 token 用量合计。抓不到返回 0，绝不抛异常。

    注意：实测 ``codex exec`` 把用量打到 **stderr** 而非 stdout，且分两行
    （``tokens used`` 一行、数字 ``26,698`` 在下一行），故调用方应把 stdout+stderr 合并传入。
    这里只可靠解析**合计**（``tokens used`` 是特定短语，不会误匹配 diff）；codex 不给 input/output
    拆分，刻意不去猜（松散正则会把 diff 里的 ``<input …>`` 误判成 token 数）。``\\s`` 跨行容忍分行。
    取最后一次匹配（通常是最终累计值）。解析失败只意味着报告里 codex token 记 0，而非中断主流程。

    Args:
        text (str): codex exec 的输出（建议 stdout+stderr 合并、已剥离 ANSI）。

    Returns:
        dict: ``{"total"}`` 一个键，int；抓不到为 0。
    """
    if not text:
        return {"total": 0}
    tot = _CODEX_TOTAL.findall(text)
    return {"total": _to_int(tot[-1]) if tot else 0}


class MetricsLedger:
    """运行度量账本：begin 设置阶段上下文，record 据上下文落一条事件。

    采集点（agents/gates）只管报自己知道的 token/成本/耗时；阶段/轮次/子任务由 engine、cli
    在调用前用 begin 注入，避免给每个适配器方法都加一串参数。
    """

    def __init__(self) -> None:
        self.events: list[Event] = []
        self._ctx: dict = {"phase": "plan", "subtask": None, "round": 0}

    def begin(self, phase: str, subtask: str | None = None, round: int = 0) -> None:
        """设置后续 record 的阶段上下文。

        Args:
            phase (str): plan / impl / gate / review。
            subtask (str | None): 子任务 id；规划阶段 None。
            round (int): 轮次；规划阶段 0。
        """
        self._ctx = {"phase": phase, "subtask": subtask, "round": round}

    def record(self, agent: str, *, model: str = "", input_tokens: int = 0,
               output_tokens: int = 0, cache_read: int = 0, cache_create: int = 0,
               cost_usd: float = 0.0, duration_s: float = 0.0,
               label: str = "", ok: bool | None = None) -> None:
        """按当前阶段上下文落一条事件。失败容忍：所有数值字段都有默认 0。"""
        self.events.append(Event(
            agent=agent, phase=self._ctx["phase"], subtask=self._ctx["subtask"],
            round=self._ctx["round"], model=model, input_tokens=input_tokens,
            output_tokens=output_tokens, cache_read=cache_read, cache_create=cache_create,
            cost_usd=cost_usd, duration_s=duration_s, label=label, ok=ok))

    # ---- 聚合（供报告使用）----
    def total_cost(self) -> float:
        """Returns: float — 累计成本（USD），仅含 claude 上报值。"""
        return sum(e.cost_usd for e in self.events)

    def total_duration(self) -> float:
        """Returns: float — 所有被计时调用的 wall-time 之和（秒）。"""
        return sum(e.duration_s for e in self.events)

    def total_tokens(self) -> int:
        """Returns: int — 输入+输出 token 总和（不含缓存命中，避免重复计）。"""
        return sum(e.input_tokens + e.output_tokens for e in self.events)

    def to_json(self) -> str:
        """导出结构化度量（events 明细 + 概要）为 JSON 字符串，落 runs/<id>/metrics.json。"""
        payload = {
            "summary": {
                "total_cost_usd": round(self.total_cost(), 6),
                "total_tokens": self.total_tokens(),
                "total_duration_s": round(self.total_duration(), 3),
                "calls": len(self.events),
            },
            "events": [asdict(e) for e in self.events],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)
