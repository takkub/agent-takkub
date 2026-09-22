"""#695: no role may write into the cockpit's own venv/interpreter, and a
PowerShell content write with a dynamic positional target is refused.

Threat model (see venv_integrity.py): 2026-09-22 a backend pane ran
``Set-Content -NoNewline 'graphify-out\\.graphify_python' $py`` — pwsh bound
``$py`` (= the cockpit's ``venv/Scripts/python.exe``, first on PATH) as
-Path and wrote the literal over the interpreter. #633's rule did not fire
because the cockpit's OWN home is exempt from it.
"""

from __future__ import annotations

import io
import json
import pathlib
import sys

import pytest

from agent_takkub import cli, pane_guard

_WIN = sys.platform == "win32"


@pytest.fixture
def fake_venv(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """A stand-in for the cockpit venv; `_exec_dirs` is patched to it so the
    rule is exercised against a path we control on every OS."""
    root = (tmp_path / "cockpit-venv").resolve()
    scripts = root / ("Scripts" if _WIN else "bin")
    scripts.mkdir(parents=True)
    py = scripts / ("python.exe" if _WIN else "python")
    py.write_bytes(b"MZ".ljust(4096, b"\x00"))
    monkeypatch.setattr(pane_guard, "_exec_dirs", lambda: frozenset({root}))
    return py


def _run_guard(monkeypatch: pytest.MonkeyPatch, payload: dict, **env) -> dict:
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(payload)))
    for key in ("TAKKUB_ROLE", "TAKKUB_PROJECT", "TAKKUB_PORT_FILE", "TAKKUB_STORAGE_ROOT"):
        monkeypatch.delenv(key, raising=False)
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    return cli.cmd_guard(None)


class TestStaticTargetsIntoCockpitVenv:
    @pytest.mark.parametrize("role", ["lead", "backend", "codex", "gemini"])
    @pytest.mark.parametrize(
        "template",
        [
            "Copy-Item foo.txt '{py}'",
            'cp foo.txt "{py}"',
            "echo hi > '{py}'",
            "Set-Content -LiteralPath '{py}' -Value 'x'",
            "Remove-Item '{py}'",
            "rm -f '{py}'",
            "mv foo.txt '{py}'",
            "Move-Item foo.txt -Destination '{py}'",
            "New-Item '{py}' -ItemType File",
            "python -c \"open(r'{py}','w').write('x')\"",
            "pip install --target '{site}' graphify",
        ],
    )
    def test_every_write_shape_is_denied_for_every_role(
        self, fake_venv: pathlib.Path, template: str, role: str
    ) -> None:
        site = fake_venv.parent.parent / "Lib" / "site-packages"
        cmd = template.format(py=fake_venv, site=site)
        v = pane_guard.evaluate_instance_guard(cmd, role)
        if "pip install" in cmd:
            # Not a file verb the guard parses — documented gap, see the
            # #202 rule in pane_guard for the pip/editable case instead.
            pytest.skip("pip target writes are covered by the #202 editable-install rule")
        assert v is not None and not v.allowed, cmd
        assert v.rule == "instance_guard:cockpit_executable", (cmd, v)
        assert "#341/#695" in v.reason

    def test_reads_and_execution_stay_allowed(self, fake_venv: pathlib.Path) -> None:
        for cmd in (
            f"Get-Content '{fake_venv}'",
            f"cat '{fake_venv}'",
            f"'{fake_venv}' -c 'print(1)'",
            f"ls '{fake_venv.parent}'",
        ):
            v = pane_guard.evaluate_instance_guard(cmd, "backend")
            assert v is None or v.allowed, (cmd, v)

    def test_unrelated_writes_stay_allowed(
        self, fake_venv: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        for cmd in (
            f"echo hi > '{tmp_path / 'out.txt'}'",
            f"Copy-Item a.txt '{tmp_path / 'b.txt'}'",
            "Set-Content -LiteralPath graphify-out/.graphify_python -Value 'x'",
            "Set-Content out.txt 'literal text'",
            f"rm -rf '{tmp_path / 'cockpit-venv2'}'",
        ):
            v = pane_guard.evaluate_instance_guard(cmd, "backend", cwd=str(tmp_path))
            assert v is None or v.allowed, (cmd, v)


class TestDynamicWriteTargets:
    """The exact 2026-09-22 command and its family."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "Set-Content -NoNewline 'graphify-out\\.graphify_python' $py",
            "$py = (Get-Command python).Source; Set-Content -NoNewline 'graphify-out\\.graphify_python' $py",
            "Set-Content 'out.txt' $(Get-Command python).Source",
            "Add-Content $target 'line'",
            "Out-File -NoNewline $dest",
            "sc $p 'x'",
            'cp foo.txt "$(which python)"',
            "cp foo.txt `which python`",
            "echo x > $(which python)",
            "echo x > `which python`",
            "cat foo | tee $(which python)",
            "Copy-Item foo.txt (Get-Command python).Source",
        ],
    )
    def test_denied_with_the_named_parameter_hint(self, cmd: str) -> None:
        v = pane_guard.evaluate_instance_guard(cmd, "backend")
        assert v is not None and not v.allowed, cmd
        assert v.rule == "instance_guard:dynamic_write_target", (cmd, v)

    @pytest.mark.parametrize(
        "cmd",
        [
            "Set-Content -LiteralPath 'graphify-out\\.graphify_python' -Value $py",
            "Set-Content -Path out.txt -Value $text -NoNewline",
            "Out-File -FilePath out.txt -InputObject $x",
            "Set-Content 'out.txt' 'plain'",
            'Set-Content "$env:TEMP\\note.txt" "x"',
            "Remove-Item $tmp -Force",
            "echo x > $OUT",
            "cp a $DEST",
        ],
    )
    def test_named_parameters_and_plain_variables_stay_allowed(self, cmd: str) -> None:
        v = pane_guard.evaluate_instance_guard(cmd, "backend")
        assert v is None or v.allowed, (cmd, v)

    def test_lead_is_not_exempt(self) -> None:
        v = pane_guard.evaluate_instance_guard(
            "Set-Content -NoNewline 'graphify-out\\.graphify_python' $py", "lead"
        )
        assert v is not None and not v.allowed


class TestEditWriteTools:
    @pytest.mark.parametrize("tool", ["Edit", "Write"])
    @pytest.mark.parametrize("role", ["lead", "backend"])
    def test_edit_write_into_cockpit_venv_is_denied(
        self, monkeypatch: pytest.MonkeyPatch, fake_venv: pathlib.Path, tool: str, role: str
    ) -> None:
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": tool,
            "tool_input": {"file_path": str(fake_venv), "content": "x"},
        }
        result = _run_guard(monkeypatch, payload, TAKKUB_ROLE=role)
        assert result.get("exit_code") == 2, result
