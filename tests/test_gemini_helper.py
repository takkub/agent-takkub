"""Tests for `gemini_helper` - the Antigravity CLI (`agy`) wrapper
behind `takkub gemini "<prompt>"`. Google retired the standalone Gemini
CLI on 2026-06-18, so the `gemini` role now runs `agy`. Mocks
shutil.which + subprocess.run so no real `agy` calls leak from CI; the
goal is to pin the argv construction (`agy -p`, NOT a subcommand) + the
error-surfacing contract.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_takkub import gemini_helper


def _proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["agy"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class TestFindAgyExecutable:
    def test_returns_path_when_on_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper.shutil, "which", lambda name: "/usr/local/bin/agy")
        assert gemini_helper.find_agy_executable() == "/usr/local/bin/agy"

    def test_probes_the_agy_binary_not_gemini(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Regression guard: the role is named "gemini" but the binary is
        # `agy`. A which("gemini") probe would always miss post-migration.
        seen = {}

        def fake_which(name):
            seen["name"] = name
            return "/x/agy"

        monkeypatch.setattr(gemini_helper.shutil, "which", fake_which)
        gemini_helper.find_agy_executable()
        assert seen["name"] == "agy"

    def test_returns_none_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper.shutil, "which", lambda name: None)
        monkeypatch.setattr(gemini_helper, "_default_agy_paths", lambda: [])
        assert gemini_helper.find_agy_executable() is None

    def test_falls_back_to_fixed_install_path_when_off_PATH(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        # Regression: the Antigravity Windows installer drops agy.exe under
        # %LOCALAPPDATA%\agy\bin but doesn't reliably add it to PATH. When
        # which() misses, we must still find the binary at its fixed
        # location — otherwise the cockpit falsely degrades gemini→claude.
        agy_exe = tmp_path / "agy" / "bin" / "agy.exe"
        agy_exe.parent.mkdir(parents=True)
        agy_exe.write_text("")
        monkeypatch.setattr(gemini_helper.shutil, "which", lambda name: None)
        monkeypatch.setattr(gemini_helper, "_default_agy_paths", lambda: [agy_exe])
        assert gemini_helper.find_agy_executable() == str(agy_exe)

    def test_path_wins_over_fixed_location(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A correctly-registered PATH entry takes priority over the fixed
        # fallback (no needless disk probing when PATH already resolves).
        monkeypatch.setattr(gemini_helper.shutil, "which", lambda name: "/on/path/agy")
        monkeypatch.setattr(
            gemini_helper,
            "_default_agy_paths",
            lambda: [Path("/should/not/be/used/agy.exe")],
        )
        assert gemini_helper.find_agy_executable() == "/on/path/agy"


class TestDefaultAgyPaths:
    """L2 (cross-platform audit 2026-07-10): the fixed-install-path fallback
    used to be Windows-only, so a mac install with `agy` off PATH had no
    fallback at all and `gemini` silently degraded to a Claude substitute
    even when Antigravity was genuinely installed."""

    def test_windows_candidates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper.sys, "platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\alice\AppData\Local")
        candidates = gemini_helper._default_agy_paths()
        # Build the expected path the SAME way the code does (Path(local) / ...)
        # rather than a hardcoded backslash literal: pathlib is OS-flavoured, so
        # on a POSIX CI host `Path(r"C:\...\agy.exe")` is a different PosixPath
        # than `Path(r"C:\...") / "agy" / "bin" / "agy.exe"` (mixed separators).
        expected = Path(r"C:\Users\alice\AppData\Local") / "agy" / "bin" / "agy.exe"
        assert expected in candidates
        assert not any(str(c).startswith("/Applications") for c in candidates)

    def test_mac_candidates_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper.sys, "platform", "darwin")
        candidates = gemini_helper._default_agy_paths()
        assert Path("/Applications/Antigravity.app/Contents/MacOS/agy") in candidates
        assert Path("/opt/homebrew/bin/agy") in candidates  # Apple Silicon Homebrew
        assert Path("/usr/local/bin/agy") in candidates  # Intel Homebrew
        assert not any("AppData" in str(c) for c in candidates)

    def test_linux_gets_no_candidates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No latent-risk claim for Linux here — audit only covered mac; a
        # bare `linux` platform still relies on PATH alone, same as before.
        monkeypatch.setattr(gemini_helper.sys, "platform", "linux")
        assert gemini_helper._default_agy_paths() == []


class TestGeminiExec:
    def test_returns_install_hint_when_binary_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gemini_helper, "find_agy_executable", lambda: None)
        ok, msg = gemini_helper.gemini_exec("hi")
        assert ok is False
        assert "agy binary not on PATH" in msg
        assert "antigravity.google/download" in msg

    def test_rejects_empty_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper, "find_agy_executable", lambda: "/x/agy")
        ok, msg = gemini_helper.gemini_exec("   ")
        assert ok is False
        assert "empty prompt" in msg

    def test_builds_argv_without_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Antigravity's headless flag is `-p <prompt>` - NOT a subcommand
        # like codex's `exec`. Pinning this is the whole point of the
        # test: a future refactor that reuses codex's argv shape would
        # send `agy exec "..."` which would fail with "unknown command".
        monkeypatch.setattr(gemini_helper, "find_agy_executable", lambda: "/x/agy")
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return _proc(0, stdout="ok")

        monkeypatch.setattr(gemini_helper.subprocess, "run", fake_run)
        ok, msg = gemini_helper.gemini_exec("hello world")
        assert ok is True
        assert msg == "ok"
        # default timeout 120 → agy --print-timeout bounded to 115s (5s under us)
        assert seen["argv"] == ["/x/agy", "-p", "hello world", "--print-timeout", "115s"]

    def test_builds_argv_with_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # `-m <model>` goes before `-p <prompt>` (both are flags so
        # ordering is technically irrelevant, but we pin a stable shape
        # for diffability).
        monkeypatch.setattr(gemini_helper, "find_agy_executable", lambda: "/x/agy")
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return _proc(0, stdout="out")

        monkeypatch.setattr(gemini_helper.subprocess, "run", fake_run)
        ok, _ = gemini_helper.gemini_exec("review", model="gemini-3.1-pro")
        assert ok is True
        assert seen["argv"] == [
            "/x/agy",
            "-m",
            "gemini-3.1-pro",
            "-p",
            "review",
            "--print-timeout",
            "115s",
        ]

    def test_empty_output_on_success_is_actionable_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # agy `-p` exits 0 but emits nothing when run non-interactively (no TTY).
        # We must NOT hand back a blank "success" — turn it into guidance toward
        # the interactive pane instead.
        monkeypatch.setattr(gemini_helper, "find_agy_executable", lambda: "/x/agy")
        monkeypatch.setattr(
            gemini_helper.subprocess, "run", lambda *a, **k: _proc(0, stdout="   \n")
        )
        ok, msg = gemini_helper.gemini_exec("hi")
        assert ok is False
        assert "no output" in msg
        assert "takkub assign --role gemini" in msg

    def test_print_timeout_scales_with_subprocess_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gemini_helper, "find_agy_executable", lambda: "/x/agy")
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return _proc(0, stdout="ok")

        monkeypatch.setattr(gemini_helper.subprocess, "run", fake_run)
        gemini_helper.gemini_exec("hi", timeout=30)
        assert "--print-timeout" in seen["argv"]
        assert seen["argv"][seen["argv"].index("--print-timeout") + 1] == "25s"

    def test_propagates_cwd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper, "find_agy_executable", lambda: "/x/agy")
        seen = {}

        def fake_run(argv, **kwargs):
            seen["kwargs"] = kwargs
            return _proc(0)

        monkeypatch.setattr(gemini_helper.subprocess, "run", fake_run)
        gemini_helper.gemini_exec("hi", cwd="C:/projects/foo")
        assert seen["kwargs"]["cwd"] == "C:/projects/foo"

    def test_returns_false_on_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper, "find_agy_executable", lambda: "/x/agy")

        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="agy", timeout=120.0)

        monkeypatch.setattr(gemini_helper.subprocess, "run", fake_run)
        ok, msg = gemini_helper.gemini_exec("hi")
        assert ok is False
        assert "timed out" in msg

    def test_returns_false_when_binary_disappears(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper, "find_agy_executable", lambda: "/x/agy")

        def fake_run(*args, **kwargs):
            raise FileNotFoundError("agy")

        monkeypatch.setattr(gemini_helper.subprocess, "run", fake_run)
        ok, msg = gemini_helper.gemini_exec("hi")
        assert ok is False
        assert "disappeared" in msg

    def test_surfaces_stderr_on_nonzero_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper, "find_agy_executable", lambda: "/x/agy")
        monkeypatch.setattr(
            gemini_helper.subprocess,
            "run",
            lambda *a, **k: _proc(1, stderr="ERROR: auth expired. Re-run `agy`."),
        )
        ok, msg = gemini_helper.gemini_exec("hi")
        assert ok is False
        assert "auth expired" in msg

    def test_falls_back_to_stdout_when_stderr_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper, "find_agy_executable", lambda: "/x/agy")
        monkeypatch.setattr(
            gemini_helper.subprocess,
            "run",
            lambda *a, **k: _proc(2, stdout="rate-limited", stderr=""),
        )
        ok, msg = gemini_helper.gemini_exec("hi")
        assert ok is False
        assert "rate-limited" in msg

    def test_trims_trailing_whitespace_from_stdout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper, "find_agy_executable", lambda: "/x/agy")
        monkeypatch.setattr(
            gemini_helper.subprocess,
            "run",
            lambda *a, **k: _proc(0, stdout="answer text\n\n  "),
        )
        ok, msg = gemini_helper.gemini_exec("hi")
        assert ok is True
        assert msg == "answer text"


def _plant_project(config_dir: Path, project_id: str, folder_uri: str) -> None:
    import json

    config_dir.mkdir(parents=True, exist_ok=True)
    body = {
        "id": project_id,
        "projectResources": {"resources": [{"folderUri": folder_uri}]},
    }
    (config_dir / f"{project_id}.json").write_text(json.dumps(body), encoding="utf-8")


@pytest.fixture(autouse=True)
def _clear_mint_inflight():
    """`_MINT_INFLIGHT` is process-wide state (#132 followup) — reset it
    around every test in this module so one test's claim can never leak
    into another's."""
    gemini_helper._MINT_INFLIGHT.clear()
    yield
    gemini_helper._MINT_INFLIGHT.clear()


class TestFolderUriToPath:
    """agy's own `~/.gemini/config/projects/<uuid>.json` registry stores the
    project's folder two different ways (see resolve_agy_project_id's
    docstring) — both must decode to the same comparable path (#132)."""

    def test_percent_encoded_windows_drive_gitfolder_form(self) -> None:
        uri = "file:///c%3A/Users/monch/WebstormProjects/agent-takkub"
        assert (
            gemini_helper._folder_uri_to_path(uri) == "c:/Users/monch/WebstormProjects/agent-takkub"
        )

    def test_plain_windows_drive_top_level_form(self) -> None:
        uri = "file://C:/Users/monch/WebstormProjects/agent-takkub"
        assert (
            gemini_helper._folder_uri_to_path(uri) == "C:/Users/monch/WebstormProjects/agent-takkub"
        )

    def test_macos_posix_path_has_no_drive_letter_to_strip(self) -> None:
        uri = "file:///Users/monch/projects/agent-takkub"
        assert gemini_helper._folder_uri_to_path(uri) == "/Users/monch/projects/agent-takkub"

    def test_percent_encoded_unicode_path_segment(self) -> None:
        uri = "file:///c%3A/Users/monch/WebstormProjects/%E0%B8%9B"
        assert gemini_helper._folder_uri_to_path(uri) == "c:/Users/monch/WebstormProjects/\u0e1b"

    def test_non_file_scheme_returns_none(self) -> None:
        assert gemini_helper._folder_uri_to_path("https://example.com/x") is None


class TestNormalizePathForCompare:
    def test_backslashes_become_forward_slashes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper.sys, "platform", "win32")
        assert (
            gemini_helper._normalize_path_for_compare(
                r"C:\Users\monch\WebstormProjects\agent-takkub"
            )
            == "c:/users/monch/webstormprojects/agent-takkub"
        )

    def test_trailing_slash_is_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper.sys, "platform", "win32")
        assert (
            gemini_helper._normalize_path_for_compare("c:/Users/monch/agent-takkub/")
            == "c:/users/monch/agent-takkub"
        )

    def test_double_slashes_collapse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper.sys, "platform", "win32")
        assert (
            gemini_helper._normalize_path_for_compare("c:/Users//monch///agent-takkub")
            == "c:/users/monch/agent-takkub"
        )

    def test_case_insensitive_on_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper.sys, "platform", "win32")
        a = gemini_helper._normalize_path_for_compare("C:/Users/Monch/Agent-Takkub")
        b = gemini_helper._normalize_path_for_compare("c:/users/monch/agent-takkub")
        assert a == b

    def test_case_insensitive_on_macos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gemini_helper.sys, "platform", "darwin")
        a = gemini_helper._normalize_path_for_compare("/Users/Monch/Agent-Takkub")
        b = gemini_helper._normalize_path_for_compare("/users/monch/agent-takkub")
        assert a == b

    def test_case_sensitive_on_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Regression guard (#132 followup): ext4 et al. are case-sensitive —
        # unconditional lowercasing would falsely equate two distinct dirs.
        monkeypatch.setattr(gemini_helper.sys, "platform", "linux")
        a = gemini_helper._normalize_path_for_compare("/Users/Monch/Agent-Takkub")
        b = gemini_helper._normalize_path_for_compare("/users/monch/agent-takkub")
        assert a != b
        # ...but separator/trailing-slash collapsing still applies.
        assert (
            gemini_helper._normalize_path_for_compare("/Users/Monch//Agent-Takkub/")
            == "/Users/Monch/Agent-Takkub"
        )


class TestResolveAgyProjectId:
    """Match spawn cwd against agy's own project registry (#132) so an
    existing Antigravity project id is reused instead of every takkub spawn
    landing in agy's shared 'default-cli-project' pseudo-project bucket."""

    def _write_project(self, config_dir: Path, project_id: str, body: dict) -> None:
        import json

        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / f"{project_id}.json").write_text(json.dumps(body), encoding="utf-8")

    def test_no_config_dir_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(gemini_helper.Path, "home", lambda: tmp_path)
        assert gemini_helper.resolve_agy_project_id(str(tmp_path / "repo")) is None

    def test_matches_percent_encoded_gitfolder_form(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Windows-shaped registry entry (lowercase drive letter) matched
        # against a differently-cased Windows cwd relies on the
        # case-insensitive branch of _normalize_path_for_compare — pin the
        # platform explicitly so this doesn't depend on which OS CI runs it
        # on (Linux is case-sensitive and would otherwise fail this exact
        # case-mismatch on its own merits, not a real bug).
        monkeypatch.setattr(gemini_helper.sys, "platform", "win32")
        monkeypatch.setattr(gemini_helper.Path, "home", lambda: tmp_path)
        config_dir = tmp_path / ".gemini" / "config" / "projects"
        self._write_project(
            config_dir,
            "17fdc03a-8cb3-446e-a833-4aaffc55f6bb",
            {
                "id": "17fdc03a-8cb3-446e-a833-4aaffc55f6bb",
                "name": "agent-takkub",
                "projectResources": {
                    "resources": [
                        {
                            "gitFolder": {
                                "folderUri": "file:///c%3A/Users/monch/WebstormProjects/agent-takkub"
                            }
                        }
                    ]
                },
            },
        )
        result = gemini_helper.resolve_agy_project_id(
            r"C:\Users\monch\WebstormProjects\agent-takkub"
        )
        assert result == "17fdc03a-8cb3-446e-a833-4aaffc55f6bb"

    def test_matches_percent_encoded_gitfolder_form_posix(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # POSIX counterpart: no drive letter to percent-decode, and Linux's
        # case-sensitive compare means the cwd must match the registry
        # exactly (no case-folding to lean on).
        monkeypatch.setattr(gemini_helper.sys, "platform", "linux")
        monkeypatch.setattr(gemini_helper.Path, "home", lambda: tmp_path)
        config_dir = tmp_path / ".gemini" / "config" / "projects"
        self._write_project(
            config_dir,
            "posix-gitfolder-id",
            {
                "id": "posix-gitfolder-id",
                "name": "agent-takkub",
                "projectResources": {
                    "resources": [
                        {
                            "gitFolder": {
                                "folderUri": "file:///Users/monch/WebstormProjects/agent-takkub"
                            }
                        }
                    ]
                },
            },
        )
        result = gemini_helper.resolve_agy_project_id("/Users/monch/WebstormProjects/agent-takkub")
        assert result == "posix-gitfolder-id"

    def test_matches_plain_top_level_folderuri_form(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(gemini_helper.Path, "home", lambda: tmp_path)
        config_dir = tmp_path / ".gemini" / "config" / "projects"
        self._write_project(
            config_dir,
            "876a3c92-9f62-4d1e-8f5c-7ad6265764e7",
            {
                "id": "876a3c92-9f62-4d1e-8f5c-7ad6265764e7",
                "name": r"C:\Users\monch\WebstormProjects\agent-takkub",
                "projectResources": {
                    "resources": [
                        {"folderUri": "file://C:/Users/monch/WebstormProjects/agent-takkub"}
                    ]
                },
            },
        )
        result = gemini_helper.resolve_agy_project_id(
            "C:/Users/monch/WebstormProjects/agent-takkub"
        )
        assert result == "876a3c92-9f62-4d1e-8f5c-7ad6265764e7"

    def test_case_insensitive_match(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # Windows filesystem is case-insensitive — pin platform explicitly so
        # this test's outcome doesn't depend on which host CI runs it on.
        monkeypatch.setattr(gemini_helper.sys, "platform", "win32")
        monkeypatch.setattr(gemini_helper.Path, "home", lambda: tmp_path)
        config_dir = tmp_path / ".gemini" / "config" / "projects"
        self._write_project(
            config_dir,
            "case-test-id",
            {
                "id": "case-test-id",
                "projectResources": {
                    "resources": [
                        {"folderUri": "file://C:/Users/monch/WebstormProjects/agent-takkub"}
                    ]
                },
            },
        )
        result = gemini_helper.resolve_agy_project_id(
            "c:/users/MONCH/WebstormProjects/AGENT-TAKKUB"
        )
        assert result == "case-test-id"

    def test_no_match_returns_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(gemini_helper.Path, "home", lambda: tmp_path)
        config_dir = tmp_path / ".gemini" / "config" / "projects"
        self._write_project(
            config_dir,
            "unrelated-id",
            {
                "id": "unrelated-id",
                "projectResources": {
                    "resources": [{"folderUri": "file://C:/Users/monch/OtherProject"}]
                },
            },
        )
        result = gemini_helper.resolve_agy_project_id(
            "C:/Users/monch/WebstormProjects/agent-takkub"
        )
        assert result is None

    def test_corrupt_project_file_is_skipped_not_fatal(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(gemini_helper.Path, "home", lambda: tmp_path)
        config_dir = tmp_path / ".gemini" / "config" / "projects"
        config_dir.mkdir(parents=True)
        (config_dir / "broken.json").write_text("{not valid json", encoding="utf-8")
        self._write_project(
            config_dir,
            "good-id",
            {
                "id": "good-id",
                "projectResources": {
                    "resources": [
                        {"folderUri": "file://C:/Users/monch/WebstormProjects/agent-takkub"}
                    ]
                },
            },
        )
        result = gemini_helper.resolve_agy_project_id(
            "C:/Users/monch/WebstormProjects/agent-takkub"
        )
        assert result == "good-id"


class TestResolveAgyProjectIdConcurrentMint:
    """#132 followup: two panes spawning agy against the same not-yet-
    registered cwd at nearly the same time must not each mint their own
    project id — the second caller polls briefly for the first caller's
    in-flight mint to land instead of racing its own `--new-project`."""

    def test_first_caller_claims_the_mint_and_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(gemini_helper.Path, "home", lambda: tmp_path)

        def _boom(_seconds):
            raise AssertionError("first caller must not poll — nobody else is minting yet")

        monkeypatch.setattr(gemini_helper.time, "sleep", _boom)

        cwd = str(tmp_path / "repo")
        result = gemini_helper.resolve_agy_project_id(cwd)
        assert result is None
        target = gemini_helper._normalize_path_for_compare(cwd)
        assert target in gemini_helper._MINT_INFLIGHT

    def test_second_caller_polls_and_picks_up_id_written_mid_wait(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(gemini_helper.Path, "home", lambda: tmp_path)
        cwd = str(tmp_path / "repo")
        target = gemini_helper._normalize_path_for_compare(cwd)

        first = gemini_helper.resolve_agy_project_id(cwd)
        assert first is None  # becomes the minter, claims target

        config_dir = tmp_path / ".gemini" / "config" / "projects"
        folder_uri = f"file://{(tmp_path / 'repo').as_posix()}"
        sleep_calls = {"n": 0}

        def fake_sleep(_seconds):
            # Simulate agy finishing its mint (writing the registry entry)
            # partway through the second caller's poll wait, without
            # actually burning real wall-clock time in the test.
            sleep_calls["n"] += 1
            if sleep_calls["n"] == 1:
                _plant_project(config_dir, "minted-id", folder_uri)

        monkeypatch.setattr(gemini_helper.time, "sleep", fake_sleep)

        second = gemini_helper.resolve_agy_project_id(cwd)
        assert second == "minted-id"
        assert sleep_calls["n"] >= 1
        assert target not in gemini_helper._MINT_INFLIGHT

    def test_second_caller_falls_back_to_new_project_after_poll_timeout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(gemini_helper.Path, "home", lambda: tmp_path)
        monkeypatch.setattr(gemini_helper, "_MINT_POLL_TIMEOUT_SEC", 0.05)
        monkeypatch.setattr(gemini_helper, "_MINT_POLL_INTERVAL_SEC", 0.01)
        monkeypatch.setattr(gemini_helper.time, "sleep", lambda _s: None)

        cwd = str(tmp_path / "repo")
        first = gemini_helper.resolve_agy_project_id(cwd)
        assert first is None  # becomes the minter

        # Nobody ever writes the registry entry — the poll must give up and
        # return None (caller falls back to --new-project) rather than
        # blocking indefinitely.
        second = gemini_helper.resolve_agy_project_id(cwd)
        assert second is None

    def test_stale_inflight_marker_is_abandoned_without_polling(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(gemini_helper.Path, "home", lambda: tmp_path)
        monkeypatch.setattr(gemini_helper, "_MINT_INFLIGHT_TTL_SEC", 0.01)

        cwd = str(tmp_path / "repo")
        target = gemini_helper._normalize_path_for_compare(cwd)
        gemini_helper._MINT_INFLIGHT[target] = gemini_helper.time.monotonic() - 10

        def _boom(_seconds):
            raise AssertionError("a stale (expired) in-flight claim must not trigger a poll")

        monkeypatch.setattr(gemini_helper.time, "sleep", _boom)

        result = gemini_helper.resolve_agy_project_id(cwd)
        assert result is None
        # This caller re-claims the mint for itself rather than waiting.
        assert target in gemini_helper._MINT_INFLIGHT

    def test_immediate_registry_match_clears_any_inflight_marker(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(gemini_helper.Path, "home", lambda: tmp_path)
        cwd = str(tmp_path / "repo")
        target = gemini_helper._normalize_path_for_compare(cwd)
        gemini_helper._MINT_INFLIGHT[target] = gemini_helper.time.monotonic()

        config_dir = tmp_path / ".gemini" / "config" / "projects"
        folder_uri = f"file://{(tmp_path / 'repo').as_posix()}"
        _plant_project(config_dir, "already-id", folder_uri)

        result = gemini_helper.resolve_agy_project_id(cwd)
        assert result == "already-id"
        assert target not in gemini_helper._MINT_INFLIGHT
