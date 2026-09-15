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

        def replace_img(m: re.Match) -> str:
            name = m.group(1)
            if name not in self.images:
                missing.append(name)
                return ""
            return self.images[name]

        body = re.sub(r"\{\{img:([\w-]+)\}\}", replace_img, body)

        if missing:
            print(f"ERROR: Missing images: {', '.join(sorted(set(missing)))}", file=sys.stderr)
            sys.exit(1)

        # Combine and return
        return head + "\n" + body + "\n" + tail

    def _get_template(self) -> str:
        """Get the base template HTML."""
        # Try to load from _assets first, then from content_dir
        asset_template = Path(__file__).parent / "_assets" / "report" / "template.html"
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
</style>
</head>
<body>
</body>
<div class="lb" id="lb" hidden role="dialog"></div>
</html>
"""

    def lint_customer(self) -> list[str]:
        """Check for forbidden words in customer report."""
        if self.report_type != "customer":
            return []

        content = self._get_content()
        issues = []

        content_lower = content.lower()
        for word in CUSTOMER_FORBIDDEN:
            # Simple case-insensitive search
            if word.lower() in content_lower:
                issues.append(f"Found forbidden word/phrase: '{word}'")

        return issues


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


def build_report(
    report_type: str, content_dir: str, out_file: str | None = None, title: str | None = None
) -> str:
    """Build a report and optionally write to file.

    Args:
        report_type: 'customer', 'dev', or 'boss'
        content_dir: directory with content.html, images.txt, etc.
        out_file: optional output file path
        title: optional HTML title

    Returns:
        HTML content as string
    """
    builder = ReportBuilder(report_type, content_dir)
    html = builder.build(title)

    if out_file:
        Path(out_file).write_text(html, encoding="utf-8")
        print(f"Wrote {out_file}")

    return html
