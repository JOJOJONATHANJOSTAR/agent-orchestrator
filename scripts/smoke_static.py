#!/usr/bin/env python3
"""smoke_static.py —— 静态站构建产物的零依赖「冒烟门」。

专门捕捉 build 能过、但页面运行时会坏的那一类 bug：HTML/CSS 引用了一个**实际不存在**
的本地资源（典型如占位图的裸路径打包器没 emit → 运行时 404）。做法：

  1. 遍历目录下所有 .html，提取本地资源引用
     （img/script/link/source/video/audio 的 src/href/srcset/poster + 内联 style 的 url()）；
  2. 对引用到的本地 .css 文件，再解析其 url() 引用（相对该 css 解析）；
  3. 校验每个引用都能解析到真实存在的文件。全部存在 → 退出码 0；有断链 → 1 并清晰列出。

外链（http(s)://、//、data:、mailto:、tel:）与页内锚点（#…）、javascript: 自动跳过。
只用标准库，跨平台。可作为编排器的一个验收门：
    --gate smoke="python scripts/smoke_static.py dist"

用法：
    python smoke_static.py <目录>              # 如 dist/
    python smoke_static.py <目录> --base <根>  # 根绝对引用（/x.js）相对此根解析，默认 = <目录>
"""
from __future__ import annotations

import argparse
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

# 携带本地资源引用的标签 → 关注的属性
_ASSET_ATTRS = {
    "img": ("src", "srcset"),
    "script": ("src",),
    "link": ("href",),
    "source": ("src", "srcset"),
    "video": ("src", "poster"),
    "audio": ("src",),
}
_URL_FUNC = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)
_EXTERNAL = ("http://", "https://", "//", "data:", "mailto:", "tel:", "javascript:")


class _AssetParser(HTMLParser):
    """收集资源引用与内联 style 的 url()。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.refs: list[str] = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        for attr in _ASSET_ATTRS.get(tag, ()):
            val = d.get(attr)
            if not val:
                continue
            if attr == "srcset":
                self.refs += _parse_srcset(val)
            else:
                self.refs.append(val)
        style = d.get("style")
        if style:
            self.refs += [m.group(2) for m in _URL_FUNC.finditer(style)]


def _parse_srcset(value: str) -> list[str]:
    """srcset="a.png 1x, b.png 2x" → ['a.png', 'b.png']（取每项的 URL 部分）。"""
    out = []
    for part in value.split(","):
        token = part.strip().split()
        if token:
            out.append(token[0])
    return out


def _is_local(ref: str) -> bool:
    ref = ref.strip()
    return bool(ref) and not ref.startswith("#") and not ref.lower().startswith(_EXTERNAL)


def _resolve(ref: str, doc: Path, root: Path) -> Path:
    """把一个引用解析成磁盘路径：根绝对(/..)相对 root，其余相对文档所在目录。去掉 ?查询/#锚点。"""
    path = unquote(urlsplit(ref).path)
    if path.startswith("/"):
        return (root / path.lstrip("/")).resolve()
    return (doc.parent / path).resolve()


def _css_urls(css: Path) -> list[str]:
    try:
        text = css.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [m.group(2) for m in _URL_FUNC.finditer(text)]


def check(target: str, base: str | None = None) -> list[tuple[str, str]]:
    """校验 target 目录下所有 .html 的本地资源引用。返回断链列表 [(来源文件, 引用)]。"""
    root = Path(base or target).resolve()
    target_dir = Path(target).resolve()
    broken: list[tuple[str, str]] = []
    seen_css: set[Path] = set()

    def verify(ref: str, doc: Path):
        if not _is_local(ref):
            return
        resolved = _resolve(ref, doc, root)
        if not resolved.is_file():
            broken.append((str(doc.relative_to(target_dir)), ref))
        elif resolved.suffix.lower() == ".css" and resolved not in seen_css:
            seen_css.add(resolved)
            for u in _css_urls(resolved):
                if _is_local(u) and not _resolve(u, resolved, root).is_file():
                    broken.append((str(resolved.relative_to(target_dir)), u))

    html_files = sorted(target_dir.rglob("*.html"))
    for doc in html_files:
        p = _AssetParser()
        p.feed(doc.read_text(encoding="utf-8", errors="replace"))
        for ref in p.refs:
            verify(ref, doc)
    return broken


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="静态站构建产物的零依赖冒烟门：校验本地资源引用是否都存在")
    ap.add_argument("dir", help="要检查的目录（如构建产物 dist/）")
    ap.add_argument("--base", default=None, help="根绝对引用(/x.js)的解析根，默认 = dir")
    args = ap.parse_args(argv)

    target = Path(args.dir)
    if not target.is_dir():
        print(f"[smoke] 目录不存在：{args.dir}")
        return 1

    broken = check(args.dir, args.base)
    n_html = len(sorted(target.resolve().rglob("*.html")))
    if not broken:
        print(f"[smoke] OK：{n_html} 个 HTML 的本地资源引用全部存在。")
        return 0

    print(f"[smoke] 发现 {len(broken)} 处断链（构建能过但运行时会 404）：")
    for src, ref in broken:
        print(f"  ✗ {src}  →  {ref}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
