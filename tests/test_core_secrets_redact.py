"""redact(): every known pattern is stripped, ordinary text is unchanged."""

from __future__ import annotations

from agent_takkub.core.secrets.redact import redact


def test_empty_string_unchanged():
    assert redact("") == ""


def test_ordinary_text_unchanged():
    text = "the quick brown fox jumps over the lazy dog, price: $12.50"
    assert redact(text) == text


def test_redacts_anthropic_api_key():
    text = "key=sk-ant-api03-abcdefghijklmnop1234567890"
    out = redact(text)
    assert "sk-ant-" not in out
    assert "***REDACTED***" in out


def test_redacts_generic_sk_key():
    text = "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx1234"  # gitleaks:allow (fake test fixture)
    out = redact(text)
    assert "sk-abcdefghijklmnopqrstuvwx1234" not in out
    assert "***REDACTED***" in out


def test_redacts_github_token():
    text = "token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"  # gitleaks:allow (fake test fixture)
    out = redact(text)
    assert "ghp_" not in out
    assert "***REDACTED***" in out


def test_redacts_bearer_header_keeps_bearer_prefix():
    text = "Authorization: Bearer abc123.def456-ghi_789=="
    out = redact(text)
    assert "abc123.def456-ghi_789==" not in out
    assert "Bearer ***REDACTED***" in out


def test_redacts_json_access_token_field():
    text = '{"access_token": "super-secret-value", "expires_in": 3600}'
    out = redact(text)
    assert "super-secret-value" not in out
    assert '"access_token": "***REDACTED***"' in out
    assert "expires_in" in out


def test_redacts_json_refresh_token_camel_case_field():
    text = '{"refreshToken": "rt-value-here"}'
    out = redact(text)
    assert "rt-value-here" not in out
    assert "***REDACTED***" in out


def test_does_not_redact_unrelated_json_fields():
    text = '{"username": "alice", "id": 42}'
    assert redact(text) == text
