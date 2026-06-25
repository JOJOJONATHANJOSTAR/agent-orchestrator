"""日志落盘：把每轮产物写到 runs/<时间戳>/。"""
from __future__ import annotations

from pathlib import Path


class ArtifactLog:
    def __init__(self, run_dir: Path | str):
        self.dir = Path(run_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def write(self, name: str, content: str) -> None:
        """把本轮产物写到 runs/<时间戳>/name。"""
        try:
            (self.dir / name).write_text(content, encoding="utf-8")
        except OSError as e:
            print(f"  ⚠ 写日志失败 {name}: {e}")
