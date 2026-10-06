import json

import pytest

from moni_pod import cli, sync
from moni_pod.ledger import EXITED, RUNNING, TERMINATED, Ledger, parse_iso
from tests.conftest import make_record

NOW = parse_iso("2026-10-06T02:20:00Z")


def rec(**kw):
    kw.setdefault("runs", [{"start": "2026-10-06T02:05:54Z"}])
    kw.setdefault("deadline", "2026-10-06T02:15:54Z")
    return make_record(**kw)


LOG = [{"source": "system", "line": "start container for img: begin", "ts": "2026-10-06T02:05:56Z"},
       {"source": "system", "line": "stop container abc", "ts": "2026-10-06T02:15:58Z"}]


def test_stop_time_from_system_log():
    ch = sync.plan({"pod-test-1": rec()}, [{"id": "pod-test-1", "status": "EXITED"}], NOW, lambda _: LOG)
    assert [(c.action, c.at, c.source) for c in ch] == [("stopped", "2026-10-06T02:15:58+00:00", "system log")]


def test_stop_time_falls_back_to_deadline_then_now():
    ch = sync.plan({"pod-test-1": rec()}, [{"id": "pod-test-1", "status": "EXITED"}], NOW, lambda _: [])
    assert (ch[0].at, ch[0].source) == ("2026-10-06T02:15:54+00:00", "TTL deadline")
    future = rec(deadline="2026-10-06T03:00:00Z")
    ch = sync.plan({"pod-test-1": future}, [{"id": "pod-test-1", "status": "EXITED"}], NOW, None)
    assert ch[0].source == "sync time"


def test_log_error_does_not_break_sync():
    def boom(_):
        raise OSError("down")
    ch = sync.plan({"pod-test-1": rec()}, [{"id": "pod-test-1", "status": "EXITED"}], NOW, boom)
    assert ch[0].source == "TTL deadline"


def test_started_outside_and_missing_and_noop():
    stopped = rec(status=EXITED, runs=[{"start": "2026-10-06T01:00:00Z", "stop": "2026-10-06T01:10:00Z"}])
    live = [{"id": "pod-test-1", "status": "RUNNING", "startedAt": "2026-10-06T02:10:00Z"},
            {"id": "same", "status": "RUNNING"}]
    pods = {"pod-test-1": stopped, "gone": rec(pod_id="gone"), "same": rec(pod_id="same"),
            "old": rec(pod_id="old", status=TERMINATED)}
    ch = {c.pod_id: c for c in sync.plan(pods, live, NOW)}
    assert ch["pod-test-1"].action == "started" and ch["pod-test-1"].at == "2026-10-06T02:10:00+00:00"
    assert ch["gone"].action == "terminated"
    assert "same" not in ch and "old" not in ch


def test_apply_updates_ledger_and_estimate():
    led = Ledger()
    led.upsert(rec(cost_per_hr=0.06, volume_gb=0))
    sync.apply(led, sync.plan(led.load(), [{"id": "pod-test-1", "status": "EXITED"}], NOW, lambda _: LOG))
    r = led.get("pod-test-1")
    assert r.status == EXITED and r.runs[-1].stop == "2026-10-06T02:15:58Z"


def test_status_cli_syncs_by_default(monkeypatch, capsys):
    Ledger().upsert(rec())

    class Fake:
        def __init__(self, key): pass
        def list_pods(self): return [{"id": "pod-test-1", "name": "t1", "status": "EXITED", "cost": 0}]
        def pod_system_log(self, pid): return LOG
        def account(self): return {}

    monkeypatch.setenv("RUNPOD_API_KEY", "fake")
    monkeypatch.setattr(cli, "RunPodClient", Fake)
    assert cli.main(["status"]) == 0
    assert "ledger synced: pod-test-1 stopped" in capsys.readouterr().out
    assert Ledger().get("pod-test-1").status == EXITED


def test_status_cli_no_sync(monkeypatch, capsys):
    Ledger().upsert(rec())

    class Fake:
        def __init__(self, key): pass
        def list_pods(self): return [{"id": "pod-test-1", "name": "t1", "status": "EXITED", "cost": 0}]
        def account(self): return {}

    monkeypatch.setenv("RUNPOD_API_KEY", "fake")
    monkeypatch.setattr(cli, "RunPodClient", Fake)
    assert cli.main(["status", "--no-sync"]) == 0
    assert "sync pending" in capsys.readouterr().out
    assert Ledger().get("pod-test-1").status == RUNNING
