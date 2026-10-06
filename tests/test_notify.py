import json

import pytest

from moni_pod import cli, notify, status
from moni_pod.ledger import EXITED, Ledger, parse_iso
from moni_pod.settings import Settings
from tests.conftest import make_record

NOW = parse_iso("2026-10-08T12:00:00Z")


def seed():
    led = Ledger()
    led.upsert(make_record(pod_id="r1", name="runner", cost_per_hr=0.12, running_disk_per_hr=0.007,
                           runs=[{"start": "2026-10-08T11:30:00Z"}], deadline="2026-10-08T12:45:00Z"))
    led.upsert(make_record(pod_id="s1", name="old-stopped", status=EXITED, volume_gb=20, deadline=None,
                           runs=[{"start": "2026-10-06T10:00:00Z", "stop": "2026-10-06T12:00:00Z"}]))  # 48 h
    led.upsert(make_record(pod_id="s2", name="new-stopped", status=EXITED, volume_gb=20, deadline=None,
                           runs=[{"start": "2026-10-08T09:00:00Z", "stop": "2026-10-08T10:00:00Z"}]))  # 2 h
    led.upsert(make_record(pod_id="s3", name="no-volume", status=EXITED, volume_gb=0, deadline=None,
                           runs=[{"start": "2026-10-08T09:00:00Z", "stop": "2026-10-08T10:00:00Z"}]))
    led.upsert(make_record(pod_id="c1", name="closed", status="TERMINATED", closed=True))
    return led


def test_summarize_hand_values():
    lines = notify.summarize(seed(), NOW, Settings())
    texts = [x.text for x in lines]
    assert texts[0].startswith("runner: RUNNING $0.127/h, stops itself in 45 min")
    # 20 GB stopped: 0.0054795 $/h → $0.132/day; 48 h → $0.263
    assert texts[1] == ("!! old-stopped: stopped 2d 0h, 20 GB disk still billing $0.132/day ($0.263 so far) - "
                        "copy results off, then delete with /moni-pod:gpu-stop")
    assert texts[2].startswith("new-stopped: stopped 2h 0m") and "$0.011 so far" in texts[2]
    assert not any("no-volume" in t or "closed" in t for t in texts)
    assert notify.summarize(seed(), NOW, Settings(stale_stopped_hours=72))[1].stale is False


def test_session_start_message_for_user_and_claude():
    seed()
    out = notify.session_start({"source": "startup"}, now=NOW, settings=Settings())
    assert out["systemMessage"].startswith("moni_pod: 1 pod(s) running, 2 stopped pod(s) still billing disk")
    assert "/moni-pod:gpu-status" in out["systemMessage"]
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart" and "Never stop, delete or start" in ctx


def test_session_start_quiet_for_user_when_nothing_bills():
    out = notify.session_start({}, now=NOW, settings=Settings())
    assert "systemMessage" not in out and out["hookSpecificOutput"]["additionalContext"] == notify.LOOKUP_CONTEXT


def test_session_end_notifies_and_records(isolated_home):
    seed()
    sent = []
    r = notify.session_end({"session_id": "s", "reason": "other"}, now=NOW, settings=Settings(),
                           notifier=lambda t, b: sent.append((t, b)) or True)
    assert r == {"notified": True}
    assert sent[0][0] == "moni_pod: GPU still running" and "runner" in sent[0][1]
    rec = json.loads((isolated_home / "last_session_end.json").read_text())
    assert rec["reason"] == "other" and "runner" in rec["message"]


def test_session_end_nothing_to_say():
    called = []
    assert notify.session_end({}, now=NOW, settings=Settings(), notifier=lambda *a: called.append(a)) is None
    assert called == []


REAL_DESKTOP_NOTIFY = notify.desktop_notify  # captured at import, before the conftest guard patches it


def test_desktop_notify_windows_toast_sync(monkeypatch):
    import subprocess
    calls = []

    def run(*a, **k):
        calls.append((a, k))
        return subprocess.CompletedProcess(a, 0)
    monkeypatch.setattr(notify.sys, "platform", "win32")
    assert REAL_DESKTOP_NOTIFY("T", "B", run=run)
    args, kw = calls[0]
    assert args[0][0] == "powershell" and "ToastNotificationManager" in args[0][-1]
    assert kw["timeout"] <= 4.5  # fits the 5 s SessionEnd hook timeout
    assert kw["env"]["MONI_NOTIFY_BODY"] == "B"  # text passed via env, not on the command line


def test_desktop_notify_failure_is_swallowed(monkeypatch):
    def boom(*a, **k):
        raise OSError("no powershell")
    monkeypatch.setattr(notify.sys, "platform", "win32")
    assert REAL_DESKTOP_NOTIFY("T", "B", run=boom) is False


def test_statusline(monkeypatch):
    assert notify.statusline(now=NOW) == ""
    seed()
    line = notify.statusline(now=NOW)
    assert line.startswith("moni_pod: GPU 1 on $0.13/h, next stop 45m | 2 stopped $0.26/day (1 old!)")


def test_hook_cli_session_events(monkeypatch, capsys):
    import io
    seed()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"source": "startup"})))
    assert cli.main(["hook", "session-start"]) == 0
    assert "systemMessage" in json.loads(capsys.readouterr().out)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"reason": "other"})))
    assert cli.main(["hook", "session-end"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and "moni_pod:" in captured.err


def test_tests_cannot_reach_real_notifications():
    seed()
    calls = []
    import subprocess
    orig = subprocess.run
    try:
        subprocess.run = lambda *a, **k: calls.append(a) or orig(["cmd", "/c", "exit", "0"])
        notify.session_end({"reason": "other"}, now=NOW, settings=Settings())  # uses the patched notifier
    finally:
        subprocess.run = orig
    assert calls == []
