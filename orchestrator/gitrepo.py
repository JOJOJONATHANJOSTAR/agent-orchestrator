"""git 适配层：工作区 diff、非破坏性快照、回滚。"""
from __future__ import annotations

from .process import Result, run


class GitRepo:
    """封装目标仓库的 git 操作。非 git 仓库时各操作安全降级为 no-op。"""

    def __init__(self, repo: str, run_id: str):
        self.repo = repo
        self.run_id = run_id
        self.enabled = self._git("rev-parse", "--is-inside-work-tree").returncode == 0

    def _git(self, *args: str) -> Result:
        return run(["git", *args], cwd=self.repo)

    def diff(self) -> str:
        """工作区改动。包含新建(未跟踪)文件——用 intent-to-add 让它们出现在 diff，
        否则新建文件（很常见）会被 `git diff` 漏掉，导致评审看不到、误判空 diff。
        排除编排器自己的 runs/ 日志目录。"""
        if not self.enabled:
            return ""
        untracked = [f for f in self._git("ls-files", "--others", "--exclude-standard")
                     .stdout.split() if not f.startswith("runs/")]
        if untracked:
            self._git("add", "-N", *untracked)
        return self._git("diff").stdout

    def snapshot(self, label: str) -> str | None:
        """非破坏性快照：用 `git stash create` 生成游离 commit 捕获当前工作区，再打标签
        防止被 gc。返回标签名；无改动或非 git 仓库时返回 None。"""
        if not self.enabled:
            return None
        sha = self._git("stash", "create").stdout.strip()
        if not sha:
            return None
        tag = f"orch/{self.run_id}/{label}"
        self._git("tag", "-f", tag, sha)
        return tag

    def restore(self, tag: str | None) -> bool:
        """把工作区中被追踪的文件恢复到某个快照标签。回滚前先再快照一次确保可逆。"""
        if not self.enabled or not tag:
            return False
        self.snapshot("pre_rollback")
        return self._git("checkout", tag, "--", ".").returncode == 0
