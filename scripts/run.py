#!/usr/bin/env python3
"""agent-orchestrator skill 的调用入口（自包含、可分发给任意用户）。

定位含 orchestrator 包的根目录并加入 sys.path。**无机器特定的写死路径**——靠相对布局自动发现，
两种部署形态都覆盖、对其他用户开箱即用：
  - 仓库内：``<repo>/scripts/run.py`` + ``<repo>/orchestrator/``；
  - 随 skill 自带：``<skill>/scripts/run.py`` + ``<skill>/orchestrator/``（用 scripts/deploy.py 部署）。
两种布局里 orchestrator/ 都在本脚本的上一级，所以「从本脚本向上逐层查找」对二者都命中。

做三件 skill 场景需要、而裸 `python -m orchestrator` 不做的事：
  1. 定位含 orchestrator 包的根（环境变量 AGENT_ORCHESTRATOR_HOME → 从本脚本向上逐层查找），加入 sys.path；
  2. Windows 上从注册表刷新 PATH——本会话 shell 启动后才装的 claude/codex 也能被找到；
  3. 校验 claude/codex 在 PATH，缺失时清晰报错（--dry-run / --help / --check-auth 跳过）。

随后委托 orchestrator.cli.main（其中含托管子会话的自动鉴权）。参数与 `python -m orchestrator` 一致。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def find_package_root() -> Path:
    """定位含 orchestrator 包的根目录（无写死路径，靠相对布局自动发现）。

    顺序：
      1. 环境变量 ``AGENT_ORCHESTRATOR_HOME``（开发者覆盖：指向工作仓库，改完即生效、不必重新部署）；
      2. 从本脚本向上逐层查找 ``orchestrator/__init__.py``——同时覆盖「仓库内运行」与「随 skill
         自带 orchestrator/」两种布局（两者该包都在 run.py 上一级）。
    都找不到 → 清晰报错（自带包缺失说明部署不完整；或设 AGENT_ORCHESTRATOR_HOME 指向仓库）。
    """
    env = os.environ.get("AGENT_ORCHESTRATOR_HOME")
    if env and (Path(env) / "orchestrator" / "__init__.py").is_file():
        return Path(env)
    for parent in Path(__file__).resolve().parents:
        if (parent / "orchestrator" / "__init__.py").is_file():
            return parent
    sys.exit(
        "[run] 找不到 orchestrator 包。请确认二者之一：\n"
        "  • 本 skill 目录自带 orchestrator/（应与 scripts/ 同级；用 scripts/deploy.py 部署即自动带上）；\n"
        "  • 或设置环境变量 AGENT_ORCHESTRATOR_HOME 指向含 orchestrator/ 的仓库根。"
    )


def refresh_path_from_registry() -> None:
    """Windows 专用：把 HKLM/HKCU 的 Path 合并进当前进程环境（现有优先、注册表补充、去重）。

    本会话 shell 的 PATH 是它启动那一刻的快照；之后装的 claude/codex 不会反映进来。best-effort：
    任何异常都静默忽略（PATH 旧不致命，真缺工具会在 require_tools 里报）。"""
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
        key = c.lower().rstrip("\\")
        if c and key not in seen:
            seen.add(key)
            merged.append(c)
    os.environ["PATH"] = os.pathsep.join(merged)


def require_tools(argv: list[str]) -> None:
    """校验 claude / codex 可用；缺失则清晰报错。--dry-run / --help / --check-auth 时跳过。"""
    if {"--dry-run", "--help", "-h", "--check-auth"} & set(argv):
        return
    missing = [t for t in ("claude", "codex") if shutil.which(t) is None]
    if missing:
        sys.exit(
            f"[run] 找不到必需的命令行工具：{', '.join(missing)}。\n"
            "  已尝试从注册表刷新 PATH 仍未找到。请确认已安装并登录 claude (Claude Code) 与 "
            "codex (Codex CLI)，且它们在 PATH 上。\n"
            "  仅想验证编排流程而不真调模型时，可加 --dry-run。"
        )


def main() -> None:
    sys.path.insert(0, str(find_package_root()))
    refresh_path_from_registry()
    require_tools(sys.argv[1:])
    from orchestrator.cli import main as cli_main
    cli_main()


if __name__ == "__main__":
    main()
