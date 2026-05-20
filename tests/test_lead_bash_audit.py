"""Tests for lead_bash_audit — write-intent detection and JSONL audit log.

Phase 1 design: detect write-y bash commands and append to
runtime/lead_bash_audit.log as JSONL.  No blocking — audit-only.
Wire via Claude Code PreToolUse hook to exercise live (Phase 2).
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_takkub.lead_bash_audit import audit_lead_bash, detect_write_intent

# ──────────────────────────────────────────────────────────────
# detect_write_intent
# ──────────────────────────────────────────────────────────────


class TestDetectWriteIntent:
    def test_shell_redirect_gt(self) -> None:
        assert detect_write_intent("echo hello > file.txt") == "shell-redirect"

    def test_shell_redirect_append(self) -> None:
        assert detect_write_intent("echo hello >> file.txt") == "shell-redirect"

    def test_set_content(self) -> None:
        assert detect_write_intent("Set-Content x.txt 'data'") == "powershell-write"

    def test_out_file(self) -> None:
        assert detect_write_intent("Get-Date | Out-File log.txt") == "powershell-write"

    def test_add_content(self) -> None:
        assert detect_write_intent("Add-Content x.txt 'more'") == "powershell-write"

    def test_python_write(self) -> None:
        cmd = "python -c \"open('file.txt', 'w').write('hello')\""
        assert detect_write_intent(cmd) == "python-write"

    def test_git_apply(self) -> None:
        assert detect_write_intent("git apply foo.patch") == "git-apply"

    def test_sed_inplace(self) -> None:
        assert detect_write_intent("sed -i s/x/y/ file.txt") == "sed-inplace"

    def test_ls_no_write(self) -> None:
        assert detect_write_intent("ls -la") is None

    def test_cat_to_dev_null_false_positive_ok(self) -> None:
        # Phase 1 accepts false positives — > in any context triggers
        assert detect_write_intent("cat file > /dev/null") == "shell-redirect"

    def test_plain_read_command(self) -> None:
        assert detect_write_intent("cat file.txt") is None

    def test_git_status_no_write(self) -> None:
        assert detect_write_intent("git status") is None

    def test_python_read_open(self) -> None:
        # open(..., 'r') should NOT trigger python-write
        cmd = "python -c \"open('file.txt', 'r').read()\""
        assert detect_write_intent(cmd) is None

    def test_set_content_case_insensitive(self) -> None:
        # PowerShell verbs are case-insensitive; match regardless
        assert detect_write_intent("set-content data.json 'payload'") == "powershell-write"

    def test_out_file_case_insensitive(self) -> None:
        assert detect_write_intent("out-file result.txt") == "powershell-write"


# ──────────────────────────────────────────────────────────────
# audit_lead_bash
# ──────────────────────────────────────────────────────────────


class TestAuditLeadBash:
    def test_write_intent_appends_jsonl(self, tmp_path: Path) -> None:
        log_file = tmp_path / "lead_bash_audit.log"
        audit_lead_bash("echo hi > out.txt", cwd="/project", log_path=log_file)
        assert log_file.exists()
        line = json.loads(log_file.read_text(encoding="utf-8").strip())
        assert line["cmd"] == "echo hi > out.txt"
        assert line["cwd"] == "/project"
        assert line["reason"] == "shell-redirect"
        assert "ts" in line

    def test_no_write_intent_does_not_append(self, tmp_path: Path) -> None:
        log_file = tmp_path / "lead_bash_audit.log"
        audit_lead_bash("ls -la", cwd="/project", log_path=log_file)
        assert not log_file.exists()

    def test_long_cmd_truncated_at_500(self, tmp_path: Path) -> None:
        long_cmd = "echo " + "x" * 600 + " > out.txt"
        log_file = tmp_path / "lead_bash_audit.log"
        audit_lead_bash(long_cmd, cwd="/project", log_path=log_file)
        line = json.loads(log_file.read_text(encoding="utf-8").strip())
        assert len(line["cmd"]) <= 500

    def test_creates_file_when_missing(self, tmp_path: Path) -> None:
        log_file = tmp_path / "subdir" / "audit.log"
        audit_lead_bash("Set-Content x.txt 'data'", cwd="/x", log_path=log_file)
        assert log_file.exists()

    def test_multiple_calls_append_multiple_lines(self, tmp_path: Path) -> None:
        log_file = tmp_path / "lead_bash_audit.log"
        audit_lead_bash("echo a > a.txt", cwd="/x", log_path=log_file)
        audit_lead_bash("echo b > b.txt", cwd="/y", log_path=log_file)
        lines = [json.loads(row) for row in log_file.read_text(encoding="utf-8").splitlines()]
        assert len(lines) == 2
        assert lines[0]["cwd"] == "/x"
        assert lines[1]["cwd"] == "/y"

    def test_ts_is_iso8601(self, tmp_path: Path) -> None:
        from datetime import datetime

        log_file = tmp_path / "lead_bash_audit.log"
        audit_lead_bash("git apply p.patch", cwd="/repo", log_path=log_file)
        line = json.loads(log_file.read_text(encoding="utf-8").strip())
        # Should parse without error
        datetime.fromisoformat(line["ts"])
