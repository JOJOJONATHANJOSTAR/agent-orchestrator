"""系统提示词常量 + 评审清单渲染 + 失败模式分流的指令构造。"""
from __future__ import annotations

from .gates import gates_detail

PLANNER_SYS = (
    "你是技术规划者。阅读相关代码后，只输出一个 JSON 对象，不要任何解释或 markdown，格式严格为："
    '{"brief": "<给实现者的详细、可执行的改动说明>", '
    '"acceptance_criteria": ["<可客观验证的验收标准>", ...]}'
)

REVIEWER_SYS = (
    "你是严格的代码评审者。对照【本子任务的】验收标准，**逐条核对本轮 diff 里真实写出的代码**，"
    "判断每条标准是否被**功能性地**满足。要点："
    "① 验收门通过是**必要不充分**条件——门多为存在性/关键字/退出码检查，可能被"
    "「有函数名但逻辑是空壳」「关键字凑齐但实际跑不通」蒙混；你要读 diff 判断逻辑是否真的成立，"
    "不要因为门绿了就默认通过。"
    "② 只审本子任务 diff 与其验收标准，别把尚未开工的后续子任务算作缺陷。"
    "③ 发现真实问题就给 revise + 可执行的定位与修复；确无问题再 pass。"
    "只输出一个 JSON 对象，不要任何解释或 markdown，格式严格为："
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
    '"gate": "<可选：只验证本子任务产物的廉价客观命令，如 python -m py_compile x.py>", '
    '"deps": ["<本子任务依赖的前置子任务id>", ...]}, ...]}。'
    "deps 只能引用本列表中其他子任务的 id，且不得出现环。把需求拆成 2-6 个内聚、可独立验收的子任务。"
    "\n【关键·门的粒度】编排器只在**汇点子任务**（没有下游依赖它的那个/那些收尾节点）上跑"
    "「整体验收门」；非汇点子任务此时交付物尚不完整，别指望整体门能过。因此："
    "① 让**最后一个子任务负责集成收尾**（依赖其余子任务），整体验收就落在它身上；"
    "② 给能独立客观验证的中间子任务，尽量各配一个**廉价、只查本子任务产物**的 gate"
    "（语法检查 / 关键文件存在 / 目标函数可导入等）；无法客观自验的，留空 gate，由评审对照"
    "acceptance_criteria 把关。切忌把「跑整体验收脚本」写进中间子任务的 gate 或验收标准。"
)


def format_findings(findings: list) -> str:
    """把结构化评审清单渲染成给 Codex 的逐条修复指令。

    Args:
        findings (list): 评审 findings，每项理想为 ``{file, where, issue, fix}`` 的 dict；
            容忍非 dict 项（直接 str 化）。

    Returns:
        str: 多行编号修复指令文本。
    """
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

    Args:
        brief (str): 子任务原始改动说明。
        diff (str): 本轮工作区 diff。
        gate_results (list): GateRunner.run 返回的各门结果。
        verdict (dict): 评审裁决（含 findings / comments）。
        git_ok (bool): 是否为可用 git 仓库（决定能否据空 diff 判定"无改动"）。

    Returns:
        tuple[str, str]: (失败模式标签, 给 Codex 的下一轮指令)。失败模式标签 ∈
        ``{empty_diff, gate_failed, review_revise}``。
    """
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
