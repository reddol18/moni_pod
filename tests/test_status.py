import json

import pytest

from moni_pod import cli, status
from moni_pod.ledger import EXITED, Ledger, parse_iso
from tests.conftest import make_record

NOW = parse_iso("2026-10-06T12:00:00Z")


def live_pod(pod_id, st="RUNNING", cost=0.44, started="2026-10-06T11:00:00Z", vol=20, gpu=True, name=None):
    p = {"id": pod_id, "name": name or pod_id, "status": st, "cost": cost if st == "RUNNING" else 0.0,
         "startedAt": started, "mounts": {"persistent": {"size": vol, "path": "/workspace"}} if vol else {}}
    if gpu:
        p["gpu"] = {"id": "NVIDIA GeForce RTX 4090", "count": 1}
    else:
        p["cpu"] = {"id": "cpu3c", "vcpuCount": 2}
    return p


def managed(pod_id="pod-test-1", **kw):
    kw.setdefault("runs", [{"start": "2026-10-06T11:00:00Z"}])
    kw.setdefault("deadline", "2026-10-06T13:00:00Z")
    return make_record(pod_id=pod_id, **kw)


def test_managed_running_pod():
    rep = status.build(NOW, {"pod-test-1": managed(budget_usd=2.0)}, [live_pod("pod-test-1")], session_id="sess-A")
    (r,) = rep.rows
    assert r.managed and r.this_session
    assert r.elapsed_sec == 3600 and r.ttl_left_sec == 3600
    assert r.est_usd == pytest.approx(0.44)  # 1 h × 0.44
    assert r.notes == []
    t = rep.totals()
    assert t["running_count"] == 1 and t["rate_per_hr"] == pytest.approx(0.44)


def test_other_session_not_marked():
    rep = status.build(NOW, {"pod-test-1": managed()}, [live_pod("pod-test-1")], session_id="sess-B")
    assert not rep.rows[0].this_session


def test_ttl_failed_note():
    rec = managed(deadline="2026-10-06T11:50:00Z")  # 10 min past, beyond 5 min grace
    rep = status.build(NOW, {"pod-test-1": rec}, [live_pod("pod-test-1")])
    assert rep.rows[0].ttl_left_sec == -600
    assert any("auto-stop failed" in n for n in rep.rows[0].notes)


def test_ttl_within_grace_no_note():
    rec = managed(deadline="2026-10-06T11:58:00Z")
    rep = status.build(NOW, {"pod-test-1": rec}, [live_pod("pod-test-1")])
    assert not any("auto-stop failed" in n for n in rep.rows[0].notes)


def test_over_budget_note():
    rec = managed(cost_per_hr=3.0, budget_usd=2.0)  # 1 h × 3 = 3 > 2
    rep = status.build(NOW, {"pod-test-1": rec}, [live_pod("pod-test-1", cost=3.0)])
    assert any("over budget" in n for n in rep.rows[0].notes)


def test_unmanaged_pod_estimated_from_live():
    rep = status.build(NOW, {}, [live_pod("pod-other", cost=0.5, started="2026-10-06T10:00:00Z")])
    r = rep.rows[0]
    assert not r.managed and r.est_usd == pytest.approx(1.0)
    assert "not created by moni_pod" in r.notes


def test_stopped_pod_bills_disk():
    rec = managed(status=EXITED, runs=[{"start": "2026-10-06T10:00:00Z", "stop": "2026-10-06T11:00:00Z"}], deadline=None)
    rep = status.build(NOW, {"pod-test-1": rec}, [live_pod("pod-test-1", st="EXITED", vol=20)])
    r = rep.rows[0]
    assert r.rate_per_hr == 0
    assert r.stopped_disk_per_hr == pytest.approx(20 * 0.2 / 730)
    assert any("stopped 1h 0m: 20 GB disk has cost $0.005 so far" in n and "gpu-stop" in n for n in r.notes)
    assert rep.totals()["stopped_disk_per_day"] == pytest.approx(round(20 * 0.2 / 730 * 24, 4))


def test_sync_mismatch_reported_not_fixed():
    led = Ledger()
    led.upsert(managed())
    rep = status.build(NOW, led.load(), [live_pod("pod-test-1", st="EXITED")])
    assert any("sync pending" in n for n in rep.rows[0].notes)
    assert led.get("pod-test-1").status == "RUNNING"  # read-only: ledger untouched


def test_missing_on_runpod():
    rep = status.build(NOW, {"pod-test-1": managed()}, [])
    assert any("not found on RunPod" in n for n in rep.rows[0].notes)


def test_offline_view_has_no_sync_notes():
    rep = status.build(NOW, {"pod-test-1": managed()}, None)
    assert rep.rows[0].status == "RUNNING"
    assert rep.rows[0].notes == []


def test_closed_history_hidden():
    rec = managed(status="TERMINATED", closed=True, terminated_at="2026-10-06T11:30:00Z")
    assert status.build(NOW, {"pod-test-1": rec}, []).rows == []


def test_cpu_hardware_label():
    rep = status.build(NOW, {}, [live_pod("c1", gpu=False, vol=0)])
    assert rep.rows[0].hardware == "CPU cpu3c 2vCPU"


def test_render_text_contains_key_facts():
    rep = status.build(NOW, {"pod-test-1": managed()}, [live_pod("pod-test-1", name="my-pod")], "sess-A")
    rep.billed_today_usd = 0.12
    text = status.render_text(rep)
    assert "my-pod" in text and "1h00m" in text and "0.440" in text
    assert "billing history today" in text
    empty = status.render_text(status.build(NOW, {}, []))
    assert "No pods." in empty and "Network volumes are not tracked" in empty


def test_cli_offline_json(capsys):
    Ledger().upsert(managed())
    assert cli.main(["status", "--offline", "--json", "--session", "sess-A"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["rows"][0]["pod_id"] == "pod-test-1" and out["rows"][0]["this_session"] is True


def test_cli_without_key_falls_back(capsys):
    assert cli.main(["status"]) == 0
    assert "RUNPOD_API_KEY not found" in capsys.readouterr().out


def test_cli_with_fake_client(monkeypatch, capsys):
    class Fake:
        def __init__(self, key): pass
        def list_pods(self): return [live_pod("pod-x", name="fake")]
        def pod_billing(self, **kw): return {"records": [{"podId": "pod-x", "totalAmount": 0.3}]}
        def pod_system_log(self, pid): return []
        def account(self): return {"clientBalance": 14.5, "currentSpendPerHr": 0.9, "spendLimit": 80}
    monkeypatch.setenv("RUNPOD_API_KEY", "fake")
    monkeypatch.setattr(cli, "RunPodClient", Fake)
    assert cli.main(["status", "--billing"]) == 0
    out = capsys.readouterr().out
    assert "fake" in out and "$0.30" in out
    assert "balance $14.50" in out and "spend limit $80/h" in out
    assert "spending more than these pods explain" in out  # 0.9 vs 0.44 expected


SSH_DIRECT = {"proxy": None, "direct": {"host": "203.0.113.7", "port": 40123, "username": "root",
                                        "command": "ssh root@203.0.113.7 -p 40123"}}


def test_ssh_info_for_running_pod_in_json_and_text():
    """Issue 1: an agent must get the pod's SSH address without reading the API key."""
    pod = dict(live_pod("pod-test-1", name="my-pod"), ssh=SSH_DIRECT)
    rep = status.build(NOW, {"pod-test-1": managed()}, [pod])
    row = rep.to_dict()["rows"][0]
    assert (row["ssh_host"], row["ssh_port"], row["ssh_user"]) == ("203.0.113.7", 40123, "root")
    assert "ssh: ssh root@203.0.113.7 -p 40123" in status.render_text(rep)


@pytest.mark.parametrize("pod", [
    dict(live_pod("p"), ssh={"proxy": None, "direct": None}),   # still provisioning / no 22/tcp mapping yet
    live_pod("p"),                                              # no ssh block at all
    dict(live_pod("p", st="EXITED"), ssh=SSH_DIRECT),           # stopped: a stale address is not offered
])
def test_ssh_info_absent(pod):
    rep = status.build(NOW, {}, [pod])
    r = rep.rows[0]
    assert r.ssh_host is None and r.ssh_port is None and r.ssh_user is None
    assert "ssh:" not in status.render_text(rep)


def test_ssh_info_offline_is_none():
    rep = status.build(NOW, {"pod-test-1": managed()}, None)
    assert rep.rows[0].ssh_host is None


def test_extend_still_uses_shared_ssh_target():
    from moni_pod import extend
    assert extend.ssh_target({"ssh": SSH_DIRECT}) == ("203.0.113.7", 40123, "root")
    assert extend.ssh_target({"ssh": {"direct": {"host": "h", "port": "22"}}}) == ("h", 22, "root")


def test_expected_spend_includes_running_disk_and_stopped_disk():
    run = managed(running_disk_per_hr=0.007)
    stop = managed(pod_id="p2", status=EXITED, volume_gb=73, deadline=None,
                   runs=[{"start": "2026-10-06T10:00:00Z", "stop": "2026-10-06T11:00:00Z"}])
    rep = status.build(NOW, {"pod-test-1": run, "p2": stop},
                       [live_pod("pod-test-1"), live_pod("p2", st="EXITED", vol=73)])
    assert rep.expected_spend_per_hr == pytest.approx(0.44 + 0.007 + 73 * 0.2 / 730, abs=1e-4)
