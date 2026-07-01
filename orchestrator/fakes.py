"""dry-run 用的确定性测试替身，实现与真实适配器相同的接口（LLM / Coder / Gates）。

剧本（与「门过才评审 + 门跟子任务走」的新流程一致，收敛由 codex 实现次数驱动、按子任务重置）：
- **汇点子任务**（跑整体门）：第 1 轮 tests 挂（演示 gate_failed 重试、此时跳过评审），第 2 轮
  tests 全过 → 触发评审 → pass → 完成。
- **非汇点子任务**（无客观门、review-only）：无门直接放行给评审；第 1 轮评审 revise、第 2 轮 pass。
FakeCodex 每轮真写一个 marker 文件，让「diff 隔离 / 树快照 / 空 diff 守卫 / 评审」这条链在
dry-run 下也被真实走到（否则无改动会触发 review-only 的空 diff 守卫而卡住）。全程不调真模型。
"""
from __future__ import annotations

from pathlib import Path


class _Brain:
    """共享状态：按子任务统计 codex 实现次数，驱动 FakeGates / FakeClaude 的确定性剧本。"""
    def __init__(self):
        self.cur_sid: str | None = None
        self.round_in_sub = 0

    def note_impl(self, label: str) -> None:
        """codex 每次实现时调用。label 形如 ``s1_round2``；子任务切换时重置轮计数。"""
        sid = label.rsplit("_round", 1)[0]
        if sid != self.cur_sid:
            self.cur_sid, self.round_in_sub = sid, 0
        self.round_in_sub += 1


class FakeClaude:
    def __init__(self, brain: _Brain):
        self.brain = brain

    def ask_text(self, prompt: str, system: str) -> str:
        if "subtasks" in system:            # DAG 规划器
            return ('{"subtasks":['
                    '{"id":"s1","title":"实现 add","brief":"在 calc.py 新增 add",'
                    '"acceptance_criteria":["有 add 函数"],"deps":[]},'
                    '{"id":"s2","title":"补单测","brief":"为 add 补 pytest 单测",'
                    '"acceptance_criteria":["pytest 全绿"],"deps":["s1"]}]}')
        if "技术规划者" in system:           # 单任务规划器
            return ('随便加点解释。{"brief":"实现 add(a,b) 返回 a+b 并补单测",'
                    '"acceptance_criteria":["pytest 全绿","存在 add 函数"]}')
        # reviewer：新流程下只在门全过时才被调用（round_in_sub≥2），此时直接 pass 完成
        if self.brain.round_in_sub >= 2:
            return '```json\n{"verdict":"pass","findings":[],"comments":""}\n```'
        return ('{"verdict":"revise","findings":[{"file":"calc.py","where":"add",'
                '"issue":"add 还没实现","fix":"新增 def add(a,b): return a+b"}],'
                '"comments":"补上 add"}')


class FakeCodex:
    def __init__(self, brain: _Brain, repo: str = "."):
        self.brain = brain
        self.repo = repo

    def implement(self, prompt: str, label: str) -> None:
        self.brain.note_impl(label)
        # 真写一个 marker，制造非空 diff——让 diff 隔离 / 树快照 / 空 diff 守卫也被真实走到
        try:
            (Path(self.repo) / "dryrun_marker.txt").write_text(
                f"{label}\n", encoding="utf-8")
        except OSError:
            pass
        print(f"  [dry-run] 假装 Codex 在做：{prompt[:40]}…")


class FakeGates:
    def __init__(self, brain: _Brain):
        self.brain = brain

    def run(self, gates=None) -> tuple[bool, list[dict]]:
        # 空门链（非汇点 review-only 子任务）：直接通过、无门结果，交评审把关
        if gates is not None and len(gates) == 0:
            return True, []
        tests_ok = self.brain.round_in_sub >= 2   # 每子任务第 1 轮挂、第 2 轮起过
        return tests_ok, [
            {"name": "lint", "cmd": "ruff check .", "passed": True, "log": "dry-run: lint 通过"},
            {"name": "tests", "cmd": "pytest -q", "passed": tests_ok,
             "log": "dry-run: " + ("测试通过" if tests_ok else "1 failed")},
        ]


def make_fakes(repo: str = ".") -> tuple[FakeClaude, FakeCodex, FakeGates]:
    brain = _Brain()
    return FakeClaude(brain), FakeCodex(brain, repo), FakeGates(brain)
