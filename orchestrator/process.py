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
    非 Python 工具会忽略这些变量，不受影响。

    Returns:
        dict: 在当前环境副本上叠加 PYTHONUTF8/PYTHONIOENCODING 的环境变量字典。
    """
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def decode(b: bytes | None) -> str:
    """稳健解码：UTF-8 优先；失败回退系统区域编码（Windows 常见 cp936/GBK）；仍失败则替换。

    Args:
        b (bytes | None): 子进程输出的原始字节；None/空时返回空串。

    Returns:
        str: 解码后的文本；所有候选编码都失败时用 UTF-8 + replace 兜底，不抛异常。
    """
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
    """剥离文本中的 ANSI CSI/OSC 控制序列（彩色、光标移动等），便于干净落盘。

    Args:
        text (str): 可能含 ANSI 转义序列的文本。

    Returns:
        str: 去除控制序列后的纯文本。
    """
    return _ANSI.sub("", text)


def run(cmd, *, clean: bool = False, **kw) -> Result:
    """统一执行子进程：注入 UTF-8 子进程环境、捕获字节后稳健解码。

    透传 cwd / shell / timeout / input 等给 subprocess.run。stdin 默认接到 DEVNULL：
    headless 调用永不需要交互输入，若子进程（如 codex）误等 stdin，给它 EOF 而不是让整条
    流水线无限挂死；显式传 input 时则交由 subprocess 自管 stdin（两者互斥）。

    Args:
        cmd (list[str] | str): 要执行的命令；list 为参数向量，str 需配合 shell=True。
        clean (bool): 为 True 时剥离 stdout/stderr 里的 ANSI 控制序列。
        **kw: 透传给 subprocess.run 的关键字参数（cwd / shell / timeout / input 等）。

    Returns:
        Result: 具名元组 (returncode, stdout, stderr)，后两者已解码为 str。

    Raises:
        subprocess.TimeoutExpired: 传入 timeout 且子进程超时（不在此捕获，交调用方处理）。
    """
    kw.setdefault("env", child_env())
    # 提供 input 时由 subprocess 自管 stdin（input 与 stdin 互斥）；否则接 DEVNULL 防误等输入。
    if "input" not in kw:
        kw.setdefault("stdin", subprocess.DEVNULL)
    p = subprocess.run(cmd, capture_output=True, **kw)   # text=False → 拿原始字节
    out, err = decode(p.stdout), decode(p.stderr)
    if clean:
        out, err = strip_ansi(out), strip_ansi(err)
    return Result(p.returncode, out, err)
