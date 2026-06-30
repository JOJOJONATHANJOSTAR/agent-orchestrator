#!/usr/bin/env python3
"""agent-orchestrator skill 的调用入口（仓库内 / 全局瘦部署两用）。

设计为「瘦指针」：不复制 orchestrator 包，而是定位到含它的仓库根并加入 sys.path——这样无论本
脚本是在仓库里、还是被部署到 ~/.claude/skills/ 下，跑的都是仓库里的最新代码（迭代只改仓库）。

做三件 skill 场景需要、而裸 `python -m orchestrator` 不做的事：
  1. 定位含 orchestrator 包的仓库根（环境变量 AGENT_ORCHESTRATOR_HOME → 从本脚本向上逐层查找
     → 已知绝对路径回退），加入 sys.path；
  2. Windows 上从注册表刷新 PATH——本会话 shell 启动后才装的 claude/codex 也能被找到；
  3. 校验 claude/codex 在 PATH，缺失时清晰报错（--dry-run / --help 跳过）。

随后委托 orchestrator.cli.main（其中含托管子会话的自动鉴权）。参数与 `python -m orchestrator` 一致。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# 仓库搬家后改这里，或设环境变量 AGENT_ORCHESTRATOR_HOME 指向仓库根
_FALLBACK_REPO = Path(r"D:\projects\agent_corporation_framework")


def find_repo_root() -> Path:
    """定位含 orchestrator 包的仓库根：环境变量 → 从本脚本向上逐层查找 → 绝对路径回退。"""
    env = os.environ.get("AGENT_ORCHESTRATOR_HOME")
    if env and (Path(env) / "orchestrator" / "__init__.py").is_file():
        return Path(env)
    for parent in Path(__file__).resolve().parents:
        if (parent / "orchestrator" / "__init__.py").is_file():
            return parent
    if (_FALLBACK_REPO / "orchestrator" / "__init__.py").is_file():
        return _FALLBACK_REPO
    sys.exit(
        "[run] 找不到 orchestrator 包：请设置环境变量 AGENT_ORCHESTRATOR_HOME 指向仓库根，"
        f"或修正 run.py 里的 _FALLBACK_REPO（当前 {_FALLBACK_REPO}）。"
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
    """校验 claude / codex 可用；缺失则清晰报错。--dry-run / --help 时跳过。"""
    if {"--dry-run", "--help", "-h"} & set(argv):
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
    sys.path.insert(0, str(find_repo_root()))
    refresh_path_from_registry()
    require_tools(sys.argv[1:])
    from orchestrator.cli import main as cli_main
    cli_main()


if __name__ == "__main__":
    main()
