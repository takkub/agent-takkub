"""Issue #123: Windows-native Chrome lifecycle for mini-browser."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent_takkub import browser_chrome


@pytest.mark.parametrize("role", ["qa", "critic", "designer"])
def test_all_browser_roles_get_native_chrome_on_windows(role: str) -> None:
    assert browser_chrome.should_manage_native_chrome(role, None, platform="win32")


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_non_windows_keeps_existing_launcher_path(platform: str) -> None:
    assert not browser_chrome.should_manage_native_chrome("qa", None, platform=platform)


def test_shards_never_get_shared_mb_chrome() -> None:
    assert not browser_chrome.should_manage_native_chrome("qa", 1, platform="win32")
    assert not browser_chrome.should_manage_native_chrome("critic", 2, platform="win32")


def test_non_browser_role_never_gets_native_chrome() -> None:
    assert not browser_chrome.should_manage_native_chrome("backend", None, platform="win32")


def test_chrome_bin_override_is_provider_neutral(tmp_path, monkeypatch) -> None:
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"stub")
    monkeypatch.setenv("CHROME_BIN", str(chrome))

    env: dict[str, str] = {}
    browser_chrome.apply_chrome_bin(env, "designer")

    assert env["CHROME_BIN"] == str(chrome)


def test_macos_user_app_path_still_resolves(tmp_path) -> None:
    chrome = (
        tmp_path / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome"
    )
    chrome.parent.mkdir(parents=True)
    chrome.write_bytes(b"stub")

    resolved = browser_chrome.find_chrome_executable(
        platform="darwin",
        env={},
        home=tmp_path,
    )

    assert resolved == str(chrome)


def test_reuses_existing_cdp_without_owning_process(monkeypatch) -> None:
    manager = browser_chrome.NativeChromeManager()
    monkeypatch.setattr(browser_chrome.sys, "platform", "win32")
    monkeypatch.setattr(manager, "_cdp_ready", lambda: True)
    popen = MagicMock(side_effect=AssertionError("must not launch"))
    monkeypatch.setattr(browser_chrome.subprocess, "Popen", popen)

    ok, msg = manager.ensure_started()
    manager.close()

    assert ok
    assert "reusing" in msg
    popen.assert_not_called()


def test_launches_native_headless_chrome_and_waits_for_cdp(tmp_path, monkeypatch) -> None:
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"stub")
    runtime = tmp_path / "runtime"
    process = MagicMock()
    process.poll.return_value = None
    popen = MagicMock(return_value=process)
    readiness = iter((False, True))
    manager = browser_chrome.NativeChromeManager()

    monkeypatch.setattr(browser_chrome.sys, "platform", "win32")
    monkeypatch.setattr(browser_chrome.config, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(browser_chrome, "find_chrome_executable", lambda: str(chrome))
    monkeypatch.setattr(manager, "_cdp_ready", lambda: next(readiness))
    monkeypatch.setattr(browser_chrome.subprocess, "Popen", popen)

    ok, msg = manager.ensure_started()

    assert ok
    assert "launched" in msg
    argv = popen.call_args.args[0]
    assert argv[0] == str(chrome)
    assert "--remote-debugging-port=9222" in argv
    assert "--headless=new" in argv
    assert any(arg.startswith("--user-data-dir=") for arg in argv)
    assert (runtime / "browser-profiles" / "mb-native-chrome").is_dir()


def test_close_kills_only_owned_windows_process_tree(monkeypatch) -> None:
    manager = browser_chrome.NativeChromeManager()
    process = MagicMock(pid=4321)
    process.poll.return_value = None
    manager._process = process
    manager._owns_process = True
    run = MagicMock()
    run.return_value.returncode = 0
    monkeypatch.setattr(browser_chrome.sys, "platform", "win32")
    monkeypatch.setattr(browser_chrome.subprocess, "run", run)

    manager.close()
    manager.close()

    assert run.call_count == 1
    assert run.call_args.args[0] == ["taskkill", "/PID", "4321", "/T", "/F"]


def test_ensure_started_is_noop_on_macos(monkeypatch) -> None:
    manager = browser_chrome.NativeChromeManager()
    monkeypatch.setattr(browser_chrome.sys, "platform", "darwin")
    popen = MagicMock(side_effect=AssertionError("must not launch"))
    monkeypatch.setattr(browser_chrome.subprocess, "Popen", popen)

    ok, msg = manager.ensure_started()

    assert ok
    assert "Windows-only" in msg
    popen.assert_not_called()
