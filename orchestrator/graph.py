"""子任务 DAG 的纯逻辑：拓扑排序、上下文拼装。"""
from __future__ import annotations

import sys


def topo_order(subs: list[dict]) -> list[dict]:
    """对子任务 DAG 做拓扑排序（Kahn 算法）。

    Args:
        subs (list[dict]): 子任务列表，每个含 ``id`` 与 ``deps``（前置 id 列表）。

    Returns:
        list[dict]: 拓扑序排列的子任务列表（前置一定排在依赖它的子任务之前）。

    Raises:
        SystemExit: id 重复、依赖了不存在的子任务，或 DAG 中存在环。
    """
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


def sink_ids(subs: list[dict]) -> set[str]:
    """「汇点」子任务的 id 集合：没有任何其他子任务把它列为前置（deps）的那些。

    汇点是 DAG 的收尾节点——整体交付物只有到它们才算集齐。整体验收门（whole-deliverable
    gate，如 `python check_site.py`）应只在汇点子任务上把关；非汇点子任务此时交付物尚不完整，
    对它们跑整体门必然失败，会逼实现者提前把后续子任务的活也一起干了（分解就此失效）。

    Args:
        subs (list[dict]): 子任务列表，每个含 ``id`` 与 ``deps``。

    Returns:
        set[str]: 无下游依赖者的 id 集合。单节点 DAG 时即该唯一节点。
    """
    depended = {d for s in subs for d in s.get("deps", [])}
    return {s["id"] for s in subs if s["id"] not in depended}


def build_context(completed: list[dict], deps: list[str],
                  failed_deps: list[str] | None = None) -> str:
    """把当前子任务依赖到的、已完成的前置子任务，整理成给 Codex 的上下文。

    Args:
        completed (list[dict]): 已完成的子任务列表（含 id / title）。
        deps (list[str]): 当前子任务依赖的前置 id 列表。
        failed_deps (list[str] | None): 在 --continue-on-fail 下仍尝试本子任务时，未完成
            的前置 id 列表——据此提醒 Codex 这些前置可能不完整、勿假设它们已就绪。

    Returns:
        str: 拼装好的上下文文本；无可拼装内容时返回空串。
    """
    parts = []
    lines = [f"- [{m['id']}] {m['title']}：已完成" for m in completed if m["id"] in deps]
    if lines:
        parts.append("已完成的前置子任务（请在其基础上继续，不要重复实现）：\n" + "\n".join(lines))
    if failed_deps:
        parts.append(
            "⚠ 以下前置子任务未通过验收（" + ", ".join(failed_deps) + "）："
            "其产物可能缺失或不正确。请勿假设它们已就绪，涉及处先自行核查现状再动手。"
        )
    return "\n\n".join(parts)
