"""design_review_html — turn a design-review .md into a self-contained .html.

Critic writes its proposal as markdown (easy to author, diff, grep). This
converter renders that markdown into a styled, **self-contained** HTML page
so a human can actually review it: the screenshots listed in the front
matter `shots:` are inlined as base64 (no broken relative paths, portable,
commit-able even though runtime/ is gitignored), impact tags become colored
badges, and findings render as impact-coded cards (via CSS `:has()`, no
brittle HTML parsing).

Usage
-----
    python -m agent_takkub.design_review_html docs/design-review/<file>.md
    → writes docs/design-review/<file>.html, prints the path

Importable: `render(md_path) -> Path`.
"""

from __future__ import annotations

import base64
import html as html_lib
import mimetypes
import os
import pathlib
import re
import sys
from html.parser import HTMLParser

import markdown
import yaml

# ── theme: matches the cockpit (static/terminal.html palette) ──────────────
_CSS = """
:root{
  --bg:#0e0e10;--panel:#18181b;--panel2:#1f1f23;--border:#27272a;
  --fg:#e4e4e7;--muted:#a1a1aa;--dim:#71717a;
  --high:#ef4444;--med:#f59e0b;--low:#52525b;--keep:#22c55e;--gold:#f5c542;
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--fg);
  font:14px/1.6 "Segoe UI","Leelawadee UI",system-ui,sans-serif}
a{color:#60a5fa;text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:980px;margin:0 auto;padding:28px 22px 80px}
h1{font-size:22px;margin:0 0 6px;border-bottom:1px solid var(--border);padding-bottom:16px}
h2{font-size:17px;margin:30px 0 14px;padding-bottom:8px;border-bottom:1px solid var(--border)}
h3{font-size:15px;margin:18px 0 8px}
p{color:var(--muted)}
code{background:var(--panel2);border:1px solid var(--border);border-radius:4px;
  padding:1px 5px;font:12.5px "Cascadia Mono","Consolas",monospace}
pre{background:var(--panel2);border:1px solid var(--border);border-radius:8px;
  padding:12px 14px;overflow:auto}
pre code{border:0;background:none;padding:0}
strong{color:var(--fg)}
blockquote{margin:18px 0;padding:12px 16px;border-radius:10px;
  background:rgba(245,158,11,.08);border:1px solid rgba(245,158,11,.3);color:#fcd34d}
blockquote p{color:#fcd34d;margin:4px 0}
ul,ol{padding-left:0}
li{list-style:none;background:var(--panel);border:1px solid var(--border);
  border-left:4px solid var(--low);border-radius:10px;padding:11px 15px;margin:0 0 10px;color:var(--muted)}
li strong:first-child{color:var(--fg)}
li ul,li ol{margin-top:8px;padding-left:0}
li li{background:var(--panel2);margin:6px 0;padding:7px 12px}
li:has(.badge.high){border-left-color:var(--high)}
li:has(.badge.med){border-left-color:var(--med)}
li:has(.badge.low){border-left-color:var(--low)}
ol.steps-numbered li{counter-increment:none}
.badge{font-size:10.5px;font-weight:700;letter-spacing:.04em;padding:2px 8px;
  border-radius:5px;text-transform:uppercase;white-space:nowrap}
.badge.high{background:var(--high);color:#fff}
.badge.med{background:var(--med);color:#3a2a00}
.badge.low{background:var(--low);color:#fff}
.meta{color:var(--muted);font-size:13px;margin:-4px 0 4px}
figure{margin:22px 0 26px;border:1px solid var(--border);border-radius:12px;overflow:hidden;background:#000}
figure img{display:block;width:100%;height:auto}
figcaption{padding:9px 14px;color:var(--muted);font-size:12px;background:var(--panel)}
.missing-shot{padding:10px 14px;color:#fca5a5;background:rgba(239,68,68,.08);
  border:1px solid rgba(239,68,68,.3);border-radius:8px;margin:10px 0;font-size:13px}
footer{margin-top:40px;padding-top:16px;border-top:1px solid var(--border);
  color:var(--dim);font-size:12px}
"""

# *impact: high* (rendered by markdown as <em>) or bare "impact: high"
_IMPACT_EM = re.compile(r"<em>\s*impact:\s*(high|med|medium|low)\s*</em>", re.IGNORECASE)
_IMPACT_BARE = re.compile(r"\bimpact:\s*(high|med|medium|low)\b", re.IGNORECASE)

_BLOCKED_TAGS = frozenset({"script", "iframe"})


def _unsafe_url(value: str) -> bool:
    """Return True for browser-executable URLs after entity/control folding."""
    folded = re.sub(r"[\x00-\x20]+", "", html_lib.unescape(value)).lower()
    return folded.startswith(("javascript:", "vbscript:", "data:"))


class _ReviewHTMLSanitizer(HTMLParser):
    """Small post-render sanitizer for Python-Markdown's raw-HTML passthrough.

    This deliberately operates on rendered HTML so Markdown code spans and
    fenced blocks are escaped exactly once by Markdown itself.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self._blocked_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._blocked_depth:
            if tag in _BLOCKED_TAGS:
                self._blocked_depth += 1
            return
        if tag in _BLOCKED_TAGS:
            self._blocked_depth = 1
            return
        safe_attrs = []
        for name, value in attrs:
            if name.lower().startswith("on"):
                continue
            if value is not None and name.lower() in ("href", "src") and _unsafe_url(value):
                value = "#"
            if value is None:
                safe_attrs.append(f" {name}")
            else:
                safe_attrs.append(f' {name}="{html_lib.escape(value, quote=True)}"')
        self.parts.append(f"<{tag}{''.join(safe_attrs)}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._blocked_depth or tag in _BLOCKED_TAGS:
            return
        self.handle_starttag(tag, attrs)
        if self.parts:
            self.parts[-1] = self.parts[-1][:-1] + " />"

    def handle_endtag(self, tag: str) -> None:
        if self._blocked_depth:
            if tag in _BLOCKED_TAGS:
                self._blocked_depth -= 1
            return
        if tag not in _BLOCKED_TAGS:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self._blocked_depth:
            self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        if not self._blocked_depth:
            self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self._blocked_depth:
            self.parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        if not self._blocked_depth:
            self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        if not self._blocked_depth:
            self.parts.append(f"<!{decl}>")


def _sanitize_rendered_html(rendered: str) -> str:
    sanitizer = _ReviewHTMLSanitizer()
    sanitizer.feed(rendered)
    sanitizer.close()
    return "".join(sanitizer.parts)


def _norm_impact(word: str) -> str:
    w = word.lower()
    return "med" if w == "medium" else w


def _impact_badge(word: str) -> str:
    lvl = _norm_impact(word)
    return f'<span class="badge {lvl}">{lvl}</span>'


def _split_front_matter(text: str) -> tuple[dict, str]:
    """Return (front_matter_dict, body). Tolerates a missing front matter."""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            try:
                fm = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                fm = {}
            return (fm if isinstance(fm, dict) else {}), parts[2].lstrip("\n")
    return {}, text


def _inline_shot(shot: str, base_dirs: list[pathlib.Path]) -> str:
    """Return a <figure> with the screenshot inlined as base64, or a
    'missing' notice if the file can't be found under any base dir.

    Env vars / ``~`` in the path are expanded first so a central path like
    ``$TAKKUB_ARTIFACTS_DIR/screenshots/login.png`` written into the front
    matter resolves at convert time (central-home migration — shots live in
    the central artifacts dir, not the repo)."""
    rel = shot.strip()
    safe_rel = html_lib.escape(rel, quote=True)
    expanded = os.path.expanduser(os.path.expandvars(rel))
    # An absolute (post-expansion) path resolves on its own; a relative one
    # is still tried under each base dir as before.
    candidates: list[pathlib.Path] = []
    if os.path.isabs(expanded):
        candidates.append(pathlib.Path(expanded))
    else:
        candidates.extend((base / expanded).resolve() for base in base_dirs)
    for p in candidates:
        if p.exists() and p.is_file():
            mime = mimetypes.guess_type(p.name)[0] or "image/png"
            b64 = base64.b64encode(p.read_bytes()).decode()
            return (
                f'<figure><img alt="{safe_rel}" src="data:{mime};base64,{b64}">'
                f"<figcaption>{safe_rel}</figcaption></figure>"
            )
    return f'<div class="missing-shot">⚠ screenshot not found: <code>{safe_rel}</code></div>'


def render(md_path: str | pathlib.Path) -> pathlib.Path:
    """Render a design-review markdown file into a sibling .html and return
    its path. Screenshots from the `shots:` front matter are inlined."""
    md_path = pathlib.Path(md_path)
    text = md_path.read_text(encoding="utf-8")
    fm, body = _split_front_matter(text)

    shots = fm.get("shots") or []
    if isinstance(shots, str):
        shots = [shots]
    # resolve shot paths relative to cwd (critic's project root) AND to the
    # md file's dir — covers both how paths get written.
    base_dirs = [pathlib.Path.cwd(), md_path.resolve().parent]
    shots_html = "".join(_inline_shot(s, base_dirs) for s in shots)

    # Python-Markdown intentionally passes raw HTML through. Sanitize its
    # rendered output rather than pre-escaping the source, which double-escapes
    # code spans/blocks containing HTML, CSS selectors, JSX, or shell syntax.
    body_html = markdown.markdown(body, extensions=["extra", "sane_lists"])
    body_html = _sanitize_rendered_html(body_html)
    body_html = _IMPACT_EM.sub(lambda m: _impact_badge(m.group(1)), body_html)
    body_html = _IMPACT_BARE.sub(lambda m: _impact_badge(m.group(1)), body_html)

    meta_bits = [
        html_lib.escape(str(fm[k]), quote=True)
        for k in ("date", "project", "reviewer")
        if fm.get(k)
    ]
    meta_html = f'<div class="meta">{" · ".join(meta_bits)}</div>' if meta_bits else ""

    title = html_lib.escape(str(fm.get("project") or md_path.stem), quote=True)
    html = (
        "<!doctype html>\n"
        '<html lang="th"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Design review · {title}</title>"
        f'<style>{_CSS}</style></head><body><div class="wrap">'
        f"{meta_html}{shots_html}{body_html}"
        "<footer>Generated by agent_takkub.design_review_html · "
        "self-contained (screenshots inlined) · open in any browser</footer>"
        "</div></body></html>\n"
    )

    out = md_path.with_suffix(".html")
    out.write_text(html, encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("usage: python -m agent_takkub.design_review_html <review.md> ...")
        return 2
    rc = 0
    for a in args:
        try:
            out = render(a)
            print(f"OK {out}")
        except FileNotFoundError:
            print(f"ERR not found: {a}")
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
