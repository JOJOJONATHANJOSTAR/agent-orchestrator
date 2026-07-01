"""评审上下文瘦身：把喂给 Claude 评审的 diff / 门日志裁剪到预算内。

订阅额度扛不住长上下文的评审——大 diff 要么超单请求上限、要么烧穿额度窗口，逼用户关评审、
退化成「Codex 单干」。这里在**送进评审前**把上下文压小：降噪（丢 lockfile/生成物/二进制）、
按**验收相关性**排序保留、超预算的文件只留统计。评审的职责是对照验收标准 + 确认门，本就不需要
逐行读完全部改动，所以裁剪对评审质量损失极小、对上下文收益极大。裁剪对所有通道都有益（省钱省时），
不止救订阅。
"""
from __future__ import annotations

import re

# 低价值/噪声文件：评审基本不需要逐行看，优先丢弃或只留统计
_NOISE = re.compile(
    r"(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Cargo\.lock|composer\.lock|"
    r"\.min\.(js|css)|\.map$|\.(png|jpe?g|gif|ico|pdf|woff2?|ttf)$|"
    r"(^|/)(dist|build|node_modules|__pycache__|\.next|out|vendor)/)",
    re.I,
)


def _split_files(diff: str) -> list[tuple[str, str]]:
    """把 unified diff 切成 ``[(路径, 该文件的 diff 段)]``。"""
    parts: list[tuple[str, str]] = []
    cur_path: str | None = None
    cur: list[str] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if cur_path is not None:
                parts.append((cur_path, "".join(cur)))
            cur = [line]
            m = re.search(r" b/(.+?)\s*$", line)
            cur_path = m.group(1) if m else "?"
        else:
            cur.append(line)
    if cur_path is not None:
        parts.append((cur_path, "".join(cur)))
    return parts


def _relevance(path: str, acceptance_text: str) -> int:
    """路径与验收标准的相关度：命中文件名/词元越多越高（优先保留）。"""
    base = path.lower().rsplit("/", 1)[-1]
    score = 3 if base and base in acceptance_text else 0
    for tok in re.split(r"[/_.\-]", base):
        if len(tok) >= 3 and tok in acceptance_text:
            score += 1
    return score


def _stat(seg: str) -> str:
    adds = sum(1 for ln in seg.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
    dels = sum(1 for ln in seg.splitlines() if ln.startswith("-") and not ln.startswith("---"))
    return f"+{adds} -{dels}"


def trim_for_review(diff: str, acceptance: list, budget: int) -> str:
    """把 diff 裁剪到 ``budget`` 字符内。``budget<=0`` 或已够小则原样返回。

    策略：降噪文件靠后 → 按（相关性高、体积小）优先 → 预算内保留全文、其余只留统计行。
    若连最相关的单个文件都超预算，则截断该文件以保证评审至少看到点真实改动。

    Args:
        diff (str): 完整 unified diff。
        acceptance (list): 验收标准（用于按相关性排序保留）。
        budget (int): 字符预算；<=0 不裁剪。

    Returns:
        str: 裁剪后的 diff（含裁剪说明横幅）。
    """
    if budget <= 0 or len(diff) <= budget:
        return diff
    files = _split_files(diff)
    if not files:
        return diff[:budget] + f"\n\n[评审上下文已截断到 {budget} 字符，完整 diff 见日志]"

    acc = " ".join(str(a) for a in acceptance).lower()
    files.sort(key=lambda fp: (1 if _NOISE.search(fp[0]) else 0,
                               -_relevance(fp[0], acc), len(fp[1])))
    kept: list[str] = []
    stat_only: list[str] = []
    used = 0
    for path, seg in files:
        if not _NOISE.search(path) and used + len(seg) <= budget:
            kept.append(seg)
            used += len(seg)
        else:
            stat_only.append(f"  {path} | {_stat(seg)}（已省略）")
    if not kept:  # 连最相关的单文件都超预算：截断它，保证评审看到真实改动
        path, seg = files[0]
        kept.append(seg[:budget] + f"\n[文件 {path} 过大，diff 已截断]\n")
        stat_only = [s for s in stat_only if not s.startswith(f"  {path} ")]

    out = "".join(kept)
    if stat_only:
        out += (f"\n\n[评审上下文已裁剪：完整 {len(files)} 文件，此处保留 {len(kept)} 文件全文，"
                f"其余仅列统计（噪声/超预算）。完整 diff 见 runs/…/*.diff]\n"
                + "\n".join(stat_only))
    return out


def trim_gate_log(block: str, cap: int = 4000) -> str:
    """门详情块裁剪：超 ``cap`` 则保留头尾（评审要的是过没过 + 关键报错，不是全量输出）。"""
    if len(block) <= cap:
        return block
    head = block[: cap * 2 // 3]
    tail = block[-(cap // 3):]
    return f"{head}\n…[门日志已裁剪，省略约 {len(block) - cap} 字符]…\n{tail}"
