"""统一的子进程执行与输出解码。

解决 Windows 下日志乱码：子进程（如 python 写的验收门）默认按系统编码（cp936/GBK）输出，
若直接按 UTF-8 解码就会乱码。这里统一做三件事：
  1) 给子进程注入 UTF-8 环境，让 Python 类子进程直接以 UTF-8 输出；
  2) 捕获原始字节后稳健解码（UTF-8 优先，失败回退系统区域编码 / GBK）；
  3) 可选剥离 ANSI 控制序列（codex 等工具的彩色/光标输出），让落盘日志干净。
"""
from __future__ import annotations

import locale
import os
import re
import subprocess
from typing import NamedTuple

# CSI 序列（\x1b[...）与 OSC 序列（\x1b]...\x07）
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)")


class Result(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


def child_env() -> dict:
    """子进程环境：强制 Python 子进程用 UTF-8 输出，使其打印的中文等被正确解码。
    非 Python 工具会忽略这些变量，不受影响。"""
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def decode(b: bytes | None) -> str:
    """稳健解码：UTF-8 优先；失败回退系统区域编码（Windows 常见 cp936/GBK）；仍失败则替换。"""
    if not b:
        return ""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for enc in (locale.getpreferredencoding(False), "cp936", "latin-1"):
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return b.decode("utf-8", errors="replace")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def run(cmd, *, clean: bool = False, **kw) -> Result:
    """统一执行子进程：注入 UTF-8 子进程环境、捕获字节后稳健解码。clean=True 时剥离 ANSI。
    透传 cwd / shell / timeout 给 subprocess.run；超时照常抛 TimeoutExpired 交调用方处理。

    stdin 默认接到 DEVNULL：headless 调用永不需要交互输入，若子进程（如 codex）误等 stdin，
    给它 EOF 而不是让整条流水线无限挂死。"""
    kw.setdefault("env", child_env())
    kw.setdefault("stdin", subprocess.DEVNULL)
    p = subprocess.run(cmd, capture_output=True, **kw)   # text=False → 拿原始字节
    out, err = decode(p.stdout), decode(p.stderr)
    if clean:
        out, err = strip_ansi(out), strip_ansi(err)
    return Result(p.returncode, out, err)
