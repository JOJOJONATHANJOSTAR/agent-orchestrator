"""收尾报告：把 MetricsLedger 聚合成终端汇总表 + 零依赖自包含 HTML（内联 SVG）报告。

零依赖铁律：不引入 matplotlib / chart.js 等，纯 Python 拼 SVG。产出的 report.html 是单文件、
可离线打开、可直接分享。终端则打一张精简汇总表，跑完即看。空账本（如 --dry-run）下不崩溃。
"""
from __future__ import annotations

import html
from collections import OrderedDict, defaultdict

from .metrics import MetricsLedger

# 配色（token 分层 / 阶段 / agent / 门链）
_TOK_SERIES = [("输入", "input_tokens", "#4e79a7"), ("输出", "output_tokens", "#f28e2b"),
               ("缓存读", "cache_read", "#59a14f"), ("缓存写", "cache_create", "#76b7b2")]
_PHASE_COLOR = OrderedDict([("plan", "#b07aa1"), ("impl", "#4e79a7"),
                            ("gate", "#edc948"), ("review", "#e15759")])
_PHASE_CN = {"plan": "规划", "impl": "实现", "gate": "门链", "review": "评审"}


# ----------------------------------------------------------------------------- 格式化
def _fmt_tokens(n: int) -> str:
    """12345 → '12.3k'；小于 1000 原样。"""
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(int(n))


def _fmt_dur(s: float) -> str:
    """秒 → '1m 23s' / '45s'。"""
    s = int(round(s))
    return f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"


# ----------------------------------------------------------------------------- 聚合
def _phase_duration(ledger: MetricsLedger) -> "OrderedDict[str, float]":
    """各阶段耗时合计（保持 plan→impl→gate→review 顺序，0 的阶段也保留位）。"""
    out = OrderedDict((p, 0.0) for p in _PHASE_COLOR)
    for e in ledger.events:
        out[e.phase] = out.get(e.phase, 0.0) + e.duration_s
    return out


def _agent_tokens(ledger: MetricsLedger) -> "OrderedDict[str, dict]":
    """每个 agent 的分层 token 合计（claude / codex）。"""
    out: "OrderedDict[str, dict]" = OrderedDict()
    for e in ledger.events:
        if e.agent == "gate":
            continue
        d = out.setdefault(e.agent, {k: 0 for _, k, _ in _TOK_SERIES})
        for _, k, _ in _TOK_SERIES:
            d[k] += getattr(e, k)
    return out


def _round_rows(ledger: MetricsLedger) -> list[dict]:
    """按 (subtask, round) 聚合每轮的成本/token/耗时，按出现顺序返回。"""
    order: list[tuple] = []
    agg: dict = {}
    for e in ledger.events:
        key = (e.subtask, e.round)
        if key not in agg:
            agg[key] = {"label": f"{e.subtask or '规划'}·r{e.round}",
                        "cost": 0.0, "tokens": 0, "dur": 0.0}
            order.append(key)
        a = agg[key]
        a["cost"] += e.cost_usd
        a["tokens"] += e.input_tokens + e.output_tokens
        a["dur"] += e.duration_s
    return [agg[k] for k in order]


def _gate_grid(ledger: MetricsLedger) -> tuple[list[str], list[str], dict]:
    """门链通过网格：返回 (列=各轮标签, 行=门名, {(col,row): ok})。"""
    cols: list[str] = []
    rows: list[str] = []
    cells: dict = {}
    for e in ledger.events:
        if e.agent != "gate":
            continue
        col = f"{e.subtask or ''}·r{e.round}"
        if col not in cols:
            cols.append(col)
        if e.label not in rows:
            rows.append(e.label)
        cells[(col, e.label)] = e.ok
    return cols, rows, cells


# ----------------------------------------------------------------------------- 终端
def terminal_summary(ledger: MetricsLedger) -> str:
    """跑完打印的精简汇总：KPI 一行 + 阶段耗时表 + agent token 表 + ASCII 占比条。"""
    if not ledger.events:
        return "（无度量数据——dry-run 或未发生真实调用）"
    lines = ["===== 用量 / 性能报告 ====="]
    lines.append(
        f"  💳 成本 ${ledger.total_cost():.4f}（仅 claude）"
        f" | 🔤 token {_fmt_tokens(ledger.total_tokens())}"
        f" | ⏱ 计时调用耗时 {_fmt_dur(ledger.total_duration())}"
        f" | 调用 {len(ledger.events)} 次")

    pd = _phase_duration(ledger)
    tot = sum(pd.values()) or 1.0
    lines.append("  ─ 耗时按阶段 ─")
    for p, sec in pd.items():
        bar = "█" * int(round(20 * sec / tot))
        lines.append(f"    {_PHASE_CN[p]:<4} {bar:<20} {_fmt_dur(sec):>7} ({sec / tot * 100:4.0f}%)")

    at = _agent_tokens(ledger)
    if at:
        lines.append("  ─ token 按 agent ─")
        for ag, d in at.items():
            io = d["input_tokens"] + d["output_tokens"]
            lines.append(
                f"    {ag:<7} 输入 {_fmt_tokens(d['input_tokens']):>7}"
                f" 输出 {_fmt_tokens(d['output_tokens']):>7}"
                f" 缓存读 {_fmt_tokens(d['cache_read']):>7}"
                f" | 计费 {_fmt_tokens(io):>7}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- SVG 原语
def _vstack_svg(cats: list[str], stacks: list[list[int]], legend: list[tuple],
                *, vfmt=_fmt_tokens) -> str:
    """纵向堆叠柱状图（SVG）。cats=x 轴标签；stacks[i]=该类各层数值；legend=[(名,色)…]。"""
    if not cats:
        return '<p class="empty">无数据</p>'
    pad_l, pad_b, pad_t, plot_h = 8, 34, 12, 170
    bw, gap = 54, 26
    width = max(260, pad_l * 2 + len(cats) * (bw + gap))
    height = pad_t + plot_h + pad_b
    top = max((sum(s) for s in stacks), default=0) or 1
    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img">']
    for i, (cat, st) in enumerate(zip(cats, stacks)):
        x = pad_l + i * (bw + gap) + gap / 2
        y = pad_t + plot_h
        for (_, color), v in zip(legend, st):
            h = plot_h * v / top
            y -= h
            if h > 0.5:
                out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw}" height="{h:.1f}" '
                           f'fill="{color}"><title>{v}</title></rect>')
        total = sum(st)
        out.append(f'<text x="{x + bw / 2:.1f}" y="{pad_t + plot_h - (plot_h * total / top) - 4:.1f}" '
                   f'class="bar-val" text-anchor="middle">{vfmt(total)}</text>')
        out.append(f'<text x="{x + bw / 2:.1f}" y="{height - 14:.1f}" class="axis" '
                   f'text-anchor="middle">{html.escape(cat)}</text>')
    out.append("</svg>")
    return "".join(out)


def _hbar_svg(segments: list[tuple]) -> str:
    """单条横向分段条（用于「耗时按阶段」）。segments=[(名, 色, 值)…]。"""
    total = sum(v for _, _, v in segments) or 1.0
    width, h, y = 520, 30, 6
    out = [f'<svg viewBox="0 0 {width} 46" class="chart" role="img">']
    x = 0.0
    for name, color, v in segments:
        w = width * v / total
        if w > 0.5:
            out.append(f'<rect x="{x:.1f}" y="{y}" width="{w:.1f}" height="{h}" fill="{color}">'
                       f'<title>{_PHASE_CN.get(name, name)} {_fmt_dur(v)} ({v / total * 100:.0f}%)</title></rect>')
            if w > 42:
                out.append(f'<text x="{x + w / 2:.1f}" y="{y + h / 2 + 4:.1f}" class="seg-val" '
                           f'text-anchor="middle">{v / total * 100:.0f}%</text>')
        x += w
    out.append("</svg>")
    return "".join(out)


def _grid_svg(cols: list[str], rows: list[str], cells: dict) -> str:
    """门链通过网格：绿=过、红=未过、灰=该轮无此门。"""
    if not cols or not rows:
        return '<p class="empty">无门链数据</p>'
    cw, ch, lbl = 56, 30, 60
    width = lbl + len(cols) * cw + 8
    height = 22 + len(rows) * ch + 8
    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img">']
    for ci, c in enumerate(cols):
        out.append(f'<text x="{lbl + ci * cw + cw / 2:.1f}" y="16" class="axis" '
                   f'text-anchor="middle">{html.escape(c)}</text>')
    for ri, r in enumerate(rows):
        y = 22 + ri * ch
        out.append(f'<text x="{lbl - 6}" y="{y + ch / 2 + 4:.1f}" class="axis" '
                   f'text-anchor="end">{html.escape(r)}</text>')
        for ci, c in enumerate(cols):
            ok = cells.get((c, r))
            color = "#59a14f" if ok else ("#e15759" if ok is False else "#e5e7eb")
            x = lbl + ci * cw
            out.append(f'<rect x="{x + 4}" y="{y + 3}" width="{cw - 8}" height="{ch - 6}" rx="4" '
                       f'fill="{color}"><title>{c} / {r}: '
                       f'{"通过" if ok else "未过" if ok is False else "—"}</title></rect>')
    out.append("</svg>")
    return "".join(out)


def _legend(items: list[tuple]) -> str:
    """图例：[(名, 色)…]。"""
    sp = "".join(f'<span class="lg"><i style="background:{c}"></i>{html.escape(n)}</span>'
                 for n, c in items)
    return f'<div class="legend">{sp}</div>'


# ----------------------------------------------------------------------------- HTML
_CSS = """
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,"Microsoft YaHei",sans-serif;
  color:#1f2937;background:#f8fafc;padding:24px}
h1{font-size:20px;margin:0 0 4px}.sub{color:#6b7280;font-size:13px;margin-bottom:20px;
  word-break:break-all}
.kpis{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:20px}
.kpi{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:12px 16px;min-width:130px}
.kpi b{display:block;font-size:22px}.kpi span{color:#6b7280;font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:16px}
.card h2{font-size:14px;margin:0 0 12px;color:#374151}
.chart{width:100%;height:auto;overflow:visible}
.axis{fill:#6b7280;font-size:11px}.bar-val{fill:#374151;font-size:11px;font-weight:600}
.seg-val{fill:#fff;font-size:11px;font-weight:600}
.legend{display:flex;flex-wrap:wrap;gap:12px;margin-top:10px;font-size:12px;color:#4b5563}
.lg{display:inline-flex;align-items:center;gap:5px}
.lg i{width:11px;height:11px;border-radius:3px;display:inline-block}
.empty{color:#9ca3af;font-size:13px;padding:20px;text-align:center}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:7px 10px;text-align:right;border-bottom:1px solid #f1f5f9}
th:first-child,td:first-child{text-align:left}thead th{color:#6b7280;font-weight:600}
footer{color:#9ca3af;font-size:12px;margin-top:20px}
"""


def render_html(ledger: MetricsLedger, meta: dict) -> str:
    """渲染自包含 HTML 报告（内联 CSS+SVG，无外部依赖、可离线打开）。

    Args:
        ledger (MetricsLedger): 度量账本。
        meta (dict): 运行元信息，含 ``run_id`` / ``repo`` / ``task`` / ``status``。

    Returns:
        str: 完整 HTML 文档字符串。
    """
    esc = html.escape
    rows = _round_rows(ledger)
    at = _agent_tokens(ledger)
    pd = _phase_duration(ledger)

    # KPI 卡
    kpis = [
        ("成本 (USD)", f"${ledger.total_cost():.4f}", "仅 claude 上报"),
        ("Token", _fmt_tokens(ledger.total_tokens()), "输入+输出计费"),
        ("耗时", _fmt_dur(ledger.total_duration()), "计时调用合计"),
        ("调用次数", str(len(ledger.events)), "claude+codex+门"),
    ]
    kpi_html = "".join(f'<div class="kpi"><b>{esc(v)}</b><span>{esc(t)}<br>{esc(d)}</span></div>'
                       for t, v, d in kpis)

    # 图1：每轮 token（堆叠）
    cats = [r["label"] for r in rows]
    # 用 round_rows 顺序，但分层数据需从事件重聚合
    round_stack: dict = defaultdict(lambda: [0, 0, 0, 0])
    for e in ledger.events:
        key = f"{e.subtask or '规划'}·r{e.round}"
        for i, (_, k, _) in enumerate(_TOK_SERIES):
            round_stack[key][i] += getattr(e, k)
    stacks = [round_stack[c] for c in cats]
    tok_legend = [(n, c) for n, _, c in _TOK_SERIES]
    chart_round = _vstack_svg(cats, stacks, tok_legend) + _legend(tok_legend)

    # 图2：耗时按阶段
    chart_phase = _hbar_svg([(p, _PHASE_COLOR[p], sec) for p, sec in pd.items() if sec > 0]) \
        + _legend([(_PHASE_CN[p], _PHASE_COLOR[p]) for p in pd])

    # 图3：token 按 agent
    a_cats = list(at.keys())
    a_stacks = [[at[a][k] for _, k, _ in _TOK_SERIES] for a in a_cats]
    chart_agent = _vstack_svg(a_cats, a_stacks, tok_legend) + _legend(tok_legend) \
        if a_cats else '<p class="empty">无 token 数据</p>'

    # 图4：门链通过网格
    cols, grows, cells = _gate_grid(ledger)
    chart_gate = _grid_svg(cols, grows, cells) + \
        _legend([("通过", "#59a14f"), ("未过", "#e15759"), ("无", "#e5e7eb")])

    # 明细表
    body_rows = "".join(
        f"<tr><td>{esc(r['label'])}</td><td>${r['cost']:.4f}</td>"
        f"<td>{_fmt_tokens(r['tokens'])}</td><td>{_fmt_dur(r['dur'])}</td></tr>"
        for r in rows) or '<tr><td colspan="4" class="empty">无</td></tr>'

    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>编排器报告 {esc(meta.get('run_id', ''))}</title><style>{_CSS}</style></head><body>
<h1>用量 / 性能报告</h1>
<div class="sub">运行 {esc(meta.get('run_id', ''))} · {esc(meta.get('status', ''))}<br>
仓库 {esc(meta.get('repo', ''))}<br>任务：{esc(meta.get('task', ''))}</div>
<div class="kpis">{kpi_html}</div>
<div class="grid">
  <div class="card"><h2>每轮 token（输入/输出/缓存）</h2>{chart_round}</div>
  <div class="card"><h2>耗时按阶段</h2>{chart_phase}</div>
  <div class="card"><h2>token 按 agent</h2>{chart_agent}</div>
  <div class="card"><h2>各轮门链通过</h2>{chart_gate}</div>
</div>
<div class="card" style="margin-top:16px"><h2>每轮明细</h2>
<table><thead><tr><th>轮</th><th>成本</th><th>token</th><th>耗时</th></tr></thead>
<tbody>{body_rows}</tbody></table></div>
<footer>由 agent-orchestrator 生成 · 成本仅 claude 上报，codex token 为 best-effort 解析</footer>
</body></html>"""
