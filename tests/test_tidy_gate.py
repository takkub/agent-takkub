"""Tests for tidy_gate.py — WARN-only repo tidiness check (#477).

Every test uses a real tmp git repo (git plumbing is cheap and is exactly
what `new_or_moved_files`/`learn_convention` are under test for) on a branch
literally named `main`, same pattern as test_prisma_gate.py.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_takkub import tidy_gate


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _commit_all(root: Path, msg: str = "commit") -> None:
    _git("add", ".", cwd=root)
    _git("commit", "-q", "-m", msg, cwd=root)


@pytest.fixture
def py_repo(tmp_path: Path) -> Path:
    """Established convention: python tests live under `tests/test_*.py`."""
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t.test", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "tests").mkdir()
    (root / "tests" / "test_foo.py").write_text("def test_x(): pass\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "foo.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(root, "init")
    return root


@pytest.fixture
def node_repo(tmp_path: Path) -> Path:
    """Established convention: node tests live under `__tests__/`."""
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t.test", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "package.json").write_text('{"name": "x"}', encoding="utf-8")
    (root / "__tests__").mkdir()
    (root / "__tests__" / "foo.test.js").write_text("test('x', () => {});\n", encoding="utf-8")
    _commit_all(root, "init")
    return root


@pytest.fixture
def bare_repo(tmp_path: Path) -> Path:
    """No tests at all yet — no established convention of any kind."""
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t.test", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "README.md").write_text("x", encoding="utf-8")
    _commit_all(root, "init")
    return root


# ---------------------------------------------------------------------------
# learn_convention / new_or_moved_files
# ---------------------------------------------------------------------------


def test_learn_convention_reads_existing_python_layout(py_repo: Path) -> None:
    convention = tidy_gate.learn_convention(py_repo)
    assert convention.py_shapes == {"tests-root"}
    assert convention.node_shapes == set()


def test_learn_convention_reads_existing_node_layout(node_repo: Path) -> None:
    convention = tidy_gate.learn_convention(node_repo)
    assert convention.node_shapes == {"__tests__"}


def test_learn_convention_empty_when_no_tests_exist(bare_repo: Path) -> None:
    convention = tidy_gate.learn_convention(bare_repo)
    assert convention.py_shapes == set()
    assert convention.node_shapes == set()


def test_new_or_moved_files_excludes_files_only_modified_in_place(py_repo: Path) -> None:
    (py_repo / "tests" / "test_foo.py").write_text("def test_x(): assert True\n", encoding="utf-8")
    assert tidy_gate.new_or_moved_files(py_repo) == []


def test_new_or_moved_files_includes_untracked(py_repo: Path) -> None:
    (py_repo / "src" / "test_bar.py").write_text("def test_y(): pass\n", encoding="utf-8")
    assert "src/test_bar.py" in tidy_gate.new_or_moved_files(py_repo)


# ---------------------------------------------------------------------------
# find_tidy_warnings — placement
# ---------------------------------------------------------------------------


def test_colocated_python_test_warns_against_tests_root_convention(py_repo: Path) -> None:
    (py_repo / "src" / "test_bar.py").write_text("def test_y(): pass\n", encoding="utf-8")
    warnings = tidy_gate.find_tidy_warnings(py_repo)
    assert any("src/test_bar.py" in w for w in warnings)


def test_tests_root_python_test_matches_convention_no_warning(py_repo: Path) -> None:
    (py_repo / "tests" / "test_bar.py").write_text("def test_y(): pass\n", encoding="utf-8")
    assert tidy_gate.find_tidy_warnings(py_repo) == []


def test_dunder_tests_node_file_matches_convention_no_warning(node_repo: Path) -> None:
    (node_repo / "__tests__" / "bar.test.js").write_text("test('y', () => {});\n", encoding="utf-8")
    assert tidy_gate.find_tidy_warnings(node_repo) == []


def test_colocated_node_test_warns_against_dunder_tests_convention(node_repo: Path) -> None:
    (node_repo / "src").mkdir()
    (node_repo / "src" / "bar.test.js").write_text("test('y', () => {});\n", encoding="utf-8")
    warnings = tidy_gate.find_tidy_warnings(node_repo)
    assert any("src/bar.test.js" in w for w in warnings)


def test_no_convention_no_placement_warning(bare_repo: Path) -> None:
    (bare_repo / "src").mkdir()
    (bare_repo / "src" / "test_anything.py").write_text("def test_z(): pass\n", encoding="utf-8")
    assert tidy_gate.find_tidy_warnings(bare_repo) == []


def test_e2e_dir_exempt_from_placement_check(py_repo: Path) -> None:
    e2e = py_repo / "tests" / "e2e"
    e2e.mkdir()
    (e2e / "test_flow.py").write_text("def test_flow(): pass\n", encoding="utf-8")
    assert tidy_gate.find_tidy_warnings(py_repo) == []


# ---------------------------------------------------------------------------
# find_tidy_warnings — scratch files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "debug_dump.py",
        "tmp_notes.txt",
        "scratch_ideas.md",
        "test.js",
        "test.ts",
        "test.py",
        "server.log",
        "patch.orig",
        "patch.rej",
        "config.bak",
        "screenshot.png",
        ".env.local",
    ],
)
def test_scratch_files_warn(bare_repo: Path, path: str) -> None:
    (bare_repo / path).write_text("x", encoding="utf-8")
    warnings = tidy_gate.find_tidy_warnings(bare_repo)
    assert any(path in w for w in warnings), warnings


def test_image_inside_allowed_dir_does_not_warn(bare_repo: Path) -> None:
    (bare_repo / "docs").mkdir()
    (bare_repo / "docs" / "diagram.png").write_text("x", encoding="utf-8")
    assert tidy_gate.find_tidy_warnings(bare_repo) == []


def test_existing_env_file_modified_in_place_does_not_warn(bare_repo: Path) -> None:
    (bare_repo / ".env.local").write_text("A=1\n", encoding="utf-8")
    _commit_all(bare_repo, "add env")
    (bare_repo / ".env.local").write_text("A=2\n", encoding="utf-8")
    assert tidy_gate.find_tidy_warnings(bare_repo) == []


def test_nothing_in_diff_is_clean(bare_repo: Path) -> None:
    assert tidy_gate.find_tidy_warnings(bare_repo) == []


# ---------------------------------------------------------------------------
# check_tidy — env-var modes
# ---------------------------------------------------------------------------


def test_check_tidy_default_warns_not_fails(bare_repo: Path) -> None:
    (bare_repo / "debug_dump.py").write_text("x", encoding="utf-8")
    finding = tidy_gate.check_tidy(bare_repo, {})
    assert finding.ok is True
    assert finding.warn is True
    assert finding.skipped is False
    assert "debug_dump.py" in finding.detail


def test_check_tidy_clean_tree_no_warn(bare_repo: Path) -> None:
    finding = tidy_gate.check_tidy(bare_repo, {})
    assert finding.ok is True
    assert finding.warn is False


def test_check_tidy_strict_fails(bare_repo: Path) -> None:
    (bare_repo / "debug_dump.py").write_text("x", encoding="utf-8")
    finding = tidy_gate.check_tidy(bare_repo, {"TAKKUB_QA_TIDY": "strict"})
    assert finding.ok is False
    assert finding.warn is False


def test_check_tidy_disabled_by_env(bare_repo: Path) -> None:
    (bare_repo / "debug_dump.py").write_text("x", encoding="utf-8")
    finding = tidy_gate.check_tidy(bare_repo, {"TAKKUB_QA_TIDY": "0"})
    assert finding.skipped is True
    assert finding.ok is True
    assert finding.warn is False


def test_check_tidy_lists_at_most_ten_files(bare_repo: Path) -> None:
    for i in range(15):
        (bare_repo / f"debug_{i}.py").write_text("x", encoding="utf-8")
    finding = tidy_gate.check_tidy(bare_repo, {})
    assert finding.warn is True
    assert "+5" in finding.detail
