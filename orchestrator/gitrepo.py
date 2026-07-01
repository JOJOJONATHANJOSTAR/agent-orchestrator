"""git 适配层：工作区 diff、非破坏性快照、回滚。"""
from __future__ import annotations

import os
import tempfile

from .process import Result, child_env, run

# diff / 树快照要排除的噪声路径：编排器自己的日志目录、Python 字节码缓存。
_EXCLUDE = (":(exclude)runs", ":(exclude)runs/**",
            ":(exclude)**/__pycache__/**", ":(exclude)**/*.pyc")


class GitRepo:
    """封装目标仓库的 git 操作。非 git 仓库时各操作安全降级为 no-op。

    Args:
        repo (str): 目标仓库路径。
        run_id (str): 本次运行 id，用于快照标签前缀（``orch/<run_id>/<label>``）。
    """

    def __init__(self, repo: str, run_id: str):
        self.repo = repo
        self.run_id = run_id
        self.enabled = self._git("rev-parse", "--is-inside-work-tree").returncode == 0

    def _git(self, *args: str, env: dict | None = None) -> Result:
        return run(["git", *args], cwd=self.repo, env=env or child_env())

    def tree_snapshot(self) -> str | None:
        """把当前工作区（含未跟踪文件、排除 runs/ 与 __pycache__）**非破坏性地**固化成一个
        git tree 对象，返回其 sha。用一次性临时 index，不碰真实 index / 工作区。

        这是「按子任务隔离 diff」的基石：在子任务开始与结束各取一次树快照，两棵树相 diff
        即得**只属于该子任务**的改动——而非 `git diff` 那样累计到 HEAD 的全量改动（会把前置
        子任务的改动一并算进来、让评审看不清本任务到底改了啥）。

        Returns:
            str | None: tree 对象 sha；非 git 仓库或异常时 None（调用方回退到全量 diff）。
        """
        if not self.enabled:
            return None
        idx = os.path.join(tempfile.gettempdir(),
                           f"orch-idx-{self.run_id}-{os.getpid()}")
        env = child_env()
        env["GIT_INDEX_FILE"] = idx
        try:
            # 以 HEAD 为基线播种临时 index（空仓库无 HEAD，则从空 index 开始）
            if self._git("rev-parse", "--verify", "-q", "HEAD").returncode == 0:
                self._git("read-tree", "HEAD", env=env)
            # 把工作区现状（含未跟踪、排除噪声）叠加到临时 index，再写成 tree
            self._git("add", "-A", "--", ".", *_EXCLUDE, env=env)
            tree = self._git("write-tree", env=env).stdout.strip()
            return tree or None
        finally:
            try:
                os.remove(idx)
            except OSError:
                pass

    def diff(self, base_tree: str | None = None) -> str:
        """改动的统一 diff 文本。

        - 传入 ``base_tree``（子任务起点的树快照 sha）时：返回 **base_tree → 当前工作区** 的
          树对树 diff，即本子任务的隔离改动（含未跟踪新文件、排除 runs/ 与 __pycache__）。
        - 不传时：回退到旧行为（工作区相对 HEAD 的全量 diff，用 intent-to-add 纳入未跟踪文件），
          用于非 git 仓库或拿不到基线的情形。

        Returns:
            str: 统一 diff 文本；非 git 仓库返回空串。
        """
        if not self.enabled:
            return ""
        cur = self.tree_snapshot()
        if base_tree and cur:
            return self._git("diff", base_tree, cur).stdout
        # 回退：全量 diff（相对 HEAD），intent-to-add 让未跟踪新文件也出现在 diff
        untracked = [f for f in self._git("ls-files", "--others", "--exclude-standard")
                     .stdout.split() if not f.startswith("runs/")]
        if untracked:
            self._git("add", "-N", *untracked)
        return self._git("diff", "--", ".", *_EXCLUDE).stdout

    def snapshot(self, label: str) -> str | None:
        """非破坏性快照：用 `git stash create` 生成游离 commit 捕获当前工作区，再打标签
        防止被 gc。

        Args:
            label (str): 快照标签后缀（最终标签为 ``orch/<run_id>/<label>``）。

        Returns:
            str | None: 快照标签名；无改动或非 git 仓库时返回 None。
        """
        if not self.enabled:
            return None
        sha = self._git("stash", "create").stdout.strip()
        if not sha:
            return None
        tag = f"orch/{self.run_id}/{label}"
        self._git("tag", "-f", tag, sha)
        return tag

    def restore(self, tag: str | None) -> bool:
        """把工作区中被追踪的文件恢复到某个快照标签。回滚前先再快照一次确保可逆。

        Args:
            tag (str | None): 目标快照标签；None 时直接返回 False。

        Returns:
            bool: 恢复是否成功（非 git 仓库或无 tag 时为 False）。
        """
        if not self.enabled or not tag:
            return False
        self.snapshot("pre_rollback")
        return self._git("checkout", tag, "--", ".").returncode == 0
