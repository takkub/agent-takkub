"""#1056 / #1052: opencode 2.x shares one background server, so a pane only
gets its own model + instructions from a server it starts itself."""

from __future__ import annotations

import json
import re

import pytest

from agent_takkub import opencode_helper as oh
from agent_takkub import provider_spec


@pytest.fixture(autouse=True)
def _fresh_cache():
    oh._major_cache.clear()
    yield
    oh._major_cache.clear()


def _npm_install(prefix, package_parts, name, version, *, posix=False):
    modules = (prefix / "lib" / "node_modules") if posix else (prefix / "node_modules")
    pkg = modules.joinpath(*package_parts)
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text(
        json.dumps({"name": name, "version": version}), encoding="utf-8"
    )
    bin_dir = (prefix / "bin") if posix else prefix
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / ("opencode" if posix else "opencode.cmd")
    shim.write_text("shim", encoding="utf-8")
    return shim


def test_two_x_package_gets_a_private_server(tmp_path):
    shim = _npm_install(tmp_path, ("@opencode", "cli"), "@opencode/cli", "2.4.2")
    assert oh.opencode_major_version(str(shim)) == 2
    assert oh.private_server_argv(str(shim)) == ["--standalone"]


def test_one_x_package_is_left_alone(tmp_path):
    shim = _npm_install(tmp_path, ("opencode-ai",), "opencode-ai", "1.19.5")
    assert oh.opencode_major_version(str(shim)) == 1
    assert oh.private_server_argv(str(shim)) == []  # 1.x rejects the flag


def test_posix_prefix_layout(tmp_path):
    shim = _npm_install(tmp_path, ("@opencode", "cli"), "@opencode/cli", "2.3.2", posix=True)
    assert oh.private_server_argv(str(shim)) == ["--standalone"]


def test_unknown_version_adds_nothing(tmp_path, monkeypatch):
    shim = tmp_path / "opencode.cmd"
    shim.write_text("shim", encoding="utf-8")

    def no_subprocess(*a, **k):
        raise OSError("not runnable")

    import subprocess

    monkeypatch.setattr(subprocess, "run", no_subprocess)
    assert oh.opencode_major_version(str(shim)) is None
    assert oh.private_server_argv(str(shim)) == []


def test_missing_binary_adds_nothing(monkeypatch):
    monkeypatch.setattr(oh, "find_opencode_executable", lambda: None)
    assert oh.private_server_argv() == []


def test_spec_wires_runtime_argv_and_model_format():
    spec = provider_spec.PROVIDER_REGISTRY["opencode"]
    assert spec.runtime_argv is provider_spec._opencode_runtime_argv
    assert re.fullmatch(spec.model_id_pattern, "opencode-go/deepseek-v4-pro")
    assert not re.fullmatch(spec.model_id_pattern, "deepseek-v4-pro")
    # still no invented effort option (#1052)
    assert spec.effort_flag is None
    assert provider_spec.effort_unsupported_reason("opencode", "opencode/x") == "provider"


def test_other_providers_have_no_runtime_argv():
    for name, spec in provider_spec.PROVIDER_REGISTRY.items():
        if name != "opencode":
            assert spec.runtime_argv is None and spec.model_id_pattern is None
