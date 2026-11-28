"""Eval cases store + runner (docs/architecture/eval-loop.md)."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

import pytest

from agent_takkub import eval_cases

PROJ = "evalproj"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(eval_cases, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(eval_cases, "_project_roots", lambda p: [tmp_path.resolve()])


def _check(code: int) -> str:
    return f'"{sys.executable}" -c "raise SystemExit({code})"'


def _run(ids=None, **kw):
    """run_cases with the token of the plan as it stands (what `--confirm` carries)."""
    token = eval_cases.plan_token(PROJ, eval_cases.plan_run(PROJ, ids)[0])
    return eval_cases.run_cases(PROJ, ids, confirm=token, **kw)


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

    changes = [_run(runner=fake)["results"][0]["change"] for _ in range(4)]
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
    _run(runner=lambda c: calls.append(c))
    assert calls == []
    assert (
        eval_cases.update_case(PROJ, d["id"], check=_check(0), cwd=str(tmp_path))["status"]
        == "active"
    )


# ── review-1 regressions (batch 2.2.63) ──────────────────────────────────


def test_empty_check_is_never_a_pass_s1(tmp_path):
    c = eval_cases.add_case(PROJ, title="t", expected="", check=_check(1), cwd=str(tmp_path))
    for blank in ("", "   "):
        with pytest.raises(ValueError):
            eval_cases.update_case(PROJ, c["id"], check=blank)
    assert eval_cases.get_case(PROJ, c["id"])["check"] == _check(1)
    assert not eval_cases.run_check({"check": " ", "cwd": str(tmp_path)})["passed"]


def test_ctrl_c_kills_the_check_tree_and_reraises_s2(tmp_path, monkeypatch):
    killed = []
    real_kill = eval_cases._kill_tree
    monkeypatch.setattr(eval_cases, "_kill_tree", lambda p: (killed.append(p.pid), real_kill(p)))
    orig_wait = subprocess.Popen.wait

    def wait(self, timeout=None):
        if not killed:
            raise KeyboardInterrupt
        return orig_wait(self, timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", wait)
    with pytest.raises(KeyboardInterrupt):
        eval_cases.run_check(
            {"check": f'"{sys.executable}" -c "import time; time.sleep(30)"', "cwd": str(tmp_path)}
        )
    assert killed


_ADDER = """
import sys
from pathlib import Path
from agent_takkub import eval_cases as ev
root = Path(sys.argv[1])
ev.RUNTIME_DIR = root
ev._project_roots = lambda p: [root.resolve()]
for i in range(15):
    ev.add_case("evalproj", title=f"{sys.argv[2]}-{i}", expected="", check="x", cwd=str(root))
"""


def test_concurrent_processes_lose_no_case_s3(tmp_path):
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    procs = [
        subprocess.Popen([sys.executable, "-c", _ADDER, str(tmp_path), f"w{k}"], env=env)
        for k in range(3)
    ]
    assert all(p.wait(timeout=120) == 0 for p in procs)
    cases = eval_cases.list_cases(PROJ)
    assert len(cases) == 45 and len({c["id"] for c in cases}) == 45


def test_cwd_outside_project_refused_s4(tmp_path, monkeypatch):
    inside = tmp_path / "proj"
    inside.mkdir()
    outside = tmp_path / "other"
    outside.mkdir()
    monkeypatch.setattr(eval_cases, "_project_roots", lambda p: [inside.resolve()])
    with pytest.raises(ValueError):
        eval_cases.add_case(PROJ, title="t", expected="", check=_check(0), cwd=str(outside))
    ok = eval_cases.add_case(PROJ, title="t", expected="", check=_check(0), cwd=str(inside))
    # a pane writes cases.json directly: outside cwd / no registered paths
    store = eval_cases._load(PROJ)
    store["cases"][ok["id"]]["cwd"] = str(outside)
    eval_cases._save(PROJ, store)
    seen = []
    out = _run(runner=lambda c: 1 / 0, announce=lambda t, r: seen.append((t, r)))
    assert out["results"] == [] and seen[0][0] == []
    assert "outside" in out["refused"][0]["reason"]
    monkeypatch.setattr(eval_cases, "_project_roots", lambda p: [])
    assert eval_cases.cwd_refusal(PROJ, str(inside))


def test_explicit_draft_or_retired_id_is_refused_with_reason_n2(tmp_path):
    seqs = [{"ref": "#7", "attempts": [{"summary": "boom"}]}]
    d = eval_cases.draft_from_fix_loop(PROJ, "ref:#7", seqs, "qa")
    r = eval_cases.add_case(PROJ, title="t", expected="", check=_check(0), cwd=str(tmp_path))
    eval_cases.retire_case(PROJ, r["id"])
    _, refused = eval_cases.plan_run(PROJ, [d["id"], r["id"]])
    reasons = {x["id"]: x["reason"] for x in refused}
    assert "draft" in reasons[d["id"]] and "cwd" in reasons[d["id"]]
    assert "retired" in reasons[r["id"]]


def test_bare_eval_command_lists_n1(monkeypatch):
    from agent_takkub import cli

    monkeypatch.setenv("TAKKUB_PROJECT", PROJ)
    assert cli.cmd_eval(argparse.Namespace(ev_cmd=None))["ok"]


def test_output_decoded_in_the_os_code_page_n6():
    assert eval_cases._decode_output("สวัสดี".encode()) == "สวัสดี"
    if sys.platform != "win32":
        return
    import ctypes

    cp = f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    for sample in ("สวัสดี", "café"):
        try:
            raw = sample.encode(cp)
        except UnicodeEncodeError:
            continue
        if raw != sample.encode():
            assert eval_cases._decode_output(raw) == sample
            return
    pytest.skip(f"no non-ASCII sample encodable in {cp}")


# ── review-2 regressions ─────────────────────────────────────────────────


def test_run_needs_the_token_of_the_plan_it_runs_s1(tmp_path):
    marker = tmp_path / "INJECTED.txt"
    c = eval_cases.add_case(PROJ, title="t", expected="", check=_check(0), cwd=str(tmp_path))
    token = eval_cases.plan_token(PROJ, eval_cases.plan_run(PROJ)[0])
    # Lead read that plan; a pane then rewrites the check in cases.json
    store = eval_cases._load(PROJ)
    store["cases"][c["id"]]["check"] = f"\"{sys.executable}\" -c \"open(r'{marker}','w')\""
    eval_cases._save(PROJ, store)
    for bad in (None, "", token):
        with pytest.raises(eval_cases.PlanChanged):
            eval_cases.run_cases(PROJ, confirm=bad)
    assert not marker.exists()
