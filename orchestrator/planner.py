"""领域层：规划（plan / decompose）与评审（review），都建立在 JsonAgent 之上。"""
from __future__ import annotations

import sys

from .agents import JsonAgent
from .prompts import DAG_PLANNER_SYS, PLANNER_SYS, REVIEWER_SYS


class Planner:
    """规划者：把需求转成实现方案（plan）或子任务 DAG（decompose）。

    Args:
        agent (JsonAgent): 强制结构化 JSON 交接的 LLM 封装。
    """

    def __init__(self, agent: JsonAgent):
        self.agent = agent

    def plan(self, task: str) -> dict:
        """单任务规划：让 Claude 产出实现方案与验收标准。

        Args:
            task (str): 用户需求。

        Returns:
            dict: 含 ``brief`` 与 ``acceptance_criteria`` 的规划对象。

        Raises:
            SystemExit: 规划 JSON 缺少必要字段。
        """
        print("▶ Claude 规划中…")
        spec = self.agent.ask(
            f"需求：{task}\n请阅读相关代码后给出实现方案。", PLANNER_SYS, what="plan"
        )
        if "brief" not in spec or "acceptance_criteria" not in spec:
            sys.exit(f"[plan] 规划 JSON 缺少必要字段 brief/acceptance_criteria: {spec}")
        print("  验收标准：", spec["acceptance_criteria"])
        return spec

    def decompose(self, task: str) -> list[dict]:
        """让 Claude 把需求拆成子任务 DAG，并做字段补全/校验。

        容忍模型输出的形状差异：标准 ``{"subtasks":[...]}`` / 裸数组 / 单个子任务对象。

        Args:
            task (str): 用户需求。

        Returns:
            list[dict]: 子任务列表，每个补全了 ``title`` / ``deps`` 字段（未排序）。

        Raises:
            SystemExit: 未得到有效 subtasks 列表，或某子任务缺 id/brief/acceptance_criteria。
        """
        print("▶ Claude 拆解子任务 DAG…")
        data = self.agent.ask(
            f"需求：{task}\n请阅读相关代码后把它拆成子任务 DAG。",
            DAG_PLANNER_SYS, what="decompose",
        )
        # 容忍模型输出的形状差异：标准 {"subtasks":[...]} / 裸数组 [...] / 单个子任务对象
        if isinstance(data, list):
            subs = data
        elif isinstance(data, dict):
            subs = data.get("subtasks")
            if subs is None and ("id" in data or "brief" in data):
                subs = [data]
        else:
            subs = None
        if not isinstance(subs, list) or not subs:
            sys.exit(f"[decompose] 未得到有效 subtasks 列表：{data}")
        for s in subs:
            for k in ("id", "brief", "acceptance_criteria"):
                if k not in s:
                    sys.exit(f"[decompose] 子任务缺少字段 {k}：{s}")
            s.setdefault("title", s["id"])
            s.setdefault("deps", [])
            if not isinstance(s["deps"], list):
                s["deps"] = []
        return subs


class Reviewer:
    """评审者：对照验收标准审查每轮改动，给出 pass/revise 裁决与逐条修复意见。

    Args:
        agent (JsonAgent): 强制结构化 JSON 交接的 LLM 封装。
    """

    def __init__(self, agent: JsonAgent):
        self.agent = agent

    def review(self, acceptance: list, diff: str, gate_detail: str, label: str) -> dict:
        """让 Claude 对照验收标准评审本轮 diff 与验收门结果。

        Args:
            acceptance (list): 验收标准列表。
            diff (str): 本轮工作区 diff。
            gate_detail (str): 验收门详情文本（gates_detail 渲染）。
            label (str): 本轮标签，用于产物命名。

        Returns:
            dict: 含 ``verdict``（pass/revise）、``findings``（list）、``comments`` 三字段，
            缺失时已补默认值。
        """
        import json
        prompt = (
            f"验收标准：{json.dumps(acceptance, ensure_ascii=False)}\n\n"
            f"验收门结果：\n{gate_detail}\n\n本轮改动 diff：\n{diff}"
        )
        verdict = self.agent.ask(prompt, REVIEWER_SYS, what=f"{label}_review")
        verdict.setdefault("verdict", "revise")
        verdict.setdefault("findings", [])
        verdict.setdefault("comments", "")
        if not isinstance(verdict["findings"], list):
            verdict["findings"] = []
        return verdict
