"""#696: the generated `takkub` pane shims refuse a clobbered interpreter
loudly instead of launching it into silence."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_takkub import cli_shim

_WIN = sys.platform == "win32"


def _posix_shell() -> str | None:
    """Git Bash on Windows (`bash` on PATH there is usually the WSL launcher,
    which cannot see `C:/` paths), plain `bash` elsewhere."""
    if not _WIN:
        return shutil.which("bash")
    for candidate in (
        Path(r"C:\Program Files\Git\bin\bash.exe"),
        Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
    ):
        if candidate.exists():
            return str(candidate)
    return None


def _fake_python(tmp_path: Path, healthy: bool) -> Path:
    scripts = tmp_path / "venv" / ("Scripts" if _WIN else "bin")
    scripts.mkdir(parents=True)
    (tmp_path / "venv" / "pyvenv.cfg").write_text(f"home = {tmp_path / 'base'}\n", encoding="utf-8")
    py = scripts / ("python.exe" if _WIN else "python")
    if healthy:
        py.write_bytes((b"MZ" if _WIN else b"\x7fELF").ljust(4096, b"\x00"))
    else:
        py.write_text("graphify-out\\.graphify_python", encoding="ascii")
    return py


class TestGeneration:
    def test_writes_shims_pointing_at_the_given_interpreter(self, tmp_path: Path) -> None:
        py = _fake_python(tmp_path, healthy=True)
        bin_dir = cli_shim.ensure_cli_shims(tmp_path / "bin", py)

        sh = (bin_dir / "takkub").read_text(encoding="utf-8")
        assert py.as_posix() in sh
        assert "-m agent_takkub.cli" in sh
        assert "[takkub] cockpit interpreter broken" in sh
        if _WIN:
            cmd = (bin_dir / "takkub.cmd").read_bytes()
            assert str(py).encode() in cmd
            assert b"MZ" in cmd
            assert b"\r\n" in cmd  # cmd.exe wants CRLF
        else:
            assert not (bin_dir / "takkub.cmd").exists()

    def test_idempotent_rewrites_only_on_change(self, tmp_path: Path) -> None:
        py = _fake_python(tmp_path, healthy=True)
        bin_dir = cli_shim.ensure_cli_shims(tmp_path / "bin", py)
        first = (bin_dir / "takkub").stat().st_mtime_ns
        cli_shim.ensure_cli_shims(tmp_path / "bin", py)
        assert (bin_dir / "takkub").stat().st_mtime_ns == first

        other = _fake_python(tmp_path / "other", healthy=True)
        cli_shim.ensure_cli_shims(tmp_path / "bin", other)
        assert other.as_posix() in (bin_dir / "takkub").read_text(encoding="utf-8")

    def test_pane_bin_dir_is_repo_bin_for_dev_and_data_home_bin_when_installed(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        assert cli_shim.pane_bin_dir(repo, repo) == repo / "bin"
        assert cli_shim.pane_bin_dir(tmp_path / "home", repo) == tmp_path / "home" / "bin"

    def test_unwritable_dir_raises_for_the_caller_to_fall_back(self, tmp_path: Path) -> None:
        py = _fake_python(tmp_path, healthy=True)
        blocker = tmp_path / "bin"
        blocker.write_text("not a dir", encoding="utf-8")
        with pytest.raises(OSError):
            cli_shim.ensure_cli_shims(blocker, py)


class TestShimRefusesBrokenInterpreter:
    """The shim must never `(no output)` — exit 1 + a `[takkub]` line on
    stderr is the contract every pane's `done`/`send` failure now shows."""

    @pytest.mark.skipif(not _WIN, reason="cmd.exe shim")
    def test_cmd_shim_windows(self, tmp_path: Path) -> None:
        py = _fake_python(tmp_path, healthy=False)
        bin_dir = cli_shim.ensure_cli_shims(tmp_path / "bin", py)
        r = subprocess.run(
            ["cmd", "/c", str(bin_dir / "takkub.cmd"), "--help"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert r.returncode == 1
        assert "[takkub] cockpit interpreter broken" in r.stderr
        assert "29 byte" in r.stderr

    def test_sh_shim(self, tmp_path: Path) -> None:
        bash = _posix_shell()
        if bash is None:
            pytest.skip("needs a POSIX shell (Git Bash on Windows)")
        py = _fake_python(tmp_path, healthy=False)
        bin_dir = cli_shim.ensure_cli_shims(tmp_path / "bin", py)
        r = subprocess.run(
            [bash, (bin_dir / "takkub").as_posix(), "--help"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert r.returncode == 1
        assert "[takkub] cockpit interpreter broken" in r.stderr
        assert "29 byte" in r.stderr

    @pytest.mark.skipif(not _WIN, reason="cmd.exe shim")
    def test_cmd_shim_runs_a_healthy_interpreter(self, tmp_path: Path) -> None:
        """Also covers a NON-venv interpreter (CI runs the suite from the
        hosted-toolcache python): with no pyvenv.cfg the repair hint is a
        plain sentence, and it must still survive cmd.exe's parser."""
        py = Path(sys.executable).with_name("python.exe")
        if not py.exists():
            pytest.skip("no console python.exe next to sys.executable")
        bin_dir = cli_shim.ensure_cli_shims(tmp_path / "bin", py)
        r = subprocess.run(
            ["cmd", "/c", str(bin_dir / "takkub.cmd"), "--help"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert r.returncode == 0, r.stderr
        assert "usage: takkub" in r.stdout
