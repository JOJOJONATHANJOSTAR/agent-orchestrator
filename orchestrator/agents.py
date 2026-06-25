"""适配层：与外部 agent（Claude / Codex）的进程通信，以及强制 JSON 的封装。

定义三个接口（Protocol），engine/planner 只依赖接口，便于 dry-run 用 fakes 替换：
  - LLM.ask_text(prompt, system) -> str     大脑（Claude）
  - Coder.implement(prompt, label) -> None   双手（Codex）
  - Gates.run() -> (bool, list[dict])        验收门（见 gates.GateRunner）
"""
from __future__ import annotations

import json
import sys
from typing import Protocol

from .process import run
from .util import extract_json


class LLM(Protocol):
    """大脑：与 Claude 进程通信，返回文本。"""
    def ask_text(self, prompt: str, system: str) -> str: ...


class Coder(Protocol):
    """双手：与 Codex 进程通信，执行代码实现。"""
    def implement(self, prompt: str, label: str) -> None: ...


class ClaudeClient:
    """headless 调 Claude，只读工具，累计成本到 budget。"""

    def __init__(self, cfg, budget):
        self.cfg = cfg
        self.budget = budget

    def ask_text(self, prompt: str, system: str) -> str:
        cmd = [
            "claude", "-p", prompt,
            "--output-format", "json",
            "--append-system-prompt", system,
            "--allowedTools", "Read,Grep,Glob",   # 只读：写代码只交给 Codex
        ]
        if self.cfg.model:
            cmd += ["--model", self.cfg.model]
        r = run(cmd, cwd=self.cfg.repo, timeout=self.cfg.claude_timeout)
        if r.returncode != 0:
            # claude 的实际错误（如无效模型 / API 报错）常在 stdout 的 JSON 里，stderr 可能为空
            detail = (r.stderr.strip() or r.stdout.strip())[:400] or "(无输出)"
            sys.exit(f"[claude] 调用失败（rc={r.returncode}）: {detail}")
        data = json.loads(r.stdout)
        if data.get("is_error"):
            sys.exit(f"[claude] 调用失败: {str(data.get('result', ''))[:400]}")
        self.budget.add(data.get("total_cost_usd"))
        return data["result"]


class CodexClient:
    """headless 调 Codex 实现代码。--full-access 跳过逐步确认（请确认在受控环境运行）。"""

    def __init__(self, cfg, artifacts):
        self.cfg = cfg
        self.artifacts = artifacts

    def implement(self, prompt: str, label: str) -> None:
        # 非交互执行：跳过审批与沙箱（codex-cli ≥ 0.x 用此 flag，旧的 --full-access 已不存在）。
        # --skip-git-repo-check 让非 git 目录也能跑。请确认在受控环境运行。
        # 约束 Codex 只改文件、不自行提交——否则改动进了 commit，编排器的 diff/快照/回滚
        # 和"人工 review 后再提交"的安全模型都会被破坏。
        guarded = prompt + (
            "\n\n[执行约束] 只创建/修改文件来完成任务；不要运行 git add / commit / push / stash，"
            "也不要回滚或清理工作区——所有改动必须留在工作区，由编排器统一管理、人工 review 后再提交。"
        )
        cmd = ["codex", "exec", guarded,
               "--dangerously-bypass-approvals-and-sandbox",
               "--skip-git-repo-check"]
        if self.cfg.codex_model:
            cmd += ["-m", self.cfg.codex_model]
        for kv in self.cfg.codex_config:          # 如 model_reasoning_effort=medium
            cmd += ["-c", kv]
        # clean=True 剥离 codex 输出里的 ANSI 控制序列，让落盘日志干净
        r = run(cmd, cwd=self.cfg.repo, timeout=self.cfg.codex_timeout, clean=True)
        self.artifacts.write(f"{label}_codex_stdout.txt", r.stdout)
        self.artifacts.write(f"{label}_codex_stderr.txt", r.stderr)
        if r.returncode != 0:
            sys.exit(f"[codex] 调用失败: {r.stderr.strip()}")
        tail = r.stdout.strip().splitlines()[-8:]
        if tail:
            print("  ┄ Codex 输出（末尾）:")
            for line in tail:
                print("    " + line)


class JsonAgent:
    """在 LLM 之上强制结构化交接：解析失败就追加纠正提示重试，超出次数才放弃。"""

    def __init__(self, llm: LLM, artifacts, retries: int):
        self.llm = llm
        self.artifacts = artifacts
        self.retries = retries

    def ask(self, prompt: str, system: str, *, what: str) -> dict:
        last_raw = ""
        for attempt in range(self.retries + 1):
            p = prompt if attempt == 0 else (
                prompt + "\n\n⚠ 你上一次没有返回合法的 JSON 对象。"
                "现在请【只】输出一个 JSON 对象本身，不要任何解释、不要 markdown 代码块、不要 ```。"
            )
            last_raw = self.llm.ask_text(p, system)
            self.artifacts.write(f"{what}_attempt{attempt}.txt", last_raw)
            data = extract_json(last_raw)
            if data is not None:
                return data
            if attempt < self.retries:
                print(f"  ⚠ {what} 未返回合法 JSON，重试 {attempt + 1}/{self.retries}…")
        sys.exit(f"[{what}] 多次尝试后仍未获得合法 JSON，最后一次原始回复见日志:\n{last_raw[:500]}")
