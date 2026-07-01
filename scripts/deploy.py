#!/usr/bin/env python3
"""把本仓库部署成**自包含**的 agent-orchestrator skill（可分发给任意用户）。

把 SKILL.md / README / scripts/ / orchestrator/ 复制到目标 skill 目录（默认
``~/.claude/skills/agent-orchestrator``），跳过 ``__pycache__`` / ``*.pyc`` / ``runs`` / ``.git``。
部署后该目录**自带 orchestrator 包**，run.py 靠相对布局自动发现，无任何机器特定写死路径——
换台机器、换个用户，把这个 skill 目录拷过去即可开箱即用。

开发者本机想「改完即生效、不必每次重新部署」：设环境变量 ``AGENT_ORCHESTRATOR_HOME`` 指向本仓库根，
run.py 会优先用它（见 run.py 的 find_package_root）。

用法：
    python scripts/deploy.py                # 部署到默认 ~/.claude/skills/agent-orchestrator
    python scripts/deploy.py <目标skill目录>  # 部署到指定目录
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

# Windows 控制台默认 GBK，打印 ✅/❌ 会崩；强制 stdout/stderr 用 UTF-8（失败静默降级）
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

_FILES = ["SKILL.md", "README.md", "README.en.md"]
_DIRS = ["scripts", "orchestrator"]
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "runs", ".git", "*.egg-info")


def main() -> None:
    repo = Path(__file__).resolve().parent.parent
    dst = (Path(sys.argv[1]).expanduser() if len(sys.argv) > 1
           else Path.home() / ".claude" / "skills" / "agent-orchestrator")
    dst.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    for f in _FILES:
        src = repo / f
        if src.is_file():
            shutil.copy2(src, dst / f)
            copied.append(f)
    for d in _DIRS:
        src = repo / d
        if not src.is_dir():
            continue
        # 先清掉目标同名目录再整体复制，避免残留已删除的旧模块（stale 文件）
        if (dst / d).exists():
            shutil.rmtree(dst / d)
        shutil.copytree(src, dst / d, ignore=_IGNORE)
        copied.append(d + "/")

    print(f"已部署到 {dst}")
    for c in copied:
        print("  +", c)

    # 自包含校验：orchestrator 包与 run.py 必须就位，否则别的用户跑不起来
    ok = (dst / "orchestrator" / "__init__.py").is_file() and (dst / "scripts" / "run.py").is_file()
    print("自包含校验：" + ("✅ orchestrator 包与 run.py 就位，可独立运行"
                          if ok else "❌ 缺关键文件，部署不完整"))
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
