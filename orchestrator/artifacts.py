"""日志落盘：把每轮产物写到 runs/<时间戳>/。"""
from __future__ import annotations

from pathlib import Path


class ArtifactLog:
    """把每轮产物写到 ``runs/<时间戳>/`` 目录。

    Args:
        run_dir (Path | str): 本次运行的日志目录（不存在则创建）。
    """

    def __init__(self, run_dir: Path | str):
        self.dir = Path(run_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def write(self, name: str, content: str) -> None:
        """把本轮产物写到 ``runs/<时间戳>/<name>``（UTF-8）。

        Args:
            name (str): 产物文件名。
            content (str): 文件内容。

        Returns:
            None: 写失败仅打印告警，不抛异常（日志落盘不应中断主流程）。
        """
        try:
            (self.dir / name).write_text(content, encoding="utf-8")
        except OSError as e:
            print(f"  ⚠ 写日志失败 {name}: {e}")
