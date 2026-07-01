"""针对本轮优化（门跟子任务走 + diff 隔离 + gate 解析）的零依赖自测。

无需 pytest：直接 `python tests/test_optimizations.py`，全绿退出码 0，有失败非 0。
覆盖：graph.sink_ids、config.parse_gate_spec、DagEngine._gates_for 路由、GitRepo 按子任务
隔离 diff（真实 git 临时仓库）。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator.util import setup_console                  # noqa: E402

setup_console()                                              # Windows 控制台强制 UTF-8

from orchestrator.config import parse_gate_spec              # noqa: E402
from orchestrator.engine import DagEngine                    # noqa: E402
from orchestrator.gitrepo import GitRepo                     # noqa: E402
from orchestrator.graph import sink_ids                      # noqa: E402

_fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        _fails.append(msg)


def test_sink_ids() -> None:
    print("[sink_ids]")
    subs = [{"id": "s1", "deps": []}, {"id": "s2", "deps": []},
            {"id": "s3", "deps": ["s1", "s2"]}]
    check(sink_ids(subs) == {"s3"}, "线性收尾：只有 s3 是汇点")
    check(sink_ids([{"id": "main", "deps": []}]) == {"main"}, "单节点即汇点")
    # 两个独立汇点
    subs2 = [{"id": "a", "deps": []}, {"id": "b", "deps": ["a"]}, {"id": "c", "deps": ["a"]}]
    check(sink_ids(subs2) == {"b", "c"}, "多汇点：b、c 都无下游")


def test_parse_gate_spec() -> None:
    print("[parse_gate_spec]")
    check(parse_gate_spec("pytest -q") == [("check", "pytest -q")], "裸命令 → check")
    check(parse_gate_spec("lint=ruff check .") == [("lint", "ruff check .")], "名字=命令")
    # 命令里含 = 不应被误拆（名字部分不是短标识符）
    check(parse_gate_spec('python -c "a==1"') == [("check", 'python -c "a==1"')],
          "命令内的 == 不被误当分隔")
    check(parse_gate_spec(["python -m py_compile x.py", "test=pytest"])
          == [("check", "python -m py_compile x.py"), ("test", "pytest")], "list 混合")
    # 同名去重编号
    got = parse_gate_spec(["pytest a", "pytest b"])
    check([n for n, _ in got] == ["check1", "check2"], "重名门加序号避免撞行")
    check(parse_gate_spec([{"name": "syn", "cmd": "python -m py_compile x"}])
          == [("syn", "python -m py_compile x")], "dict 形状")
    check(parse_gate_spec("") == [] and parse_gate_spec([None, "  "]) == [], "空/非法项跳过")


def test_gates_for_routing() -> None:
    print("[DagEngine._gates_for 路由]")
    cfg = SimpleNamespace(decompose=True, gates=[("tests", "python check_site.py")])
    eng = DagEngine(cfg, git=None, runner=None)
    sinks = {"s5"}
    g_own, why = eng._gates_for({"id": "s1", "gate": "python -m py_compile x.py"}, sinks)
    check(g_own == [("check", "python -m py_compile x.py")] and "自带" in why,
          "自带门优先")
    g_sink, why = eng._gates_for({"id": "s5"}, sinks)
    check(g_sink == cfg.gates and "汇点" in why, "汇点用整体验收门")
    g_mid, why = eng._gates_for({"id": "s2"}, sinks)
    check(g_mid == [] and "评审" in why, "非汇点无自带门 → 空门链交评审")
    # 非 decompose：main 恒用整体门
    cfg2 = SimpleNamespace(decompose=False, gates=[("tests", "pytest")])
    eng2 = DagEngine(cfg2, git=None, runner=None)
    g_main, _ = eng2._gates_for({"id": "main"}, {"main"})
    check(g_main == cfg2.gates, "单任务模式恒用整体门")


def test_diff_isolation() -> None:
    print("[GitRepo 按子任务隔离 diff]")
    d = tempfile.mkdtemp()
    try:
        subprocess.run(["git", "init", "-q", d], check=True)
        subprocess.run(["git", "-C", d, "commit", "-q", "--allow-empty", "-m", "init"],
                       check=True)
        g = GitRepo(d, "testrun")
        check(g.enabled, "临时目录被识别为 git 仓库")

        base_s1 = g.tree_snapshot()
        (Path(d) / "a.txt").write_text("from s1\n", encoding="utf-8")
        d1 = g.diff(base_s1)
        check("a.txt" in d1 and "b.txt" not in d1, "s1 diff 只含 a.txt")

        base_s2 = g.tree_snapshot()      # 此刻基线已含 a.txt
        (Path(d) / "b.txt").write_text("from s2\n", encoding="utf-8")
        d2 = g.diff(base_s2)
        check("b.txt" in d2 and "a.txt" not in d2,
              "s2 diff 只含 b.txt（前置 s1 的改动被隔离掉）")

        # __pycache__ / pyc 噪声排除
        (Path(d) / "__pycache__").mkdir(exist_ok=True)
        (Path(d) / "__pycache__" / "x.pyc").write_text("junk", encoding="utf-8")
        base3 = g.tree_snapshot()
        (Path(d) / "c.txt").write_text("c\n", encoding="utf-8")
        d3 = g.diff(base3)
        check("__pycache__" not in d3 and "c.txt" in d3, "diff 排除 __pycache__/*.pyc")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main() -> int:
    for t in (test_sink_ids, test_parse_gate_spec, test_gates_for_routing,
              test_diff_isolation):
        t()
    print()
    if _fails:
        print(f"❌ {len(_fails)} 项断言失败：")
        for f in _fails:
            print("   - " + f)
        return 1
    print("✅ 全部断言通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
