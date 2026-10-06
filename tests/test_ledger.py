import json
import os
import time

import pytest

from moni_pod.ledger import EXITED, RUNNING, TERMINATED, Ledger, LedgerLockTimeout, parse_iso
from tests.conftest import make_record


def test_empty_ledger(isolated_home):
    assert Ledger().load() == {}
    assert Ledger().path.parent == isolated_home


def test_roundtrip_and_schema():
    led = Ledger()
    rec = make_record(runs=[{"start": "2026-10-06T10:00:00Z"}], budget_usd=2.0)
    led.upsert(rec)
    got = led.get("pod-test-1")
    assert got == rec
    raw = json.loads(led.path.read_text(encoding="utf-8"))
    assert raw["version"] == 1 and "pod-test-1" in raw["pods"]


def test_unknown_fields_are_ignored(isolated_home):
    isolated_home.mkdir(parents=True)
    rec = make_record().to_dict()
    rec["future_field"] = 1
    (isolated_home / "ledger.json").write_text(json.dumps({"version": 1, "pods": {"pod-test-1": rec}}))
    assert Ledger().get("pod-test-1").name == "t1"


def test_lifecycle_transitions():
    led = Ledger()
    led.upsert(make_record(runs=[]))
    rec = led.mark_started("pod-test-1", 600, at=parse_iso("2026-10-06T10:00:00Z"))
    assert rec.status == RUNNING and rec.deadline == "2026-10-06T10:10:00Z"
    # starting twice does not open a second run
    led.mark_started("pod-test-1", 600, at=parse_iso("2026-10-06T10:01:00Z"))
    assert len(led.get("pod-test-1").runs) == 1

    rec = led.mark_stopped("pod-test-1", at=parse_iso("2026-10-06T10:08:00Z"))
    assert rec.status == EXITED and rec.deadline is None
    assert rec.runs[-1].stop == "2026-10-06T10:08:00Z"
    led.mark_stopped("pod-test-1", at=parse_iso("2026-10-06T11:00:00Z"))  # idempotent
    assert led.get("pod-test-1").runs[-1].stop == "2026-10-06T10:08:00Z"

    led.mark_started("pod-test-1", 600, at=parse_iso("2026-10-06T12:00:00Z"))
    rec = led.mark_terminated("pod-test-1", at=parse_iso("2026-10-06T12:05:00Z"))
    assert rec.status == TERMINATED and rec.terminated_at == "2026-10-06T12:05:00Z"
    assert [r.stop for r in rec.runs] == ["2026-10-06T10:08:00Z", "2026-10-06T12:05:00Z"]


def test_no_temp_file_left():
    led = Ledger()
    led.upsert(make_record())
    assert sorted(p.name for p in led.home.iterdir()) == ["ledger.json"]


def test_lock_blocks_then_times_out():
    led = Ledger()
    led.home.mkdir(parents=True, exist_ok=True)
    led.lock_path.write_text("")
    with pytest.raises(LedgerLockTimeout):
        with led._locked(timeout=0.2):
            pass


def test_stale_lock_is_broken():
    led = Ledger()
    led.home.mkdir(parents=True, exist_ok=True)
    led.lock_path.write_text("")
    old = time.time() - 120
    os.utime(led.lock_path, (old, old))
    led.upsert(make_record())  # does not raise
    assert not led.lock_path.exists()
