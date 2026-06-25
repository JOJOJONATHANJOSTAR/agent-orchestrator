"""编排层：SubtaskRunner（单子任务多轮循环）+ DagEngine（按拓扑序驱动 DAG）。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .gates import gates_detail, gates_summary
from .graph import build_context
from .prompts import build_next_instruction


class SubtaskRunner:
    """对单个子任务跑「实现→门链→评审」多轮循环。只依赖注入进来的抽象。"""

    def __init__(self, cfg, budget, artifacts, git, coder, gates, reviewer):
        self.cfg = cfg
        self.budget = budget
        self.artifacts = artifacts
        self.git = git
        self.coder = coder
        self.gates = gates
        self.reviewer = reviewer

    def run(self, subtask: dict, context: str) -> tuple[str, str | None]:
        """返回 (状态, 最近一次全门通过的快照tag)；状态 ∈ {"done","failed","budget"}。"""
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
        res = DagResult(initial_tag=self.git.snapshot("initial"))
        res.best_tag = res.initial_tag
        completed: list[dict] = []

        for st in subtasks:
            blocked = [d for d in st["deps"] if d in res.failed]
            if blocked:
                print(f"\n⏭ 跳过子任务 [{st['id']}]：前置未完成 {blocked}")
                res.failed.add(st["id"])
                continue

            header = f"子任务 [{st['id']}] {st['title']}" if self.cfg.decompose else "实现"
            print(f"\n========== {header} ==========")
            context = build_context(completed, st["deps"])
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
