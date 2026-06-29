"""编排层：SubtaskRunner（单子任务多轮循环）+ DagEngine（按拓扑序驱动 DAG）。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .gates import gates_detail, gates_summary
from .graph import build_context
from .prompts import build_next_instruction


class SubtaskRunner:
    """对单个子任务跑「实现→门链→评审」多轮循环。只依赖注入进来的抽象。

    Args:
        cfg: 运行配置（max_rounds 等）。
        budget (Budget): 成本/耗时账本。
        artifacts (ArtifactLog): 产物落盘。
        git (GitRepo): 快照与 diff。
        coder (Coder): 代码实现适配器。
        gates (Gates): 验收门链。
        reviewer (Reviewer): 评审者。
    """

    def __init__(self, cfg, budget, artifacts, git, coder, gates, reviewer):
        self.cfg = cfg
        self.budget = budget
        self.artifacts = artifacts
        self.git = git
        self.coder = coder
        self.gates = gates
        self.reviewer = reviewer

    def run(self, subtask: dict, context: str) -> tuple[str, str | None]:
        """对单个子任务跑「实现→门链→评审」多轮循环，直到通过或耗尽轮数/预算。

        Args:
            subtask (dict): 子任务，含 id / brief / acceptance_criteria。
            context (str): 前置子任务上下文（build_context 产出）。

        Returns:
            tuple[str, str | None]: (状态, 最近一次全门通过的快照 tag)。状态 ∈
            ``{"done", "failed", "budget"}``；无可用快照时 tag 为 None。
        """
        sid = subtask["id"]
        brief = subtask["brief"]
        acceptance = subtask["acceptance_criteria"]
        instruction = brief if not context else f"{context}\n\n当前子任务：\n{brief}"
        best = None

        for rnd in range(1, self.cfg.max_rounds + 1):
            ok, why = self.budget.ok()
            if not ok:
                print(f"\n⛔ 预算用尽：{why}，提前停止。")
                return "budget", best

            label = f"{sid}_round{rnd}"
            print(f"\n--- [{sid}] 第 {rnd}/{self.cfg.max_rounds} 轮 ---")
            self.git.snapshot(f"{label}_before")

            print("▶ Codex 实现中…")
            self.artifacts.write(f"{label}_instruction.txt", instruction)
            self.coder.implement(instruction, label)

            diff = self.git.diff()
            self.artifacts.write(f"{label}.diff", diff)
            if not diff.strip() and self.git.enabled:
                print("  ⚠ 本轮没有产生任何代码改动（空 diff）。")

            passed, gate_results = self.gates.run()
            self.artifacts.write(f"{label}_gates.json",
                                 json.dumps(gate_results, ensure_ascii=False, indent=2))
            print(f"  验收门链：{gates_summary(gate_results)}  →  "
                  f"{'全过 ✅' if passed else '有未过 ❌'}")

            tag = self.git.snapshot(f"{label}_after")
            if tag:
                print(f"  ⛳ 本轮快照可恢复：git stash apply {tag}")
            if passed and tag:
                best = tag

            print(f"  💳 累计成本 ${self.budget.usd:.4f} | 耗时 {self.budget.elapsed():.0f}s")

            verdict = self.reviewer.review(acceptance, diff, gates_detail(gate_results), label)
            print(f"  Claude 评审：{verdict['verdict']}"
                  + (f"（{len(verdict['findings'])} 条意见）" if verdict["findings"] else ""))

            if passed and verdict["verdict"] == "pass":
                print(f"  ✅ 子任务 [{sid}] 完成。")
                return "done", best

            mode, instruction = build_next_instruction(
                brief, diff, gate_results, verdict, self.git.enabled)
            print(f"  ↪ 失败模式：{mode}，已生成下一轮针对性指令。")

        print(f"  ❌ 子任务 [{sid}] 到达最大轮数 {self.cfg.max_rounds} 仍未通过。")
        return "failed", best


@dataclass
class DagResult:
    """DAG 执行结果汇总。

    Attributes:
        done (set): 完成的子任务 id。
        failed (set): 失败或被跳过的子任务 id。
        all_done (bool): 是否所有子任务都完成。
        stopped_budget (bool): 是否因预算用尽提前停止。
        best_tag (str | None): 最近一次全门通过的快照 tag（回滚目标）。
        initial_tag (str | None): 起点快照 tag。
    """
    done: set = field(default_factory=set)
    failed: set = field(default_factory=set)
    all_done: bool = False
    stopped_budget: bool = False
    best_tag: str | None = None
    initial_tag: str | None = None


class DagEngine:
    """按拓扑序逐个跑子任务；失败沿 DAG 向下游传播（依赖失败者的被跳过）。"""

    def __init__(self, cfg, git, runner: SubtaskRunner):
        self.cfg = cfg
        self.git = git
        self.runner = runner

    def run(self, subtasks: list[dict]) -> DagResult:
        """按拓扑序逐个跑子任务，并按依赖处理失败级联。

        默认下游依赖失败者会被整支跳过；cfg.continue_on_fail 为真时改为仅告警、仍尝试
        （并在上下文标注前置未完成）。

        Args:
            subtasks (list[dict]): 已拓扑排序的子任务列表。

        Returns:
            DagResult: 完成/失败集合、是否全完成、是否因预算停止、最佳与初始快照 tag。
        """
        res = DagResult(initial_tag=self.git.snapshot("initial"))
        res.best_tag = res.initial_tag # 初始快照可作为回滚目标
        completed: list[dict] = []

        for st in subtasks:
            blocked = [d for d in st["deps"] if d in res.failed]
            if blocked and not self.cfg.continue_on_fail:
                print(f"\n⏭ 跳过子任务 [{st['id']}]：前置未完成 {blocked}")
                res.failed.add(st["id"])
                continue

            header = f"子任务 [{st['id']}] {st['title']}" if self.cfg.decompose else "实现"
            print(f"\n========== {header} ==========")
            if blocked:
                print(f"⚠ 前置未完成 {blocked}，--continue-on-fail 下仍尝试本子任务"
                      f"（上下文可能不完整）。")
            context = build_context(completed, st["deps"], blocked)
            status, best = self.runner.run(st, context)
            if best:
                res.best_tag = best

            if status == "budget":
                res.stopped_budget = True
                res.failed.add(st["id"])
                break
            if status == "done":
                res.done.add(st["id"])
                completed.append(st)
            else:
                res.failed.add(st["id"])

        res.all_done = len(res.done) == len(subtasks)
        return res
