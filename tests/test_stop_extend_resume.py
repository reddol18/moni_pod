import subprocess

import pytest

from moni_pod import extend, guard, start, stop
from moni_pod.ledger import EXITED, RUNNING, TERMINATED, Ledger, parse_iso
from moni_pod.settings import Settings
from tests.conftest import make_record

NOW = parse_iso("2026-10-06T11:00:00Z")
S = Settings()


class FakeClient:
    def __init__(self, status="RUNNING", billed=0.0, ssh=True, env=None):
        self.status, self.billed, self.ssh, self.env = status, billed, ssh, env or {"MONI_POD_TTL_SEC": "3600"}
        self.actions, self.patches = [], []

    def get_pod(self, pid):
        p = {"id": pid, "status": self.status, "env": dict(self.env)}
        if self.ssh:
            p["ssh"] = {"direct": {"host": "203.0.113.5", "port": 22022, "username": "root"}}
        return p

    def pod_action(self, pid, action):
        self.actions.append(action)
        if action == "stop":
            self.status = "EXITED"
        return None if action == "terminate" else {"id": pid, "status": "RUNNING" if action == "start" else self.status}

    def update_pod(self, pid, body):
        self.patches.append(body)
        return {}

    def pod_billing(self, **kw):
        return {"records": [{"podId": kw["pod_id"], "totalAmount": self.billed}]}


def running(**kw):
    kw.setdefault("runs", [{"start": "2026-10-06T10:00:00Z"}])
    kw.setdefault("deadline", "2026-10-06T12:00:00Z")
    kw.setdefault("cost_per_hr", 0.12)
    kw.setdefault("volume_gb", 20)
    kw.setdefault("budget_usd", 2.0)
    return make_record(**kw)


# ---- stop / terminate ---------------------------------------------------------------

def test_stop_needs_no_token_and_settles_pending():
    Ledger().upsert(running())
    c = FakeClient()
    s = stop.stop_pod("pod-test-1", action="stop", session_id="sess-A", client=c, retrieved=None, now=NOW)
    assert c.actions == ["stop"]
    rec = Ledger().get("pod-test-1")
    assert rec.status == EXITED and rec.runs[-1].stop == "2026-10-06T11:00:00Z"
    assert s.estimate_usd == pytest.approx(0.12) and s.billed_usd is None and not s.closed
    assert s.stopped_disk_per_day == pytest.approx(20 * 0.2 / 730 * 24, abs=1e-6)
    assert "not posted yet" in s.note


def test_terminate_requires_retrieval_answer_then_token():
    Ledger().upsert(running())
    c = FakeClient()
    with pytest.raises(stop.StopRefused, match="copied off"):
        stop.stop_pod("pod-test-1", action="terminate", session_id="sess-A", client=c, retrieved=None, now=NOW)
    with pytest.raises(stop.StopRefused, match="not retrieved"):
        stop.stop_pod("pod-test-1", action="terminate", session_id="sess-A", client=c, retrieved=False, now=NOW)
    with pytest.raises(guard.TokenError):
        stop.stop_pod("pod-test-1", action="terminate", session_id="sess-A", client=c, retrieved=True, now=NOW)
    assert c.actions == []  # nothing deleted


def test_terminate_with_token_billing_provisional_then_closed():
    Ledger().upsert(running())
    guard.mint("stop", "sess-A", now=NOW)
    c = FakeClient(billed=0.125)
    s = stop.stop_pod("pod-test-1", action="terminate", session_id="sess-A", client=c, retrieved=True, now=NOW)
    assert c.actions == ["terminate"]
    rec = Ledger().get("pod-test-1")
    assert rec.status == TERMINATED and rec.retrieved is True and rec.billed_usd == 0.125
    assert rec.closed  # 0.125 ≥ 95 % of the 0.12 estimate: billing caught up
    assert s.diff_usd == pytest.approx(0.005)


def test_discard_unretrieved_is_explicit():
    Ledger().upsert(running())
    guard.mint("stop", "sess-A", now=NOW)
    stop.stop_pod("pod-test-1", action="terminate", session_id="sess-A", client=FakeClient(),
                  retrieved=False, discard_unretrieved=True, now=NOW)
    assert Ledger().get("pod-test-1").retrieved is False


def test_unknown_pod_refused():
    with pytest.raises(stop.StopRefused, match="not in the moni_pod ledger"):
        stop.stop_pod("other", action="stop", session_id="s", client=FakeClient(), retrieved=None, now=NOW)


def deleted_pod():
    # $0.12/h for 30 min → estimate $0.06; deleted 10:30
    Ledger().upsert(running(status=TERMINATED, terminated_at="2026-10-06T10:30:00Z", volume_gb=0,
                            runs=[{"start": "2026-10-06T10:00:00Z", "stop": "2026-10-06T10:30:00Z"}]))


def test_settle_not_posted_stays_open():
    deleted_pod()
    out = stop.reconcile(client=FakeClient(billed=0.0), now=NOW)
    assert out[0].billed_usd is None and not out[0].closed and "not posted" in out[0].note


def test_settle_partial_billing_stays_open_and_provisional():
    deleted_pod()
    out = stop.reconcile(client=FakeClient(billed=0.0163), now=NOW)  # the M5 case: ~27 % posted
    assert not out[0].closed and "provisional (27% of estimate" in out[0].note
    out = stop.reconcile(client=FakeClient(billed=0.056), now=NOW)  # 93 %: still under 95 %
    assert not out[0].closed


def test_settle_closes_when_billing_reaches_95_percent():
    deleted_pod()
    out = stop.reconcile(client=FakeClient(billed=0.057), now=NOW)  # exactly 95 % of 0.06
    assert out[0].closed and out[0].note == "" and Ledger().get("pod-test-1").closed
    assert stop.reconcile(client=FakeClient(billed=0.057), now=NOW) == []  # closed records skipped


def test_settle_24h_cap_closes_with_warning():
    deleted_pod()
    day_later = parse_iso("2026-10-07T10:30:00Z")
    out = stop.reconcile(client=FakeClient(billed=0.03), now=day_later)
    assert out[0].closed and out[0].note.startswith("WARNING: closed after 24 h") and "50% of the estimate" in out[0].note
    deleted_pod()
    Ledger().update("pod-test-1", closed=False)
    out = stop.reconcile(client=FakeClient(billed=0.0), now=day_later)
    assert out[0].closed and "no charge" in out[0].note


def test_settle_just_before_cap_stays_open():
    deleted_pod()
    out = stop.reconcile(client=FakeClient(billed=0.03), now=parse_iso("2026-10-07T10:29:00Z"))
    assert not out[0].closed


def test_stopped_pod_never_closes():
    Ledger().upsert(running(status=EXITED, runs=[{"start": "2026-10-06T10:00:00Z", "stop": "2026-10-06T10:30:00Z"}]))
    out = stop.reconcile(client=FakeClient(billed=1.0), now=parse_iso("2026-10-09T00:00:00Z"))
    assert not out[0].closed  # still billing disk


# ---- extend ----------------------------------------------------------------------------

def ok_ssh(epoch):
    calls = []

    def ssh(host, port, user, cmd):
        calls.append((host, port, user, cmd))
        return subprocess.CompletedProcess([], 0, stdout=f"{epoch}\n", stderr="")
    return ssh, calls


def test_extend_moves_deadline_over_ssh():
    Ledger().upsert(running(ttl_sec=7200))
    guard.mint("extend", "sess-A", now=NOW)
    new_epoch = int(parse_iso("2026-10-06T13:00:00Z").timestamp())
    ssh, calls = ok_ssh(new_epoch)
    r = extend.extend("pod-test-1", 1, session_id="sess-A", client=FakeClient(), settings=S, now=NOW, ssh=ssh)
    assert calls[0][:3] == ("203.0.113.5", 22022, "root") and "+ 3600" in calls[0][3]
    assert r.new_deadline == "2026-10-06T13:00:00Z" and r.extra_max_usd == pytest.approx(0.12)
    rec = Ledger().get("pod-test-1")
    assert rec.deadline == "2026-10-06T13:00:00Z" and rec.ttl_sec == 10800
    assert guard.find_valid("extend", "sess-A", now=NOW) is None  # consumed


def test_extend_needs_user_token():
    Ledger().upsert(running())
    with pytest.raises(guard.TokenError):
        extend.extend("pod-test-1", 1, session_id="sess-A", client=FakeClient(), settings=S, now=NOW, ssh=ok_ssh(1)[0])


def test_extend_cap_and_budget():
    Ledger().upsert(running())  # run 10:00 → deadline 12:00 = 2 h
    guard.mint("extend", "sess-A", now=NOW)
    with pytest.raises(extend.ExtendRefused, match="TTL cap"):
        extend.extend("pod-test-1", 6.5, session_id="sess-A", client=FakeClient(), settings=S, now=NOW, ssh=ok_ssh(1)[0])
    Ledger().update("pod-test-1", cost_per_hr=1.0)
    with pytest.raises(extend.ExtendRefused, match="budget"):
        extend.extend("pod-test-1", 2, session_id="sess-A", client=FakeClient(), settings=S, now=NOW, ssh=ok_ssh(1)[0])
    assert guard.find_valid("extend", "sess-A", now=NOW) is not None  # refusals keep the token


def test_extend_without_ssh_or_failed_ssh_keeps_token():
    Ledger().upsert(running())
    guard.mint("extend", "sess-A", now=NOW)
    with pytest.raises(extend.ExtendRefused, match="no direct SSH"):
        extend.extend("pod-test-1", 1, session_id="sess-A", client=FakeClient(ssh=False), settings=S, now=NOW)

    def bad(*a):
        return subprocess.CompletedProcess([], 255, stdout="", stderr="Permission denied (publickey)")
    with pytest.raises(extend.ExtendRefused, match="Permission denied"):
        extend.extend("pod-test-1", 1, session_id="sess-A", client=FakeClient(), settings=S, now=NOW, ssh=bad)
    assert guard.find_valid("extend", "sess-A", now=NOW) is not None
    assert Ledger().get("pod-test-1").deadline == "2026-10-06T12:00:00Z"


def test_extend_stopped_pod_refused():
    Ledger().upsert(running(status=EXITED, runs=[{"start": "2026-10-06T10:00:00Z", "stop": "2026-10-06T10:30:00Z"}]))
    guard.mint("extend", "sess-A", now=NOW)
    with pytest.raises(extend.ExtendRefused, match="--resume"):
        extend.extend("pod-test-1", 1, session_id="sess-A", client=FakeClient(), settings=S, now=NOW)


# ---- resume ----------------------------------------------------------------------------

def stopped(**kw):
    return running(status=EXITED, deadline=None, ttl_sec=3600,
                   runs=[{"start": "2026-10-06T09:00:00Z", "stop": "2026-10-06T10:00:00Z"}], **kw)


def test_resume_plan_and_execute_same_ttl():
    Ledger().upsert(stopped())
    plan = start.plan_resume("pod-test-1", hours=None, session_id="sess-A", settings=S, now=NOW)
    assert plan.ok and plan.hours == 1 and plan.max_cost_usd == pytest.approx(0.12)
    assert any("same host" in w for w in plan.warnings)
    c = FakeClient(status="EXITED")
    with pytest.raises(guard.TokenError):
        start.execute_resume(plan, client=c, session_id="sess-A", now=NOW)
    guard.mint("start", "sess-A", now=NOW)
    start.execute_resume(plan, client=c, session_id="sess-A", now=NOW)
    assert c.actions == ["start"] and c.patches == []
    rec = Ledger().get("pod-test-1")
    assert rec.status == RUNNING and len(rec.runs) == 2 and rec.deadline == "2026-10-06T12:00:00Z"


def test_resume_with_new_ttl_patches_env_first():
    Ledger().upsert(stopped())
    guard.mint("start", "sess-A", now=NOW)
    c = FakeClient(status="EXITED", env={"MONI_POD_TTL_SEC": "3600", "A": "1"})
    plan = start.plan_resume("pod-test-1", hours=0.5, session_id="sess-A", settings=S, now=NOW)
    start.execute_resume(plan, client=c, session_id="sess-A", now=NOW)
    assert c.patches == [{"env": {"MONI_POD_TTL_SEC": "1800", "A": "1"}}] and c.actions == ["start"]


def test_resume_refusals():
    Ledger().upsert(running())
    assert not start.plan_resume("pod-test-1", hours=1, session_id="sess-A", settings=S, now=NOW).ok
    Ledger().upsert(stopped(pod_id="p2", cost_per_hr=1.0))
    plan = start.plan_resume("p2", hours=3, session_id="sess-A", settings=S, now=NOW)
    assert any("budget" in p for p in plan.problems)
    with pytest.raises(start.StartRefused):
        start.plan_resume("nope", hours=1, session_id="sess-A", settings=S, now=NOW)


def test_ssh_binary_choice(monkeypatch):
    monkeypatch.setenv("MONI_POD_SSH", "/opt/ssh")
    assert extend.ssh_binary() == "/opt/ssh"
    monkeypatch.delenv("MONI_POD_SSH")
    monkeypatch.setattr(extend.sys, "platform", "linux")
    assert extend.ssh_binary() == "ssh"
    monkeypatch.setattr(extend.sys, "platform", "win32")
    monkeypatch.setattr(extend.os.path, "exists", lambda p: p == extend.WINDOWS_OPENSSH)
    assert extend.ssh_binary() == extend.WINDOWS_OPENSSH
