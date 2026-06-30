"""领域层：规划（plan / decompose）与评审（review），都建立在 JsonAgent 之上。"""
from __future__ import annotations

import sys

from .agents import JsonAgent
from .prompts import DAG_PLANNER_SYS, PLANNER_SYS, REVIEWER_SYS


class Planner:
    def __init__(self, agent: JsonAgent):
        self.agent = agent

    def plan(self, task: str) -> dict:
        print("▶ Claude 规划中…")
        # 让 Claude 给出实现方案（brief）和验收标准（acceptance_criteria），并做字段校验
        spec = self.agent.ask(
            f"需求：{task}\n请阅读相关代码后给出实现方案。", PLANNER_SYS, what="plan"
        )
        # 容忍模型输出的形状差异：标准 {"brief": "...", "acceptance_criteria": [...]} / 裸对象 {...}
        if "brief" not in spec or "acceptance_criteria" not in spec:
            sys.exit(f"[plan] 规划 JSON 缺少必要字段 brief/acceptance_criteria: {spec}")
        print("  验收标准：", spec["acceptance_criteria"])
        return spec

    def decompose(self, task: str) -> list[dict]:
        """让 Claude 把需求拆成子任务 DAG，并做字段补全/校验。"""
        print("▶ Claude 拆解子任务 DAG…")
        # 让 Claude 给出子任务列表（subtasks），并做字段校验
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
    """基于 JsonAgent 的评审器：对照验收标准审查改动 diff 和验收门结果，输出结构化评审清单。"""
    def __init__(self, agent: JsonAgent):
        self.agent = agent

    def review(self, acceptance: list, diff: str, gate_detail: str, label: str) -> dict:
        # 函数返回结构化评审结果：{"verdict": "pass" / "revise", "findings": [...], "comments": "..."}
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
