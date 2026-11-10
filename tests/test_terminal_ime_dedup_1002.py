"""#1002: typing Thai on macOS doubled characters ("กากา", "ก่ก่") — the
helper-textarea fallback compared its text against only the LAST xterm
onData chunk inside 50 ms. It now consumes a short log of what xterm really
emitted. Also: macOS/Thai faces in the font stack, the 250 ms heartbeat
redraws only after a write, and `.claude.json` is not re-parsed every 2 s."""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from agent_takkub import provider_usage

HTML = Path(__file__).resolve().parents[1] / "src" / "agent_takkub" / "static" / "terminal.html"


def test_single_chunk_50ms_heuristic_is_gone() -> None:
    text = HTML.read_text(encoding="utf-8")
    assert "lastEmittedTime <= 50" not in text
    assert "consumeEmitted(capturedVal)" in text and "recordEmitted(data)" in text


def test_font_stack_has_macos_and_thai_faces() -> None:
    text = HTML.read_text(encoding="utf-8")
    for face in ('"SF Mono"', '"Menlo"', '"Thonburi"', '"Leelawadee UI"'):
        assert face in text


def test_heartbeat_poke_redraws_only_after_a_write() -> None:
    text = HTML.read_text(encoding="utf-8")
    poke = text[text.index("window.termPoke") :][:200]
    assert "if (!writtenSincePoke) return;" in poke
    assert "onWriteParsed" in text


_SIM = textwrap.dedent(
    r"""
    const fs = require('fs');
    const body = [...fs.readFileSync(process.argv[1], 'utf8')
      .matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]).pop();
    new Function(body);
    const s = body.indexOf('const EMIT_LOG_MS');
    const e = body.indexOf('};', body.indexOf('const consumeEmitted')) + 2;
    const api = new Function(body.slice(s, e) + '\nreturn {recordEmitted, consumeEmitted};')();
    const out = [];
    const run = (emitted, flushed) => {
      const sent = [];
      emitted.forEach((x) => api.recordEmitted(x));
      flushed.forEach((v) => { if (!api.consumeEmitted(v)) sent.push(v); });
      out.push(sent.join('|'));
    };
    run(['ก', 'า'], ['ก', 'า']);
    run(['ก', '่'], ['ก่']);
    run([], ['ฟ']);
    run(['a'], ['a', 'a']);
    process.stdout.write(JSON.stringify(out));
    """
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_dedup_against_the_reported_races() -> None:
    res = subprocess.run(
        ["node", "-e", _SIM, str(HTML)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert res.returncode == 0, res.stderr
    import json

    assert json.loads(res.stdout) == ["", "", "ฟ", "a"]


def test_claude_json_parsed_once_per_change(tmp_path, monkeypatch) -> None:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    f = cfg / ".claude.json"
    f.write_text('{"oauthAccount": {"emailAddress": "a@example.test"}}', encoding="utf-8")
    monkeypatch.setattr(provider_usage.Path, "home", classmethod(lambda cls: tmp_path / "none"))
    provider_usage._OAUTH_ACCOUNT_CACHE.clear()
    reads: list[Path] = []
    real = Path.read_text

    def counting(self, *a, **k):
        reads.append(self)
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", counting)
    for _ in range(5):
        assert provider_usage._claude_account_label(cfg) == "a@example.test"
    assert reads.count(f) == 1
    f.write_text('{"oauthAccount": {"emailAddress": "bb@example.test"}}', encoding="utf-8")
    assert provider_usage._claude_account_label(cfg) == "bb@example.test"
