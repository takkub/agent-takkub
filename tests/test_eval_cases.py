"""Eval cases store + runner (docs/architecture/eval-loop.md)."""

from __future__ import annotations

import sys

import pytest

from agent_takkub import eval_cases

PROJ = "evalproj"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(eval_cases, "RUNTIME_DIR", tmp_path)


def _check(code: int) -> str:
    return f'"{sys.executable}" -c "raise SystemExit({code})"'


def test_add_list_retire_keeps_case(tmp_path):
    a = eval_cases.add_case(PROJ, title="t1", expected="e", check=_check(0), cwd=str(tmp_path))
    b = eval_cases.add_case(PROJ, title="t2", expected="e", check=_check(0), cwd=str(tmp_path))
    assert (a["id"], b["id"]) == ("ev-001", "ev-002")
    eval_cases.retire_case(PROJ, "ev-001")
    assert [c["id"] for c in eval_cases.list_cases(PROJ)] == ["ev-002"]
    assert eval_cases.get_case(PROJ, "ev-001")["status"] == "retired"
    assert len(eval_cases.list_cases(PROJ, include_retired=True)) == 2


def test_add_requires_check_and_real_cwd(tmp_path):
    with pytest.raises(ValueError):
        eval_cases.add_case(PROJ, title="t", expected="", check=" ", cwd=str(tmp_path))
    with pytest.raises(ValueError):
        eval_cases.add_case(PROJ, title="t", expected="", check="x", cwd=str(tmp_path / "nope"))


def test_run_records_change_against_previous_run(tmp_path):
    eval_cases.add_case(PROJ, title="t", expected="", check=_check(0), cwd=str(tmp_path))
    outcomes = iter([True, True, False, True])

    def fake(case):
        ok = next(outcomes)
        return {
            "passed": ok,
            "exit": 0 if ok else 1,
            "timed_out": False,
            "duration_s": 0,
            "tail": "",
        }

    changes = [eval_cases.run_cases(PROJ, runner=fake)["results"][0]["change"] for _ in range(4)]
    assert changes == ["new", "same", "REGRESSED", "fixed"]
    runs = (tmp_path / "eval" / PROJ / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(runs) == 4


def test_real_check_exit_code_and_timeout(tmp_path):
    ok = eval_cases.run_check({"check": _check(0), "cwd": str(tmp_path), "timeout_s": 30})
    bad = eval_cases.run_check({"check": _check(3), "cwd": str(tmp_path), "timeout_s": 30})
    slow = eval_cases.run_check(
        {
            "check": f'"{sys.executable}" -c "import time; time.sleep(30)"',
            "cwd": str(tmp_path),
            "timeout_s": 1,
        }
    )
    assert ok["passed"] and ok["exit"] == 0
    assert not bad["passed"] and bad["exit"] == 3
    assert slow["timed_out"] and not slow["passed"] and slow["duration_s"] < 20


def test_draft_from_fix_loop_is_not_run_until_given_a_check(tmp_path):
    seqs = [{"identity": "ref:#7", "ref": "#7", "attempts": [{"summary": "boom in x.py"}]}]
    d = eval_cases.draft_from_fix_loop(PROJ, "ref:#7", seqs, "qa")
    assert d["status"] == "draft" and d["source"] == {"fix_loop": "ref:#7", "issue": "#7"}
    assert eval_cases.draft_from_fix_loop(PROJ, "ref:#7", seqs, "qa") is None  # one per identity
    assert eval_cases.draft_from_fix_loop(PROJ, "ref:#8", [{"attempts": []}], "qa") is None
    calls = []
    eval_cases.run_cases(PROJ, runner=lambda c: calls.append(c))
    assert calls == []
    assert (
        eval_cases.update_case(PROJ, d["id"], check=_check(0), cwd=str(tmp_path))["status"]
        == "active"
    )
