"""适配层：与外部 agent（Claude / Codex）的进程通信，以及强制 JSON 的封装。

定义三个接口（Protocol），engine/planner 只依赖接口，便于 dry-run 用 fakes 替换：
  - LLM.ask_text(prompt, system) -> str     大脑（Claude）
  - Coder.implement(prompt, label) -> None   双手（Codex）
  - Gates.run() -> (bool, list[dict])        验收门（见 gates.GateRunner）
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Protocol

from .metrics import parse_codex_tokens
from .process import run
from .util import extract_json

# 宿主托管会话注入的、会让 headless claude 误用宿主路由/登录态的变量
_HOST_PREFIX = "CLAUDE_CODE_"
_HOST_EXACT = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")
# 一次性配置凭据的默认文件（可用 CCO_AUTH_FILE 覆盖路径）
_AUTH_FILE = "~/.claude_codex_orchestrator.env"

# 两条鉴权通道各自的凭据环境变量名
_API_KEY_VAR = "ANTHROPIC_API_KEY"            # api 通道：按 API 计费
_OAUTH_VAR = "CLAUDE_CODE_OAUTH_TOKEN"        # subscription 通道：吃订阅额度（claude setup-token 产物）
_DEFAULT_CHANNEL_VAR = "CCO_DEFAULT_CHANNEL"  # auto 时的默认通道偏好（subscription / api）

# channel 名 -> (凭据环境变量名, 人类可读名)
_CHANNELS: dict[str, tuple[str, str]] = {
    "subscription": (_OAUTH_VAR, "订阅额度（CLAUDE_CODE_OAUTH_TOKEN）"),
    "api": (_API_KEY_VAR, "API key（ANTHROPIC_API_KEY）"),
}


def _read_config_file() -> dict[str, str]:
    """读取一次性配置文件里的所有 `KEY=VALUE`（忽略空行/#注释，去引号）。读不到返回 {}。"""
    raw = os.environ.get("CCO_AUTH_FILE")
    path = Path(raw).expanduser() if raw else Path(_AUTH_FILE).expanduser()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip().strip('"').strip("'")
        if k.strip() and v:
            out[k.strip()] = v
    return out


def _available_channels(cfg: dict[str, str]) -> dict[str, str]:
    """返回当前可用的「通道 -> 凭据值」：每条通道的凭据取「环境变量优先，其次配置文件」。"""
    out: dict[str, str] = {}
    for ch, (var, _name) in _CHANNELS.items():
        val = os.environ.get(var) or cfg.get(var)
        if val:
            out[ch] = val
    return out


def _pick_channel(channel: str, available: dict[str, str], cfg: dict[str, str]) -> str | None:
    """据请求的 channel 与可用通道决定最终通道。

    - 显式指定 subscription/api：可用则用之；不可用则 fail-fast（不静默回落）。
    - auto：按 CCO_DEFAULT_CHANNEL → 唯一可用 → 二者皆有时优先订阅；都没配则返回 None。
    """
    if channel in _CHANNELS:
        if channel in available:
            return channel
        _fail_channel_unavailable(channel)
    if not available:
        return None
    pref = (cfg.get(_DEFAULT_CHANNEL_VAR) or os.environ.get(_DEFAULT_CHANNEL_VAR) or "").strip()
    if pref in available:
        return pref
    if len(available) == 1:
        return next(iter(available))
    return "subscription"  # 两条都可用、无明确默认：优先订阅，贴合 skill 直觉


def _fail_channel_unavailable(channel: str) -> None:
    """显式请求的通道缺凭据时，清晰报错并退出（避免漏到宿主网关再撞含糊的 'Not logged in'）。"""
    var = _CHANNELS[channel][0]
    if channel == "subscription":
        how = (f"在普通终端跑 `claude setup-token`，把输出的 token 写入 {_AUTH_FILE}："
               f"{_OAUTH_VAR}=...")
        other = "api"
    else:
        how = (f"写入 {_AUTH_FILE}：{_API_KEY_VAR}=sk-ant-...（或 setx 同名环境变量）")
        other = "subscription"
    sys.exit(
        f"[auth] 指定的鉴权通道 '{channel}' 不可用：未找到 {var}。\n"
        f"  配置方法：{how}\n"
        f"  或改用另一条通道：--auth-channel {other}（若已配置）。"
    )


def prepare_agent_auth(channel: str = "auto") -> None:
    """托管子会话里为子进程配置独立鉴权，支持「订阅额度 / API key」两条通道、每次可选。

    托管会话（存在 `CLAUDE_CODE_*`）中，宿主登录 token 运行时注入、不落地为 CLI 可读凭据，
    于是子进程 `claude -p` 报 'Not logged in'。这里据 channel 选定一条通道的凭据：
      - ``subscription``：用 ``CLAUDE_CODE_OAUTH_TOKEN``（`claude setup-token` 产物），走订阅额度；
      - ``api``：用 ``ANTHROPIC_API_KEY``，按 API 计费；
      - ``auto``：按 ``CCO_DEFAULT_CHANNEL`` → 唯一可用 → 二者皆有时优先订阅。
    凭据可来自环境变量或一次性配置文件 ``~/.claude_codex_orchestrator.env``。

    选定后**先剥离**宿主会话变量（`CLAUDE_CODE_*` / `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN`），
    **再注入**该通道凭据——注入必须在剥离之后，否则同样以 ``CLAUDE_CODE_`` 开头的 OAuth token
    会被剥离循环一并删掉。两套凭据互斥注入，避免 claude 选错。

    显式指定的通道不可用时 fail-fast（见 `_fail_channel_unavailable`）。非托管会话、或 auto 下
    什么都没配时为 no-op（后者交由后续失败时的 `_auth_hint` 指引）。只改本进程的 os.environ
    （子进程继承），不影响宿主。

    Args:
        channel (str): ``auto`` | ``subscription`` | ``api``。

    Returns:
        None
    """
    if not any(k.startswith(_HOST_PREFIX) for k in os.environ):
        return
    cfg = _read_config_file()
    available = _available_channels(cfg)
    chosen = _pick_channel(channel, available, cfg)
    if chosen is None:
        return  # auto 且未配置任何通道：优雅 no-op，交由 _auth_hint 指引

    var, name = _CHANNELS[chosen]
    value = available[chosen]
    # 顺序关键：先剥离宿主注入，再注入凭据（OAuth token 也以 CLAUDE_CODE_ 开头，否则会被一并删除）
    for k in [k for k in os.environ if k.startswith(_HOST_PREFIX)]:
        del os.environ[k]
    for k in _HOST_EXACT:
        os.environ.pop(k, None)
    os.environ.pop(_OAUTH_VAR if var == _API_KEY_VAR else _API_KEY_VAR, None)  # 互斥：清掉另一通道凭据
    os.environ[var] = value
    print(f"  ℹ 检测到托管子会话：已用「{name}」为子进程配置鉴权。")


def _auth_hint() -> str:
    """headless `claude` 鉴权失败时的修复引导。

    托管子会话（Claude Code 内部启动的子进程）里，token 是运行时注入的，不以独立 CLI
    可读的凭据形式存在，于是裸跑 `claude -p` 会撞 'Not logged in'。检测到这种环境就提示
    如何一次性配置两条通道之一（之后由 prepare_agent_auth 自动生效）。

    Returns:
        str: 多行修复建议文本；据环境是否含 CLAUDE_CODE_* 给出托管/普通两种引导。
    """
    managed = any(k.startswith(_HOST_PREFIX) for k in os.environ)
    lines = ["", "  ↳ 修复建议："]
    if managed:
        lines += [
            "    检测到托管子会话：headless claude 无法复用宿主登录态，需配置一条独立鉴权通道。",
            f"    一次性写入 {_AUTH_FILE} 即可（之后每次自动生效）：",
            "      • 订阅额度（不走 API 计费）：先 `claude setup-token`，再写 "
            f"{_OAUTH_VAR}=...",
            f"      • API 计费：写 {_API_KEY_VAR}=sk-ant-...",
            f"    两条都配后，可用 --auth-channel subscription|api 每次任选，或用 "
            f"{_DEFAULT_CHANNEL_VAR}=... 设默认。",
        ]
    else:
        lines += ["    请在终端先执行 `claude /login` 完成登录，或设置 ANTHROPIC_API_KEY。"]
    return "\n".join(lines)


class LLM(Protocol):
    """大脑：与 Claude 进程通信，返回文本。

    Args:
        prompt (str): 用户侧提示（经 stdin 传入）。
        system (str): 追加的系统提示。

    Returns:
        str: 模型回复正文。
    """
    def ask_text(self, prompt: str, system: str) -> str: ...


class Coder(Protocol):
    """双手：与 Codex 进程通信，执行代码实现。

    Args:
        prompt (str): 给实现者的改动说明。
        label (str): 本轮标签（如 ``s1_round2``），用于日志产物命名。
    """
    def implement(self, prompt: str, label: str) -> None: ...


class ClaudeClient:
    """headless 调 Claude，只读工具，累计成本到 budget，并把 token/成本/耗时记入 ledger。"""

    def __init__(self, cfg, budget, ledger=None):
        self.cfg = cfg
        self.budget = budget
        self.ledger = ledger

    def ask_text(self, prompt: str, system: str) -> str:
        """headless 调用 Claude 并累计成本。

        Args:
            prompt (str): 用户侧提示，经 stdin 传入。
            system (str): 追加到默认系统提示之后的内容（--append-system-prompt）。

        Returns:
            str: Claude 返回 JSON 里的 ``result`` 正文。

        Raises:
            SystemExit: 进程非零退出或返回 is_error；鉴权类错误会附带 _auth_hint 引导。
        """
        # prompt 经 stdin 传入，不作命令行参数：评审时 prompt 含完整 diff，作为参数会撑爆
        # Windows 命令行长度上限（~32K）触发 WinError 206。stdin 无此限制，跨平台稳妥。
        cmd = [
            "claude", "-p",
            "--output-format", "json",
            "--append-system-prompt", system,
            "--allowedTools", "Read,Grep,Glob",   # 只读：写代码只交给 Codex
        ]
        if self.cfg.model:
            cmd += ["--model", self.cfg.model]
        t0 = time.perf_counter()
        r = run(cmd, cwd=self.cfg.repo, timeout=self.cfg.claude_timeout,
                input=prompt.encode("utf-8"))
        dt = time.perf_counter() - t0
        if r.returncode != 0:
            # claude 的实际错误（如无效模型 / API 报错）常在 stdout 的 JSON 里，stderr 可能为空
            detail = (r.stderr.strip() or r.stdout.strip())[:400] or "(无输出)"
            hint = _auth_hint() if "not logged in" in detail.lower() else ""
            sys.exit(f"[claude] 调用失败（rc={r.returncode}）: {detail}{hint}")
        data = json.loads(r.stdout)
        if data.get("is_error"):
            detail = str(data.get("result", ""))[:400]
            hint = _auth_hint() if "not logged in" in detail.lower() else ""
            sys.exit(f"[claude] 调用失败: {detail}{hint}")
        self.budget.add(data.get("total_cost_usd"))
        if self.ledger is not None:
            u = data.get("usage") or {}
            self.ledger.record(
                "claude", model=str(data.get("model") or self.cfg.model or ""),
                input_tokens=int(u.get("input_tokens") or 0),
                output_tokens=int(u.get("output_tokens") or 0),
                cache_read=int(u.get("cache_read_input_tokens") or 0),
                cache_create=int(u.get("cache_creation_input_tokens") or 0),
                cost_usd=float(data.get("total_cost_usd") or 0.0),
                duration_s=dt)
        return data["result"]


class CodexClient:
    """headless 调 Codex 实现代码。--full-access 跳过逐步确认（请确认在受控环境运行）。"""

    def __init__(self, cfg, artifacts, ledger=None):
        self.cfg = cfg
        self.artifacts = artifacts
        self.ledger = ledger

    def implement(self, prompt: str, label: str) -> None:
        """headless 调用 Codex 实现代码，stdout/stderr 落盘到 artifacts。

        Args:
            prompt (str): 给实现者的改动说明；内部会追加"只改文件、不自行提交、同类问题
                全仓修复"等执行约束后再传给 codex。
            label (str): 本轮标签，用于产物文件命名（``<label>_codex_stdout.txt`` 等）。

        Returns:
            None

        Raises:
            SystemExit: codex 进程非零退出。
        """
        # 非交互执行：跳过审批与沙箱（codex-cli ≥ 0.x 用此 flag，旧的 --full-access 已不存在）。
        # --skip-git-repo-check 让非 git 目录也能跑。请确认在受控环境运行。
        # 约束 Codex 只改文件、不自行提交——否则改动进了 commit，编排器的 diff/快照/回滚
        # 和"人工 review 后再提交"的安全模型都会被破坏。
        guarded = prompt + (
            "\n\n[执行约束] 只创建/修改文件来完成任务；不要运行 git add / commit / push / stash，"
            "也不要回滚或清理工作区——所有改动必须留在工作区，由编排器统一管理、人工 review 后再提交。"
            "\n[同类修复] 修某个缺陷时，用 grep/搜索把全仓库里的同类问题（相同的错误写法/反模式/"
            "失效路径）一并修掉，不要只改触发处的那一处。"
        )
        cmd = ["codex", "exec", guarded,
               "--dangerously-bypass-approvals-and-sandbox",
               "--skip-git-repo-check"]
        if self.cfg.codex_model:
            cmd += ["-m", self.cfg.codex_model]
        for kv in self.cfg.codex_config:          # 如 model_reasoning_effort=medium
            cmd += ["-c", kv]
        # clean=True 剥离 codex 输出里的 ANSI 控制序列，让落盘日志干净
        t0 = time.perf_counter()
        r = run(cmd, cwd=self.cfg.repo, timeout=self.cfg.codex_timeout, clean=True)
        dt = time.perf_counter() - t0
        self.artifacts.write(f"{label}_codex_stdout.txt", r.stdout)
        self.artifacts.write(f"{label}_codex_stderr.txt", r.stderr)
        if self.ledger is not None:
            # codex 不报 USD 成本（吃订阅额度）；token 从 stdout best-effort 解析，抓不到记 0
            tk = parse_codex_tokens(r.stdout)
            inp, out = tk["input"], tk["output"]
            if not inp and not out and tk["total"]:
                inp = tk["total"]  # 只拿到合计、无输入/输出拆分：整体计入输入层，至少不丢失
            self.ledger.record("codex", model=str(self.cfg.codex_model or ""),
                               input_tokens=inp, output_tokens=out, duration_s=dt)
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
        """向 LLM 提问并强制返回结构化 JSON：解析失败追加纠正提示重试。

        Args:
            prompt (str): 用户侧提示。
            system (str): 系统提示。
            what (str): 本次调用的语义标签（如 ``decompose`` / ``s1_round1_review``），
                用于日志产物命名与错误信息。

        Returns:
            dict | list: extract_json 解析出的结构化对象。

        Raises:
            SystemExit: 超过重试次数仍未拿到合法 JSON。
        """
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
