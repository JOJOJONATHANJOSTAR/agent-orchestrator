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


def _resolve_channel(channel: str, available: dict[str, str],
                     cfg: dict[str, str]) -> tuple[str | None, str]:
    """据请求的 channel 与可用通道**纯函数**地决定最终通道（不退出、不注入）。

    供 prepare_agent_auth（真实注入）与 describe_auth（--check-auth 预检）共用同一套解析，
    避免两处逻辑漂移。

    Returns:
        tuple[str | None, str]: (选定通道或 None, 原因码/说明)。原因码：``explicit-unavailable``
        （显式指定但缺凭据，调用方应 fail-fast）、``none-configured``（auto 且全未配），其余为人读说明。
    """
    if channel in _CHANNELS:
        if channel in available:
            return channel, f"显式指定 {channel}"
        return None, "explicit-unavailable"
    if not available:
        return None, "none-configured"
    pref = (cfg.get(_DEFAULT_CHANNEL_VAR) or os.environ.get(_DEFAULT_CHANNEL_VAR) or "").strip()
    if pref in available:
        return pref, f"auto → 默认通道 CCO_DEFAULT_CHANNEL={pref}"
    if len(available) == 1:
        return next(iter(available)), "auto → 唯一可用通道"
    return "subscription", "auto → 两条皆可用，优先订阅"  # 贴合 skill 直觉


def _fail_channel_unavailable(channel: str) -> None:
    """显式请求的通道缺凭据时，清晰报错并退出（避免漏到宿主网关再撞含糊的 'Not logged in'）。"""
    var = _CHANNELS[channel][0]
    other = "api" if channel == "subscription" else "subscription"
    pre = ("订阅通道需先在普通终端跑 `claude setup-token` 拿到 token。\n  "
           if channel == "subscription" else "")
    sys.exit(
        f"[auth] 指定的鉴权通道 '{channel}' 不可用：未找到 {var}。\n"
        f"  配置方法（推荐，凭据只留本机、你自己一条命令）：{pre}"
        f"在普通终端跑 `agent-orchestrate --setup-auth`，按提示隐藏输入凭据。\n"
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
    chosen, reason = _resolve_channel(channel, available, cfg)
    if chosen is None:
        if reason == "explicit-unavailable":
            _fail_channel_unavailable(channel)  # 显式指定却缺凭据：fail-fast
        return  # none-configured：auto 且全未配，优雅 no-op，交由 _auth_hint 指引

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


def _auth_file_path() -> Path:
    raw = os.environ.get("CCO_AUTH_FILE")
    return Path(raw).expanduser() if raw else Path(_AUTH_FILE).expanduser()


def setup_auth() -> None:
    """交互式凭据配置向导——由【用户本人】在普通终端运行（`--setup-auth`）。

    重要：助手不应代跑本向导、也不应替用户把密钥写进文件——帮用户落地密钥属于隐私敏感操作，
    别的用户的 Claude 会（应当）拒绝。本向导让用户自己一条命令搞定：用 getpass **隐藏输入**
    （密钥不回显、不进 shell 历史、不进对话/日志），与现有配置合并后写入本机文件并收紧权限。

    仅在「托管子会话」（在 Claude Code 里跑本 skill、复用不了宿主登录）时需要；普通终端里
    直接 `claude /login` 即可，无需配置。
    """
    import getpass

    path = _auth_file_path()
    existing = _read_config_file()   # 合并写：不清掉已配好的另一条通道

    print("===== 凭据配置向导（--setup-auth）=====")
    print(f"将写入本机文件：{path}")
    print("· 凭据只留在你本机；输入用 getpass 隐藏（不回显 / 不进 shell 历史 / 不进日志）。")
    print("· 仅「在 Claude Code 里跑本 skill」时需要；普通终端 `claude /login` 即可，无需配。\n")
    print("要配置哪条鉴权通道？")
    print("  1) 订阅额度  CLAUDE_CODE_OAUTH_TOKEN（来自 `claude setup-token`，吃订阅额度）")
    print("  2) API key   ANTHROPIC_API_KEY（按 API 计费）")
    print("  3) 两条都配")
    choice = (input("选择 1 / 2 / 3（回车默认 1）：").strip() or "1")

    updated = dict(existing)
    if choice in ("1", "3"):
        print("\n[订阅] 请先在另一个普通终端运行 `claude setup-token`，")
        print("       再把它输出的 token 粘到这里（输入时不显示）：")
        tok = getpass.getpass("  CLAUDE_CODE_OAUTH_TOKEN = ").strip()
        if tok:
            updated[_OAUTH_VAR] = tok
        else:
            print("  （空输入，跳过订阅通道）")
    if choice in ("2", "3"):
        key = getpass.getpass("  ANTHROPIC_API_KEY = ").strip()
        if key:
            updated[_API_KEY_VAR] = key
        else:
            print("  （空输入，跳过 API 通道）")

    if not (updated.get(_OAUTH_VAR) or updated.get(_API_KEY_VAR)):
        sys.exit("未输入任何凭据，未写入文件。")

    if updated.get(_OAUTH_VAR) and updated.get(_API_KEY_VAR):
        d = input("两条都配了，默认用哪条？subscription / api（回车默认 subscription）：").strip()
        updated[_DEFAULT_CHANNEL_VAR] = "api" if d == "api" else "subscription"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{k}={v}\n" for k, v in updated.items()), encoding="utf-8")
    try:
        os.chmod(path, 0o600)   # POSIX：仅本人可读写
    except OSError:
        pass
    if os.name == "nt":         # Windows：用 icacls 收紧到当前用户（best-effort）
        user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
        if user:
            run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"])

    configured = [c for c in ("subscription", "api") if updated.get(_CHANNELS[c][0])]
    print(f"\n✅ 已写入 {path}（权限已收紧到仅本人）。已配置通道：{', '.join(configured)}")
    print("   验证：agent-orchestrate --check-auth")


def _mask(val: str) -> str:
    """脱敏展示凭据：只露开头几位 + 长度，不打印完整密钥。"""
    v = val.strip()
    return f"{v[:6]}…，长度 {len(v)}" if len(v) > 8 else f"（长度 {len(v)}）"


def describe_auth(channel: str = "auto") -> str:
    """鉴权预检（`--check-auth` 用）：复用 `_resolve_channel` 的真实解析，**只报告不注入、脱敏**。

    回答「这次会用哪条通道、凭据从哪来、有没有配」，避免靠手搓 grep 误判——尤其别再把「进程环境
    变量为空」当成「没配置」（凭据是放配置文件里、由 prepare_agent_auth 直接读，本就不进 shell 环境）。

    Args:
        channel (str): 拟用的 ``--auth-channel``（auto / subscription / api）。

    Returns:
        str: 多行预检报告。
    """
    managed = any(k.startswith(_HOST_PREFIX) for k in os.environ)
    cfg = _read_config_file()
    available = _available_channels(cfg)
    raw = os.environ.get("CCO_AUTH_FILE")
    path = Path(raw).expanduser() if raw else Path(_AUTH_FILE).expanduser()

    out = ["===== 鉴权预检（--check-auth）====="]
    out.append(f"会话类型：{'托管子会话（需独立鉴权）' if managed else '普通会话（claude 用本机登录态即可）'}")
    out.append(f"配置文件：{path}（{'存在' if path.is_file() else '不存在'}）")
    out.append(f"请求通道：--auth-channel {channel}")
    out.append("各通道凭据：")
    for ch, (var, _name) in _CHANNELS.items():
        src = "环境变量" if os.environ.get(var) else ("配置文件" if cfg.get(var) else None)
        if src:
            out.append(f"  ✓ {ch:<12}（{var}）已配置 · 来源 {src} · {_mask(available[ch])}")
        else:
            out.append(f"  ✗ {ch:<12}（{var}）未配置")

    chosen, reason = _resolve_channel(channel, available, cfg)
    if chosen is not None:
        verdict = ("将使用：" + chosen + f"（{reason}）"
                   + ("" if managed else "；但非托管会话其实无需注入，claude 用本机登录态即可"))
        out.append("结论：✅ " + verdict)
    elif reason == "explicit-unavailable":
        out.append(f"结论：❌ 显式指定的 '{channel}' 缺凭据，真实运行会 fail-fast。"
                   + _auth_hint())
    else:  # none-configured
        if managed:
            out.append("结论：❌ 托管子会话但未配置任何通道，真实运行 claude -p 会 'Not logged in'。"
                       + _auth_hint())
        else:
            out.append("结论：✅ 未配置独立通道，但本会话非托管，claude 用本机登录态即可，无需配置。")
    return "\n".join(out)


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
            "    ✅ 推荐（凭据只留本机、你自己一条命令、助手不接触密钥）：",
            "       在普通终端跑  agent-orchestrate --setup-auth  ——向导会隐藏输入 token/key、",
            "       写入本机文件并收紧权限。订阅通道会提示你先跑 `claude setup-token`。",
            "    两条都配后，可用 --auth-channel subscription|api 每次任选，或在向导里设默认。",
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

    def __init__(self, cfg, budget, ledger=None, model=None):
        self.cfg = cfg
        self.budget = budget
        self.ledger = ledger
        self.model = model or cfg.model   # 可覆盖（如评审专用的省额度小模型）

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
        if self.model:
            cmd += ["--model", self.model]
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
                "claude", model=str(data.get("model") or self.model or ""),
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
            # codex 不报 USD 成本（吃订阅额度）；token 合计 best-effort 解析，抓不到记 0。
            # 注意：codex exec 把「tokens used\n<数字>」打到 stderr 而非 stdout，故两者合并解析。
            # codex 无 input/output 拆分，合计整体计入输入层（codex 改代码绝大头本就是上下文输入）。
            total = parse_codex_tokens(r.stdout + "\n" + r.stderr)["total"]
            self.ledger.record("codex", model=str(self.cfg.codex_model or ""),
                               input_tokens=total, duration_s=dt)
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
