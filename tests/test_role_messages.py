"""Targeted tests for #277 — `takkub send` must survive a pane respawn, and
must be checkable after the fact.

The reported loss: a spec correction (the owner had changed their mind about
password storage) was sent, answered `ok: sent`, and disappeared with the pane
that respawned a moment later. The agent kept building the cancelled design.
In one session `backend` respawned 4 times and 3 messages vanished, with no
record anywhere that they had ever existed.

Two guarantees, both tested here:
  1. a message written into a session generation the pane has since left is
     re-delivered rather than lost — bounded, so a crash-looping pane can't
     turn one message into an endless re-paste;
  2. every message is readable back with its real delivery state, so "did this
     land?" is a lookup instead of a matter of trust.
"""

from __future__ import annotations

import pathlib

from agent_takkub import role_messages


def _append(tmp: pathlib.Path, **kw) -> str:
    base = {"to_role": "backend", "from_role": "lead", "body": "ใช้ bcrypt", "generation": 1}
    base.update(kw)
    return role_messages.append(tmp, "proj", **base)


class TestRecordAndRead:
    def test_append_then_read_round_trips(self, tmp_path: pathlib.Path) -> None:
        msg_id = _append(tmp_path)
        records = role_messages.read(tmp_path, "proj", role="backend")
        assert len(records) == 1
        assert records[0]["id"] == msg_id
        assert records[0]["state"] == "sent", "a written message is not a received one"

    def test_read_filters_by_role(self, tmp_path: pathlib.Path) -> None:
        _append(tmp_path, to_role="backend")
        _append(tmp_path, to_role="frontend")
        assert len(role_messages.read(tmp_path, "proj", role="frontend")) == 1
        assert len(role_messages.read(tmp_path, "proj")) == 2

    def test_corrupt_line_does_not_hide_the_rest(self, tmp_path: pathlib.Path) -> None:
        _append(tmp_path)
        path = tmp_path / "messages" / "proj.jsonl"
        path.write_text(path.read_text(encoding="utf-8") + "{not json\n", encoding="utf-8")
        assert len(role_messages.read(tmp_path, "proj")) == 1

    def test_missing_store_reads_empty(self, tmp_path: pathlib.Path) -> None:
        assert role_messages.read(tmp_path, "nothing-here") == []

    def test_per_role_cap_evicts_oldest_only_for_that_role(
        self, tmp_path: pathlib.Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(role_messages, "MAX_RECORDS_PER_ROLE", 3)
        for i in range(5):
            _append(tmp_path, to_role="backend", body=f"m{i}")
        _append(tmp_path, to_role="frontend", body="keep me")
        backend = role_messages.read(tmp_path, "proj", role="backend")
        assert [r["body"] for r in backend] == ["m2", "m3", "m4"]
        assert len(role_messages.read(tmp_path, "proj", role="frontend")) == 1


class TestRespawnDetection:
    def test_message_from_an_older_generation_is_pending(self, tmp_path: pathlib.Path) -> None:
        _append(tmp_path, generation=1)
        pending = role_messages.undelivered_for_generation(tmp_path, "proj", "backend", 2)
        assert len(pending) == 1, "a message written into a dead session is lost until replayed"

    def test_message_in_the_live_generation_is_left_alone(self, tmp_path: pathlib.Path) -> None:
        _append(tmp_path, generation=2)
        assert role_messages.undelivered_for_generation(tmp_path, "proj", "backend", 2) == []

    def test_delivered_message_is_never_replayed(self, tmp_path: pathlib.Path) -> None:
        msg_id = _append(tmp_path, generation=1)
        role_messages.mark_delivered(tmp_path, "proj", msg_id)
        assert role_messages.undelivered_for_generation(tmp_path, "proj", "backend", 5) == []

    def test_replay_cap_stops_an_endless_repaste(self, tmp_path: pathlib.Path) -> None:
        msg_id = _append(tmp_path, generation=1)
        for gen in range(2, 2 + role_messages.MAX_REPLAYS):
            assert role_messages.undelivered_for_generation(tmp_path, "proj", "backend", gen)
            role_messages.mark_replayed(tmp_path, "proj", msg_id, gen)
        assert role_messages.undelivered_for_generation(tmp_path, "proj", "backend", 99) == [], (
            "a pane that keeps dying must not be pasted the same message forever"
        )

    def test_abandoned_message_stays_readable(self, tmp_path: pathlib.Path) -> None:
        msg_id = _append(tmp_path)
        role_messages.mark_abandoned(tmp_path, "proj", msg_id, "replay cap reached")
        records = role_messages.read(tmp_path, "proj")
        assert records[0]["state"] == "abandoned"
        assert records[0]["abandoned_reason"] == "replay cap reached"


class TestCliRendering:
    def test_states_are_distinguishable_at_a_glance(self, tmp_path: pathlib.Path) -> None:
        delivered = _append(tmp_path, body="ถึงแล้วแน่นอน")
        role_messages.mark_delivered(tmp_path, "proj", delivered)
        _append(tmp_path, body="ยังไม่ยืนยัน")
        lines = "\n".join(role_messages.format_for_cli(role_messages.read(tmp_path, "proj")))
        assert "✅ ถึงแล้ว" in lines
        assert "⏳ ส่งแล้วยังไม่ยืนยัน" in lines
        assert "ถึงแล้วแน่นอน" in lines

    def test_replay_count_is_visible(self, tmp_path: pathlib.Path) -> None:
        msg_id = _append(tmp_path)
        role_messages.mark_replayed(tmp_path, "proj", msg_id, 2)
        lines = "\n".join(role_messages.format_for_cli(role_messages.read(tmp_path, "proj")))
        assert "ส่งซ้ำหลัง respawn 1x" in lines
