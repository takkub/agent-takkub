"""#441 — credential values are scrubbed at every cockpit forwarding hop
(done note → Lead digest, `takkub send`, remote mirror), key names kept."""

from __future__ import annotations

from unittest.mock import MagicMock

from agent_takkub.secret_redact import redact_secrets, redact_with_notice


class TestKeyValue:
    def test_env_line_value_replaced_key_kept(self) -> None:
        out, names = redact_secrets("PORT=8080\nGAMES_BFF_INTERNAL_SECRET=abcdef0123456789\n")
        assert "abcdef0123456789" not in out
        assert "GAMES_BFF_INTERNAL_SECRET=<redacted:GAMES_BFF_INTERNAL_SECRET>" in out
        assert "PORT=8080" in out
        assert names == ["GAMES_BFF_INTERNAL_SECRET"]

    def test_quoted_and_export_forms(self) -> None:
        text = "export DB_PASSWORD=\"s3cr3t-value\"\nAPI_KEY: 'zzzz-yyyy-1234'"
        out, names = redact_secrets(text)
        assert out == "export DB_PASSWORD=\"<redacted:DB_PASSWORD>\"\nAPI_KEY: '<redacted:API_KEY>'"
        assert names == ["DB_PASSWORD", "API_KEY"]

    def test_placeholders_left_alone(self) -> None:
        text = (
            "SECRET_KEY=${SECRET_KEY}\nAPI_TOKEN=<paste-here>\nJWT_SECRET=changeme\nX_KEY=xxxxxxxx"
        )
        out, names = redact_secrets(text)
        assert out == text
        assert names == []

    def test_prose_mentioning_key_not_touched(self) -> None:
        text = "ตั้ง API_KEY ในไฟล์ .env แล้ว restart"
        out, names = redact_secrets(text)
        assert out is text and names == []

    def test_sed_output_style_line(self) -> None:
        # what `sed -n '100,110p' deploy/.env.vps` prints inside a tool result
        text = "  GAMES_BFF_INTERNAL_SECRET=9f8e7d6c5b4a39281706f5e4d3c2b1a0"  # gitleaks:allow
        out, names = redact_secrets(text)
        assert "9f8e7d6c" not in out
        assert names == ["GAMES_BFF_INTERNAL_SECRET"]


class TestLiterals:
    def test_jwt_and_github_token(self) -> None:
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"  # gitleaks:allow
        text = f"Authorization: Bearer {jwt}\ntoken ghp_abcdefghijklmnopqrstuvwxyz0123"
        out, names = redact_secrets(text)
        assert jwt not in out and "ghp_abcdefghij" not in out
        assert "<redacted:jwt>" in out and "<redacted:github-token>" in out
        assert names == ["jwt", "github-token"]

    def test_pem_block(self) -> None:
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nAAAA\n-----END RSA PRIVATE KEY-----"  # gitleaks:allow
        out, names = redact_secrets(f"key:\n{pem}\nend")
        assert out == "key:\n<redacted:private-key>\nend"
        assert names == ["private-key"]

    def test_clean_text_returns_same_object(self) -> None:
        text = "deploy เสร็จ nginx reload แล้ว"
        out, names = redact_secrets(text)
        assert out is text and names == []


class TestNotice:
    def test_trailer_names_what_was_scrubbed(self) -> None:
        out = redact_with_notice("A_SECRET=abcd1234\nB_TOKEN=efgh5678")
        assert out.endswith("⚠ cockpit redacted 2 secret value(s): A_SECRET, B_TOKEN (#441)")

    def test_clean_text_unchanged(self) -> None:
        text = "nothing here"
        assert redact_with_notice(text) is text


class TestHops:
    def test_orchestrator_send_and_notify_use_redaction(self, monkeypatch) -> None:
        from PyQt6.QtCore import QObject

        from agent_takkub import orchestrator as orch_mod
        from agent_takkub.orchestrator import Orchestrator

        o = Orchestrator.__new__(Orchestrator)
        QObject.__init__(o)
        events: list[dict] = []
        monkeypatch.setattr(orch_mod, "_log_event", lambda ev, **kw: events.append({ev: kw}))
        out = o._redact_forwarded_text("X_SECRET=abcd1234", "P", hop="send", from_role="backend")
        assert "abcd1234" not in out and "<redacted:X_SECRET>" in out
        assert events and "forwarded_secret_redacted" in events[0]
        assert events[0]["forwarded_secret_redacted"]["names"] == ["X_SECRET"]
        # clean text: no event, same object back
        events.clear()
        clean = "done"
        assert o._redact_forwarded_text(clean, "P", hop="send") is clean
        assert events == []

    def test_remote_mirror_redacts_text_field(self, monkeypatch) -> None:
        from agent_takkub.remote import notify

        scanner = MagicMock()
        scanner.read_messages.return_value = [
            {"text": "DB_PASSWORD=hunter22222", "kind": "lead"},
            {"text": "ok", "kind": "me"},
            {"kind": "sys"},
        ]
        monkeypatch.setattr(notify, "_read_from_conversation_store_v2", lambda *_a: None)
        monkeypatch.setattr(notify, "history_scanner", lambda _p: scanner)
        rows = notify.read_recent_lead_messages(MagicMock(), 10, provider="claude", project_ns="P")
        assert "hunter22222" not in rows[0]["text"]
        assert "<redacted:DB_PASSWORD>" in rows[0]["text"]
        assert rows[1] == {"text": "ok", "kind": "me"}
        assert rows[2] == {"kind": "sys"}
