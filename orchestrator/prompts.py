"""系统提示词常量 + 评审清单渲染 + 失败模式分流的指令构造。"""
from __future__ import annotations

from .gates import gates_detail

PLANNER_SYS = (
    "你是技术规划者。阅读相关代码后，只输出一个 JSON 对象，不要任何解释或 markdown，格式严格为："
    '{"brief": "<给实现者的详细、可执行的改动说明>", '
    '"acceptance_criteria": ["<可客观验证的验收标准>", ...]}'
)

REVIEWER_SYS = (
    "你是代码评审者。对照验收标准审查本轮 diff 和验收门结果。只输出一个 JSON 对象，"
    "不要任何解释或 markdown，格式严格为："
    '{"verdict": "pass" 或 "revise", '
    '"findings": [{"file": "<文件路径>", "where": "<函数名或行号>", '
    '"issue": "<问题>", "fix": "<具体修复指令>"}, ...], '
    '"comments": "<可选的总体说明>"}。'
    "verdict 为 pass 时 findings 应为空数组。"
)

DAG_PLANNER_SYS = (
    "你是技术规划者。把需求拆解成有依赖关系的子任务（DAG）。阅读相关代码后，只输出一个 JSON "
    "对象，不要任何解释或 markdown，格式严格为："
    '{"subtasks": [{"id": "<短id如s1>", "title": "<一句话简述>", '
    '"brief": "<给实现者的详细、可执行的改动说明>", '
    '"acceptance_criteria": ["<可客观验证的验收标准>", ...], '
    '"deps": ["<本子任务依赖的前置子任务id>", ...]}, ...]}。'
    "deps 只能引用本列表中其他子任务的 id，且不得出现环。把需求拆成 2-6 个内聚、可独立验收的子任务。"
)


def format_findings(findings: list) -> str:
    """把结构化评审清单渲染成给 Codex 的逐条修复指令。"""
    lines = []
    for i, f in enumerate(findings, 1):
        if not isinstance(f, dict):
            lines.append(f"{i}. {f}")
            continue
        where = f.get("where") or f.get("locator") or "?"
        lines.append(
            f"{i}. [{f.get('file', '?')}:{where}] 问题：{f.get('issue', '')}"
            f" → 修复：{f.get('fix', '')}"
        )
    return "\n".join(lines)


def build_next_instruction(brief: str, diff: str, gate_results: list,
                           verdict: dict, git_ok: bool) -> tuple[str, str]:
    """失败模式分流：根据「空改动 / 验收门挂 / 评审要改」生成针对性的下一轮指令。
    返回 (失败模式标签, 给 Codex 的指令)。"""
    findings_txt = format_findings(verdict.get("findings", [])) or verdict.get("comments", "")

    if git_ok and not diff.strip():
        mode = "empty_diff"
        body = (
            "你上一轮没有产生任何代码改动。请确实地修改文件来满足下面的需求，不要只解释。\n"
            f"原始需求：\n{brief}"
        )
    elif not all(r["passed"] for r in gate_results):
        mode = "gate_failed"
        failed = [r["name"] for r in gate_results if not r["passed"]]
        body = (
            f"以下验收门未通过：{', '.join(failed)}。请优先修复使其全部通过。\n\n"
            f"失败门输出：\n{gates_detail(gate_results, failed_only=True)}\n\n"
            f"评审补充意见：\n{findings_txt}"
        )
    else:
        mode = "review_revise"
        body = (
            "验收门已全部通过，但评审认为还需改进。请按下列逐条意见修改：\n\n"
            f"{findings_txt}"
        )
    return mode, "上一轮未达标，请修复。\n" + body
