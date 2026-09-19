"""Central report builder for 3 report types: customer/dev/boss.

Usage:
    from agent_takkub.report_builder import build_report

    html = build_report(
        report_type='customer',
        content_dir='./report-content',
        title='My Report'
    )

Reports support all providers (claude/codex/gemini-agy/opencode/kimi/cursor)
and work on both Windows ConPTY and macOS.
"""

import base64
import hashlib
import io
import os
import re
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    Image = None


# Forbidden words for customer reports
CUSTOMER_FORBIDDEN = {
    "commit",
    ".ts",
    ".py",
    ".js",
    ".html",
    ".css",
    "/api/",
    "/path/",
    "./",
    "../",
    "error",
    "bug",
    "бага",
    "バグ",
    "บั๊ก",
    "migration",
    "qa-gate",
    "qa-test",
    "endpoint",
    "database",
    "response",
    "request",
    "status code",
    "codex",
    "claude",
    "gemini",
    "opencode",
    "kimi",
    "cursor",
    "anthropic",
}

# (#676) Warn-level patterns for customer reports — things that slipped into
# real customer manuals and had to be chased down by hand. Warnings never
# block the build (unlike CUSTOMER_FORBIDDEN): each needs human judgement.
CUSTOMER_WARN_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    (
        "การรับประกันเวลา (ระบบไม่ได้การันตี)",
        re.compile(r"(?:ภายใน|ไม่เกิน)\s*\d+(?:\s*[-–]\s*\d+)?\s*(?:วินาที|นาที|ชั่วโมง|ชม\.?)"),
    ),
    (
        "อ้างว่าอัตโนมัติ (ตรวจว่าจริงไหม — หลายขั้นตอนมีคนกดอนุมัติ)",
        re.compile(r"อัตโนมัติ|ระบบจะ(?:ดำเนินการ)?(?:โอน|จ่าย|เติม)"),
    ),
    (
        "อีเมล/โดเมนภายใน",
        re.compile(r"@[\w.-]*\.local\b|@admin\.com\b", re.I),
    ),
    (
        "ชื่อบัญชีทดสอบ",
        re.compile(r"\b(?:QA|UAT|Regress(?:ion)?|Zero\s*Balance)\b|บัญชีทดสอบ", re.I),
    ),
    (
        "host:port ภายใน",
        re.compile(r"\b(?:localhost|127\.0\.0\.1|(?:\d{1,3}\.){3}\d{1,3}):\d{2,5}\b", re.I),
    ),
)

# (#676) lint reads TEXT only — it cannot see pixels. A pane once reported
# "no internal data leaked" off a clean grep while the screenshots showed 8
# internal e-mail accounts. Every lint result must carry this line.
LINT_TEXT_ONLY_DISCLAIMER = (
    "⚠ lint ตรวจเฉพาะข้อความ ไม่ได้ตรวจเนื้อหาในภาพ — "
    "ข้อมูลจริง/อีเมล/ชื่อทดสอบที่อยู่ในพิกเซลของรูปต้องเปิดดูด้วยตาเองทุกรูป"
)

# Extra CSS for all report types (mobile fixes, status badges, KPIs)
EXTRA_CSS = """
  .status{display:inline-block;border-radius:999px;padding:1px 10px;font-size:.82rem;font-weight:700;white-space:nowrap}
  .status.ok{background:var(--member-soft);color:var(--member)}
  .status.bad{background:#f8e1dc;color:#a2331b}
  .status.skip{background:var(--accent-soft);color:var(--accent)}
  @media (prefers-color-scheme: dark){:root:not([data-theme="light"]) .status.bad{background:#3b1d16;color:#f0a58f}}
  :root[data-theme="dark"] .status.bad{background:#3b1d16;color:#f0a58f}
  .kpis{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));margin:6px 0 4px}
  .kpi{background:var(--bg);border:1px solid var(--line);border-radius:14px;padding:12px 14px}
  .kpi b{display:block;font-size:1.6rem;line-height:1.2}
  .kpi span{color:var(--muted);font-size:.9rem}
  code{font-family:ui-monospace,Consolas,monospace;font-size:.88em;background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:0 5px;word-break:break-all}
  pre.ev{background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:10px 12px;overflow-x:auto;font-size:.85rem;line-height:1.5;margin:10px 0 0}
  section.sys>*{min-width:0}
  .shots{grid-template-columns:repeat(auto-fit,minmax(min(260px,100%),1fr))}
  pre.ev{white-space:pre-wrap;overflow-wrap:break-word}
  @media (max-width:900px){section.sys{grid-template-columns:minmax(0,1fr)}}
  @media (max-width:640px){table{min-width:0}th,td{overflow-wrap:break-word;padding:10px 8px}td:first-child,.status{white-space:normal}.status{border-radius:8px;padding:2px 8px;line-height:1.4}}
  figure img{cursor:zoom-in}
  figcaption{padding:8px 12px;font-size:.88rem;color:var(--muted);line-height:1.5}
  .imgpair{display:grid;gap:14px;grid-template-columns:repeat(2,minmax(0,1fr));align-items:start;margin:10px 0}
  .imgpair .device{display:inline-block;background:var(--accent-soft);color:var(--accent);border-radius:999px;padding:1px 10px;font-size:.78rem;font-weight:700}
  .imgpair .pair-cap{grid-column:1/-1;margin:0;color:var(--muted);font-size:.92rem;line-height:1.55}
  @media (max-width:640px){.imgpair{grid-template-columns:minmax(0,1fr)}.imgpair .pair-cap{grid-column:auto}}
"""


class ReportBuilder:
    def __init__(self, report_type: str, content_dir: str, max_width: int = 1440):
        """Initialize report builder.

        Args:
            report_type: 'customer', 'dev', or 'boss'
            content_dir: directory containing content.html, images.txt, and other files
            max_width: maximum image width in pixels
        """
        if report_type not in ("customer", "dev", "boss"):
            raise ValueError(f"Invalid report_type: {report_type}")

        self.report_type = report_type
        self.content_dir = Path(content_dir)
        self.max_width = max_width
        self.images: dict[str, str] = {}  # name -> data URI
        self.image_md5s: set[str] = set()  # track duplicate images
        self._load_images()

    def _load_images(self) -> None:
        """Load images from images.txt and convert to data URIs."""
        images_file = self.content_dir / "images.txt"
        if not images_file.exists():
            return

        with open(images_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "|" not in line:
                    continue

                name, path = line.split("|", 1)
                name = name.strip()
                path = path.strip()

                if os.path.exists(path):
                    data_uri = self._image_to_data_uri(path)
                    self.images[name] = data_uri

                    # Check for duplicate images by MD5
                    md5 = hashlib.md5(Path(path).read_bytes()).hexdigest()
                    if md5 in self.image_md5s:
                        print(f"WARNING: duplicate image hash found for {name}", file=sys.stderr)
                    self.image_md5s.add(md5)
                else:
                    print(f"WARNING: image not found: {path}", file=sys.stderr)

    def _image_to_data_uri(self, path: str) -> str:
        """Convert image to JPEG data URI."""
        if Image is None:
            # Fallback: read as base64 if PIL not available
            data = Path(path).read_bytes()
            return f"data:image/png;base64,{base64.b64encode(data).decode()}"

        im = Image.open(path)

        # Convert RGBA/palette images to RGB
        if im.mode in ("RGBA", "P", "LA"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            im = im.convert("RGBA")
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")

        # Resize if wider than max_width
        if im.width > self.max_width:
            new_height = round(im.height * self.max_width / im.width)
            im = im.resize((self.max_width, new_height), Image.LANCZOS)

        # Save as JPEG to BytesIO
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80, optimize=True)

        return f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()}"

    def build(self, title: str | None = None) -> str:
        """Build the complete HTML report.

        Args:
            title: HTML title (default: based on report type)

        Returns:
            Complete HTML document as string
        """
        template_html = self._get_template()
        content_html = self._get_content()

        # Get title
        if not title:
            title = {
                "customer": "User Manual",
                "dev": "Technical Report",
                "boss": "Executive Summary",
            }[self.report_type]

        # Split template
        style_end = template_html.index("</style>")
        head = (
            template_html[:style_end]
            + EXTRA_CSS
            + template_html[style_end : template_html.index("<body>") + len("<body>")]
        )
        tail = template_html[template_html.index('<div class="lb" id="lb"') :]

        # Update title
        head = re.sub(r"<title>.*?</title>", f"<title>{title}</title>", head)

        # Replace image placeholders and handle special sections
        body = content_html

        # Handle conditional sections for report type
        if self.report_type == "customer":
            # Remove dev/boss-only sections
            body = re.sub(r"<!--DEV-ONLY-->.*?<!--/DEV-ONLY-->", "", body, flags=re.DOTALL)
            body = re.sub(r"<!--BOSS-ONLY-->.*?<!--/BOSS-ONLY-->", "", body, flags=re.DOTALL)
        elif self.report_type == "dev":
            body = re.sub(
                r"<!--CUSTOMER-ONLY-->.*?<!--/CUSTOMER-ONLY-->", "", body, flags=re.DOTALL
            )
            body = re.sub(r"<!--BOSS-ONLY-->.*?<!--/BOSS-ONLY-->", "", body, flags=re.DOTALL)
        elif self.report_type == "boss":
            body = re.sub(
                r"<!--CUSTOMER-ONLY-->.*?<!--/CUSTOMER-ONLY-->", "", body, flags=re.DOTALL
            )
            body = re.sub(r"<!--DEV-ONLY-->.*?<!--/DEV-ONLY-->", "", body, flags=re.DOTALL)

        # Replace image placeholders
        missing = []

        def _src(name: str) -> str:
            if name not in self.images:
                missing.append(name)
                return ""
            return self.images[name]

        def replace_img(m: re.Match) -> str:
            return _src(m.group(1))

        # (#676) `{{figure:name|คำบรรยาย}}` — a full figure with a real
        # caption slot (a good caption says what is happening on screen, not
        # the file name), and `{{imgpair:desk|mobile|คำบรรยาย}}` — the
        # desktop/mobile pair every customer manual needs, laid out side by
        # side with device chips and stacked on narrow screens. Both expand
        # BEFORE the plain `{{img:}}` pass; `{{img:}}` stays byte-compatible.
        def replace_figure(m: re.Match) -> str:
            name, caption = m.group(1), (m.group(2) or "").strip()
            cap_html = f"<figcaption>{caption}</figcaption>" if caption else ""
            return f'<figure><img src="{_src(name)}" alt="{caption}" loading="lazy">{cap_html}</figure>'

        def replace_imgpair(m: re.Match) -> str:
            desk, mobile, caption = m.group(1), m.group(2), (m.group(3) or "").strip()
            cap_html = f'<p class="pair-cap">{caption}</p>' if caption else ""
            return (
                '<div class="imgpair">'
                f'<figure><img src="{_src(desk)}" alt="{caption} (จอคอมพิวเตอร์)" loading="lazy">'
                '<figcaption><span class="device">💻 คอมพิวเตอร์</span></figcaption></figure>'
                f'<figure><img src="{_src(mobile)}" alt="{caption} (จอมือถือ)" loading="lazy">'
                '<figcaption><span class="device">📱 มือถือ</span></figcaption></figure>'
                f"{cap_html}</div>"
            )

        body = re.sub(r"\{\{figure:([\w-]+)(?:\|([^}]*))?\}\}", replace_figure, body)
        body = re.sub(r"\{\{imgpair:([\w-]+)\|([\w-]+)(?:\|([^}]*))?\}\}", replace_imgpair, body)
        body = re.sub(r"\{\{img:([\w-]+)\}\}", replace_img, body)

        if missing:
            # ValueError (not sys.exit) so cli.py's build wrapper reports it
            # as an ordinary "build failed: ..." instead of a bare SystemExit
            # escaping the except.
            raise ValueError(f"Missing images: {', '.join(sorted(set(missing)))}")

        # Combine and return
        return head + "\n" + body + "\n" + tail

    def _get_template(self) -> str:
        """Get the base template HTML."""
        # Source of truth: repo-root `assets/report/template.html` (dev
        # checkout) or the packaged `_assets/report/` copy (installed build,
        # staged by setup.py from the same repo-root source). The per-content
        # template override and the minimal fallback stay last so a user
        # explicitly dropping `template.html` into their content dir always
        # wins over the shipped kit.
        asset_template = report_asset_root() / "template.html"
        if asset_template.exists():
            return asset_template.read_text(encoding="utf-8")

        content_template = self.content_dir / "template.html"
        if content_template.exists():
            return content_template.read_text(encoding="utf-8")

        # Fallback to a minimal template if nothing is found
        return self._get_fallback_template()

    def _get_content(self) -> str:
        """Get the content.html file."""
        content_file = self.content_dir / "content.html"
        if content_file.exists():
            return content_file.read_text(encoding="utf-8")
        return ""

    def _get_fallback_template(self) -> str:
        """Return a minimal HTML template."""
        return """<!doctype html>
<html lang="th">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Report</title>
<style>
  :root{
    --bg:#f5f4f0; --paper:#ffffff; --ink:#1c1b19; --muted:#63615b; --line:#e2dfd6;
    --accent:#a86a00; --accent-soft:#f6ead2; --admin:#3c5a8a; --admin-soft:#e6ecf5;
    --member:#2f7a55; --member-soft:#e3f1e9; --shot-bg:#ebe8e0;
  }
  @media (prefers-color-scheme: dark){
    :root:not([data-theme="light"]){
      --bg:#131311; --paper:#1c1c19; --ink:#eceae4; --muted:#a29f97; --line:#34332e;
      --accent:#e0a53a; --accent-soft:#3a2d15; --admin:#8fb0e0; --admin-soft:#1e2a3b;
      --member:#76c79c; --member-soft:#18301f; --shot-bg:#0d0d0c;
    }
  }
  :root[data-theme="dark"]{
    --bg:#131311; --paper:#1c1c19; --ink:#eceae4; --muted:#a29f97; --line:#34332e;
    --accent:#e0a53a; --accent-soft:#3a2d15; --admin:#8fb0e0; --admin-soft:#1e2a3b;
    --member:#76c79c; --member-soft:#18301f; --shot-bg:#0d0d0c;
  }
  *{box-sizing:border-box}
  html,body{margin:0}
  body{background:var(--bg);color:var(--ink);
    font:16px/1.7 "Noto Sans Thai","Leelawadee UI","Sukhumvit Set",system-ui,sans-serif;
    padding-inline:clamp(16px,3vw,48px);padding-block:clamp(24px,4vw,56px)}
  header.top{margin-bottom:clamp(24px,3vw,40px)}
  header.top h1{font-size:clamp(1.7rem,3.2vw,2.6rem);line-height:1.25;margin:0 0 8px;letter-spacing:-.01em}
  header.top p{margin:0;color:var(--muted);font-size:1.05rem;max-width:70ch}
  .overview{background:var(--paper);border:1px solid var(--line);border-radius:18px;padding:clamp(16px,2vw,28px);margin-bottom:clamp(28px,3vw,44px)}
  .overview h2{margin:0 0 14px;font-size:1.25rem}
  .table-wrap{overflow-x:auto}
  table{border-collapse:collapse;width:100%;min-width:640px;font-size:.98rem}
  th,td{text-align:left;padding:12px 14px;border-bottom:1px solid var(--line);vertical-align:top}
  th{font-size:.85rem;color:var(--muted);font-weight:600}
  section.sys{background:var(--paper);border:1px solid var(--line);border-radius:18px;
    padding:clamp(18px,2.4vw,36px);margin-bottom:clamp(18px,2vw,28px);
    display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.25fr);gap:clamp(20px,3vw,48px);align-items:start}
  section.sys h2{margin:0 0 8px;font-size:clamp(1.3rem,2vw,1.7rem);line-height:1.3}
  .shots{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
  figure{margin:0;background:var(--shot-bg);border:1px solid var(--line);border-radius:14px;overflow:hidden;display:flex;flex-direction:column}
  figure img{display:block;width:100%;height:auto;max-height:620px;object-fit:contain}
  footer{color:var(--muted);font-size:.88rem;margin-top:28px}
  .lb{position:fixed;inset:0;background:rgba(0,0,0,.88);z-index:50;display:flex;flex-direction:column}
  .lb[hidden]{display:none}
  .lb-bar{display:flex;justify-content:flex-end;padding:10px 14px}
  .lb-bar button{background:#2a2a28;color:#eee;border:1px solid #444;border-radius:8px;padding:6px 14px;font:inherit;cursor:pointer}
  .lb-stage{flex:1;overflow:auto;display:flex;align-items:flex-start;justify-content:center;touch-action:pan-x pan-y pinch-zoom}
  .lb-stage img{max-width:100%;height:auto;margin:auto}
</style>
</head>
<body>
</body>
<div class="lb" id="lb" hidden role="dialog" aria-modal="true" aria-label="ภาพขยาย">
  <div class="lb-bar"><button type="button" id="lb-close" aria-label="ปิด">ปิด ✕</button></div>
  <div class="lb-stage" id="lb-stage"><img id="lb-img" alt=""></div>
</div>
<script>
(function(){
  var lb=document.getElementById('lb'), img=document.getElementById('lb-img');
  function close(){ lb.hidden=true; document.body.style.overflow=''; img.removeAttribute('src'); }
  document.addEventListener('click',function(e){
    var t=e.target;
    if(t && t.tagName==='IMG' && t.id!=='lb-img' && t.closest('figure')){
      img.src=t.src; img.alt=t.alt||''; lb.hidden=false; document.body.style.overflow='hidden';
    }
  });
  document.getElementById('lb-close').addEventListener('click',close);
  document.getElementById('lb-stage').addEventListener('click',function(e){ if(e.target===e.currentTarget) close(); });
  document.addEventListener('keydown',function(e){ if(!lb.hidden && e.key==='Escape') close(); });
})();
</script>
</html>
"""

    def lint_customer(self) -> list[str]:
        """Check for forbidden words in customer report (blockers only)."""
        return self.lint_customer_full()[0]

    def lint_customer_full(self) -> tuple[list[str], list[str]]:
        """(#676) Full customer lint: `(blockers, warnings)`.

        Blockers are the CUSTOMER_FORBIDDEN word hits (unchanged contract —
        they abort the build). Warnings are the CUSTOMER_WARN_PATTERNS hits:
        time guarantees, "automatic" claims, internal e-mails/test accounts/
        host:port — real leaks from real manuals, but each needs a human
        call, so they never block. Non-customer types return ([], [])."""
        if self.report_type != "customer":
            return [], []

        content = self._get_content()
        blockers: list[str] = []
        warnings: list[str] = []

        content_lower = content.lower()
        for word in sorted(CUSTOMER_FORBIDDEN):
            if word.lower() in content_lower:
                blockers.append(f"Found forbidden word/phrase: '{word}'")

        for label, pattern in CUSTOMER_WARN_PATTERNS:
            hits = pattern.findall(content)
            if hits:
                sample = str(hits[0]).strip()
                warnings.append(f"{label}: พบ {len(hits)} จุด เช่น '{sample[:60]}'")

        return blockers, warnings


# (#676) Default size ceiling for a built report. When a build crossed the
# team's 4 MB line before, the only "signal" was a pane silently DELETING a
# whole section to squeeze under it — warn out loud instead, with the two
# legitimate levers named.
REPORT_SIZE_WARN_BYTES = 4 * 1024 * 1024


def size_warning(html: str, max_bytes: int = REPORT_SIZE_WARN_BYTES) -> str:
    """Return a loud advisory when the built HTML exceeds `max_bytes`, else ''.

    Never blocks the build — the right response (lower JPEG quality, split
    the manual, drop images) is the author's call, not the builder's, and
    silently truncating content is exactly the failure this exists to stop.
    """
    if max_bytes <= 0:
        return ""
    size = len(html.encode("utf-8"))
    if size <= max_bytes:
        return ""
    return (
        f"⚠ ไฟล์ใหญ่ {size / (1024 * 1024):.2f} MB เกินเพดาน {max_bytes / (1024 * 1024):.2f} MB — "
        "ลดคุณภาพ JPEG (--max-width ให้แคบลง) หรือแยกคู่มือเป็นหลายไฟล์ · "
        "**ห้ามตัดหัวข้อทิ้งเงียบๆ เพื่อให้ไฟล์เล็กลง**"
    )


def check_mobile(html: str, viewports: list[int] | None = None) -> list[str]:
    """Check HTML for mobile viewport issues using playwright.

    Args:
        html: HTML content to check
        viewports: list of viewport widths to test (default: 360, 390, 768)

    Returns:
        list of issues found (empty if no issues or playwright not available)
    """
    if viewports is None:
        viewports = [360, 390, 768]

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []

    issues = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for width in viewports:
                page = browser.new_page(viewport={"width": width, "height": 800})
                try:
                    page.set_content(html)
                    page.wait_for_load_state("networkidle")

                    scroll_width = page.evaluate("document.documentElement.scrollWidth")
                    viewport_width = width
                    if scroll_width > viewport_width:
                        issues.append(
                            f"viewport {width}px: content width {scroll_width}px > viewport {viewport_width}px"
                        )

                    tables = page.query_selector_all("table")
                    for table_idx, table in enumerate(tables):
                        table_box = table.bounding_box()
                        table_width = table_box["width"] if table_box else 0
                        if table_width > viewport_width:
                            issues.append(
                                f"viewport {width}px: table {table_idx} width {table_width}px > viewport {viewport_width}px"
                            )
                finally:
                    page.close()
        finally:
            browser.close()

    return issues


def report_asset_root() -> Path:
    """Where the report kit (template + mobile-check scripts) lives.

    Mirrors ``config.ASSETS_ROOT``'s dev-vs-installed split for the other
    shipped assets (CLAUDE.md, docs/lead/*, skills): a dev checkout reads the
    committed repo-root source ``assets/report/``, an installed build reads
    the copy that setup.py stages from it into ``_assets/report/`` at wheel
    build time (and that pyproject package_data ships). Shipping the template
    inside the wheel is what stops an installed ``takkub report build`` from
    silently falling back to the minimal template.
    """
    from agent_takkub.config import DATA_HOME, REPO_ROOT

    if DATA_HOME == REPO_ROOT:
        return REPO_ROOT / "assets" / "report"
    return Path(__file__).resolve().parent / "_assets" / "report"


def build_report(
    report_type: str,
    content_dir: str,
    out_file: str | None = None,
    title: str | None = None,
    max_width: int = 1440,
) -> str:
    """Build a report and optionally write to file.

    Args:
        report_type: 'customer', 'dev', or 'boss'
        content_dir: directory with content.html, images.txt, etc.
        out_file: optional output file path
        title: optional HTML title
        max_width: maximum embedded-image width in pixels (#676)

    Returns:
        HTML content as string
    """
    builder = ReportBuilder(report_type, content_dir, max_width=max_width)
    html = builder.build(title)

    if out_file:
        Path(out_file).write_text(html, encoding="utf-8")
        print(f"Wrote {out_file}")

    return html
