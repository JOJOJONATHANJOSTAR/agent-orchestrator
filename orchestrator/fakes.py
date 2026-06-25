"""dry-run 用的确定性测试替身，实现与真实适配器相同的接口（LLM / Coder / Gates）。

剧本：每个子任务第 1 次评审 revise、第 2 次 pass；验收门第 1 轮 tests 挂、之后全过，
据此演示门链 + gate_failed/review_revise 分流 + 多轮流转，全程不调真模型。
"""
from __future__ import annotations


class _Brain:
    """共享计数器，驱动 FakeClaude 与 FakeGates 的确定性剧本。"""
    def __init__(self):
        self.round = 0


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
        # reviewer：每个子任务第 2 次评审才通过
        self.brain.round += 1
        if self.brain.round % 2 == 0:
            return '```json\n{"verdict":"pass","findings":[],"comments":""}\n```'
        return ('{"verdict":"revise","findings":[{"file":"calc.py","where":"add",'
                '"issue":"add 还没实现","fix":"新增 def add(a,b): return a+b"}],'
                '"comments":"补上 add"}')


class FakeCodex:
    def implement(self, prompt: str, label: str) -> None:
        print(f"  [dry-run] 假装 Codex 在做：{prompt[:40]}…")


class FakeGates:
    def __init__(self, brain: _Brain):
        self.brain = brain

    def run(self) -> tuple[bool, list[dict]]:
        tests_ok = self.brain.round >= 1
        return tests_ok, [
            {"name": "lint", "cmd": "ruff check .", "passed": True, "log": "dry-run: lint 通过"},
            {"name": "tests", "cmd": "pytest -q", "passed": tests_ok,
             "log": "dry-run: " + ("测试通过" if tests_ok else "1 failed")},
        ]


def make_fakes() -> tuple[FakeClaude, FakeCodex, FakeGates]:
    brain = _Brain()
    return FakeClaude(brain), FakeCodex(), FakeGates(brain)
