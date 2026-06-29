"""最底层无依赖工具：控制台编码、JSON 配平解析。"""
from __future__ import annotations

import json
import sys


def setup_console() -> None:
    """避免 Windows GBK 控制台遇到 ▶/✅ 等字符直接 UnicodeEncodeError 崩溃。

    同时开 line_buffering：重定向到文件（如 `python run.py > orch.log`）时，stdout 默认
    走块缓冲，整轮日志要等进程退出才落盘、进度全程不可见。逐行刷新让 orch.log 实时可跟。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (AttributeError, ValueError):
            pass


def extract_json(text):
    """从文本中提取第一个「配平」的 JSON 值——对象 `{...}` 或数组 `[...]`（容忍前后多余文字 /
    markdown / 多个块 / 字符串内含括号）。

    同时支持对象与数组很关键：真实模型有时会把本应包在 {"subtasks": [...]} 里的列表直接当顶层
    数组返回，或外层对象有瑕疵——这两种情况下回退扫描会命中内层数组，避免只抓到单个子任务。

    Args:
        text (str): 可能内嵌 JSON 的任意文本（通常是模型的原始回复）。

    Returns:
        dict | list | None: 解析出的第一个合法 JSON 值；找不到时返回 None（不抛异常）。
    """
    pairs = {"{": "}", "[": "]"}
    starts = [i for i, ch in enumerate(text) if ch in pairs]
    for start in starts:
        open_ch, close_ch = text[start], pairs[text[start]]
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break  # 这一段不合法，从下一个开括号重试
    return None
