"""编排层：SubtaskRunner（单子任务多轮循环）+ DagEngine（按拓扑序驱动 DAG）。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .config import parse_gate_spec
from .gates import gates_detail, gates_summary
from .graph import build_context, sink_ids
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

    def __init__(self, cfg, budget, artifacts, git, coder, gates, reviewer, ledger=None):
        self.cfg = cfg
        self.budget = budget
        self.artifacts = artifacts
        self.git = git
        self.coder = coder
        self.gates = gates
        self.reviewer = reviewer
        self.ledger = ledger

    def _begin(self, phase: str, sid: str, rnd: int) -> None:
        """给 ledger 设置当轮阶段上下文（dry-run 下 ledger 为 None，no-op）。"""
        if self.ledger is not None:
            self.ledger.begin(phase, sid, rnd)

    def run(self, subtask: dict, context: str,
            gates: list[tuple[str, str]] | None = None) -> tuple[str, str | None]:
        """对单个子任务跑「实现→门链→评审」多轮循环，直到通过或耗尽轮数/预算。

        Args:
            subtask (dict): 子任务，含 id / brief / acceptance_criteria。
            context (str): 前置子任务上下文（build_context 产出）。
            gates: 本子任务的**有效门链** ``[(名字, 命令)]``。``None`` 时退回全局 ``cfg.gates``；
                **空列表**表示「无客观门」（非汇点子任务），改由评审对照 acceptance_criteria 把关，
                此时要求本子任务本轮有实际改动（空 diff 不算通过）。

        Returns:
            tuple[str, str | None]: (状态, 最近一次全门通过的快照 tag)。状态 ∈
            ``{"done", "failed", "budget"}``；无可用快照时 tag 为 None。
        """
        sid = subtask["id"]
        brief = subtask["brief"]
        acceptance = subtask["acceptance_criteria"]
        instruction = brief if not context else f"{context}\n\n当前子任务：\n{brief}"
        best = None
        eff_gates = self.cfg.gates if gates is None else gates
        review_only = len(eff_gates) == 0        # 无客观门：全靠评审 + 「本轮须有改动」把关
        # 子任务起点的树快照：用于把每轮 diff 隔离成「只属于本子任务」的改动（排除前置子任务）
        base_tree = self.git.tree_snapshot()

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
            self._begin("impl", sid, rnd)
            self.coder.implement(instruction, label)

            diff = self.git.diff(base_tree)      # 相对子任务起点的隔离 diff
            self.artifacts.write(f"{label}.diff", diff)
            has_change = bool(diff.strip()) or not self.git.enabled
            if not has_change:
                print("  ⚠ 本轮没有产生任何代码改动（空 diff）。")

            self._begin("gate", sid, rnd)
            gates_ok, gate_results = self.gates.run(eff_gates)
            self.artifacts.write(f"{label}_gates.json",
                                 json.dumps(gate_results, ensure_ascii=False, indent=2))
            if review_only:
                print("  验收门链：（无客观门，交评审把关）"
                      + ("" if has_change else "  →  但本轮空 diff ❌"))
            else:
                print(f"  验收门链：{gates_summary(gate_results)}  →  "
                      f"{'全过 ✅' if gates_ok else '有未过 ❌'}")

            tag = self.git.snapshot(f"{label}_after")
            if tag:
                print(f"  ⛳ 本轮快照可恢复：git stash apply {tag}")
            if gates_ok and has_change and tag:
                best = tag

            print(f"  💳 累计成本 ${self.budget.usd:.4f} | 耗时 {self.budget.elapsed():.0f}s")

            # 是否够格进评审：有客观门则以门为准；无客观门（review_only）则要求本轮有实际改动，
            # 否则空 diff 会被评审「无据可查地放行」。门未过 / 空 diff 的轮次跳过评审——下一轮
            # 指令由 build_next_instruction 据门报错或空 diff 驱动即可（低价值评审开销省掉）。
            ready = gates_ok and (not review_only or has_change)
            if ready:
                if self.cfg.no_review:
                    verdict = {"verdict": "pass", "findings": [],
                               "comments": "(评审已禁用：门全过即通过)"}
                    print("  Claude 评审：已跳过（--no-review，门全过即完成）")
                else:
                    self._begin("review", sid, rnd)
                    verdict = self.reviewer.review(
                        acceptance, diff, gates_detail(gate_results), label)
                    print(f"  Claude 评审：{verdict['verdict']}"
                          + (f"（{len(verdict['findings'])} 条意见）" if verdict["findings"] else ""))
                if verdict["verdict"] == "pass":
                    print(f"  ✅ 子任务 [{sid}] 完成。")
                    return "done", best
            else:
                # 门未过或空 diff：跳过评审，给中性 verdict，让 build_next_instruction 走对应分支
                verdict = {"verdict": "revise", "findings": [], "comments": ""}
                reason = "空 diff，需实际改动" if review_only and not has_change else "门未过，先据门报错修复"
                print(f"  Claude 评审：已跳过（{reason}）")

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

    def _gates_for(self, st: dict, sinks: set[str]) -> tuple[list[tuple[str, str]], str]:
        """决定某子任务的**有效门链**，以及一句人读的选择理由。

        优先级：① 子任务自带 ``gate``/``gates``（廉价、只验本任务产物）→ 用它；
        ② 否则若是**汇点**（或单节点/非 decompose）→ 用全局 ``cfg.gates``（整体验收在此把关）；
        ③ 否则（非汇点、无自带门）→ 空门链，交评审对照 acceptance_criteria 把关。

        这是修「门粒度 vs 分解粒度错配」的核心：非汇点子任务不再被整体验收门逼着提前把
        后续子任务的活也干了。

        Args:
            st (dict): 子任务。
            sinks (set[str]): 汇点 id 集合（graph.sink_ids）。

        Returns:
            tuple[list[tuple[str, str]], str]: (有效门链, 选择理由)。
        """
        own = st.get("gate") or st.get("gates")
        if own:
            parsed = parse_gate_spec(own)
            if parsed:
                return parsed, f"子任务自带门（{', '.join(n for n, _ in parsed)}）"
        if not self.cfg.decompose or st["id"] in sinks:
            return self.cfg.gates, "整体验收门（汇点/单任务）"
        return [], "无客观门 → 交评审对照验收标准"

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
        sinks = sink_ids(subtasks)   # 只有汇点子任务才跑「整体验收门」

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
            eff_gates, why = self._gates_for(st, sinks)
            if self.cfg.decompose:
                print(f"  门策略：{why}")
            context = build_context(completed, st["deps"], blocked)
            status, best = self.runner.run(st, context, eff_gates)
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
