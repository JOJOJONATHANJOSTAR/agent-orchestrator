#!/usr/bin/env python3
"""scripts/run.py —— agent-orchestrator skill 的入口包装。

放在 orchestrator.cli.main 前面的瘦启动器，做三件 skill 场景需要、而裸 `python -m
orchestrator` 不做的事：

  1. 把仓库根加入 sys.path，使 `orchestrator` 包无论从哪个 CWD 调用都能被 import；
  2. 在 Windows 上尽力从注册表刷新 PATH——本会话 shell 启动后才安装的工具（claude/codex）
     即使不在旧 PATH 上也能被找到（best-effort，任何失败都吞掉，不影响后续运行）；
  3. 校验 claude / codex 在 PATH 上，缺失时给出清晰可操作的报错（--dry-run / --help 时跳过）。

随后委托给 orchestrator.cli.main()。参数与 `python -m orchestrator` 完全一致。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# 1) 让 `orchestrator` 无论 CWD 在哪都可 import
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def refresh_path_from_registry() -> None:
    """Windows 专用：把 HKLM/HKCU 的 Path 合并进当前进程环境。

    本会话 shell 的 PATH 是它启动那一刻的快照；之后用户装了 claude/codex 也不会反映进来。
    这里直接读注册表里的最新 Path 并按"现有优先、注册表补充"去重合并。best-effort：任何异常
    都静默忽略（PATH 旧不是致命错误，真缺工具会在 require_tools 里报）。"""
    if os.name != "nt":
        return
    try:
        import winreg
    except Exception:
        return
    extra: list[str] = []
    for hive, sub in (
        (winreg.HKEY_LOCAL_MACHINE,
         r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    ):
        try:
            with winreg.OpenKey(hive, sub) as key:
                val, _ = winreg.QueryValueEx(key, "Path")
        except OSError:
            continue
        if val:
            extra += os.path.expandvars(val).split(os.pathsep)
    if not extra:
        return
    seen: set[str] = set()
    merged: list[str] = []
    for chunk in os.environ.get("PATH", "").split(os.pathsep) + extra:
        c = chunk.strip()
        if c and c.lower() not in seen:
            seen.add(c.lower())
            merged.append(c)
    os.environ["PATH"] = os.pathsep.join(merged)


def require_tools(argv: list[str]) -> None:
    """校验 claude / codex 可用；缺失则清晰报错。--dry-run / --help 时跳过。"""
    if {"--dry-run", "--help", "-h"} & set(argv):
        return
    missing = [t for t in ("claude", "codex") if shutil.which(t) is None]
    if missing:
        sys.exit(
            f"[run] 找不到必需的命令行工具：{', '.join(missing)}。\n"
            "  请确认已安装并登录 claude (Claude Code) 与 codex (Codex CLI)，且它们在 PATH 上。\n"
            "  仅想验证编排流程而不真调模型时，可加 --dry-run。"
        )


def main() -> None:
    refresh_path_from_registry()
    require_tools(sys.argv[1:])
    from orchestrator.cli import main as cli_main
    cli_main()


if __name__ == "__main__":
    main()
