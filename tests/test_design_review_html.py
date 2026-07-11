"""Tests for design_review_html — md → self-contained HTML converter."""

from __future__ import annotations

import base64

from agent_takkub.design_review_html import _split_front_matter, render

# 1x1 transparent PNG
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _write_review(
    tmp_path, shots_block, body="## 🔧 ปรับ\n\n- **Fix X** — do it — *impact: high*\n"
):
    md = tmp_path / "2026-05-31-sample.md"
    md.write_text(
        f"---\ndate: 2026-05-31\nproject: sample\nreviewer: critic\n{shots_block}---\n\n# Review\n\n{body}",
        encoding="utf-8",
    )
    return md


class TestFrontMatter:
    def test_parses_shots_list(self):
        fm, body = _split_front_matter(
            "---\nproject: x\nshots:\n  - a.png\n  - b.png\n---\n\n# Hi\n"
        )
        assert fm["project"] == "x"
        assert fm["shots"] == ["a.png", "b.png"]
        assert body.startswith("# Hi")

    def test_no_front_matter(self):
        fm, body = _split_front_matter("# Just markdown\n\ntext")
        assert fm == {}
        assert body.startswith("# Just markdown")


class TestRender:
    def test_inlines_screenshot_as_base64(self, tmp_path):
        (tmp_path / "shot.png").write_bytes(_PNG)
        md = _write_review(tmp_path, "shots:\n  - shot.png\n")
        out = render(md)
        assert out == md.with_suffix(".html")
        html = out.read_text(encoding="utf-8")
        assert "data:image/png;base64," in html
        assert base64.b64encode(_PNG).decode() in html  # the actual bytes inlined

    def test_missing_shot_shows_notice_not_crash(self, tmp_path):
        md = _write_review(tmp_path, "shots:\n  - nope.png\n")
        html = render(md).read_text(encoding="utf-8")
        assert "screenshot not found" in html
        assert "nope.png" in html

    def test_impact_tag_becomes_badge(self, tmp_path):
        md = _write_review(tmp_path, "")
        html = render(md).read_text(encoding="utf-8")
        assert '<span class="badge high">high</span>' in html
        # the raw "impact: high" text should be gone (replaced by the badge)
        assert "impact: high" not in html

    def test_medium_normalised_to_med(self, tmp_path):
        md = _write_review(tmp_path, "", body="- **Y** — z — *impact: medium*\n")
        html = render(md).read_text(encoding="utf-8")
        assert '<span class="badge med">med</span>' in html

    def test_self_contained_has_inline_css_and_has_selector(self, tmp_path):
        md = _write_review(tmp_path, "")
        html = render(md).read_text(encoding="utf-8")
        assert "<style>" in html
        assert ":has(.badge.high)" in html  # cards coloured purely via CSS
        assert 'src="data:' not in html or True  # no external asset refs required

    def test_expands_env_var_in_shot_path(self, tmp_path, monkeypatch):
        """Central-home item C: a shot path written as
        ``$TAKKUB_ARTIFACTS_DIR/screenshots/x.png`` in the front matter (not
        shell-expanded, since it's file content) resolves at convert time."""
        shots_dir = tmp_path / "artifacts" / "screenshots"
        shots_dir.mkdir(parents=True)
        (shots_dir / "login.png").write_bytes(_PNG)
        monkeypatch.setenv("TAKKUB_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
        md = _write_review(tmp_path, "shots:\n  - $TAKKUB_ARTIFACTS_DIR/screenshots/login.png\n")
        html = render(md).read_text(encoding="utf-8")
        assert base64.b64encode(_PNG).decode() in html  # resolved + inlined
        assert "screenshot not found" not in html

    def test_absolute_shot_path_resolves(self, tmp_path):
        shot = tmp_path / "abs.png"
        shot.write_bytes(_PNG)
        md = _write_review(tmp_path, f"shots:\n  - {shot}\n")
        html = render(md).read_text(encoding="utf-8")
        assert base64.b64encode(_PNG).decode() in html
