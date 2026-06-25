"""子任务 DAG 的纯逻辑：拓扑排序、上下文拼装。"""
from __future__ import annotations

import sys


def topo_order(subs: list[dict]) -> list[dict]:
    """对子任务 DAG 做拓扑排序（Kahn）。校验 id 唯一、依赖存在、无环。"""
    by_id = {s["id"]: s for s in subs}
    if len(by_id) != len(subs):
        sys.exit("[dag] 子任务 id 有重复")
    indeg = {sid: 0 for sid in by_id}
    adj: dict[str, list[str]] = {sid: [] for sid in by_id}
    for s in subs:
        for d in s["deps"]:
            if d not in by_id:
                sys.exit(f"[dag] 子任务 {s['id']} 依赖了不存在的 {d}")
            adj[d].append(s["id"])
            indeg[s["id"]] += 1
    queue = [sid for sid, deg in indeg.items() if deg == 0]
    order = []
    while queue:
        sid = queue.pop(0)
        order.append(by_id[sid])
        for nb in adj[sid]:
            indeg[nb] -= 1
            if indeg[nb] == 0:
                queue.append(nb)
    if len(order) != len(subs):
        sys.exit("[dag] 子任务 DAG 存在环，无法拓扑排序")
    return order


def build_context(completed: list[dict], deps: list[str]) -> str:
    """把当前子任务依赖到的、已完成的前置子任务，整理成给 Codex 的上下文。"""
    lines = [f"- [{m['id']}] {m['title']}：已完成" for m in completed if m["id"] in deps]
    if not lines:
        return ""
    return "已完成的前置子任务（请在其基础上继续，不要重复实现）：\n" + "\n".join(lines)
