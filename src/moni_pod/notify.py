"""Session warnings (M5, PLAN §3 "warn only, never act").

- SessionEnd: Claude Code discards its JSON output and gives it a 1.5 s budget, and the screen is closing, so the
  warning goes to the desktop: a detached OS notification (Windows balloon via PowerShell; notify-send/osascript
  elsewhere). It also writes ~/.moni_pod/last_session_end.json.
- SessionStart: a `systemMessage` the user sees + `additionalContext` for Claude, listing running pods and stopped
  pods that still bill disk (days stopped, $ so far, $/day; highlighted after `stale_stopped_hours`).
Ledger only (no network): fast and works offline. Nothing is stopped or deleted here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime

from . import config, cost
from .fsutil import write_json_atomic
from .ledger import EXITED, RUNNING, Ledger, parse_iso, to_iso, utcnow
from .settings import Settings


LOOKUP_CONTEXT = ("moni_pod is installed. For RunPod GPU stock, prices or which GPU to pick, invoke the Skill "
                  "'moni-pod:gpu-list' (read-only, no approval). Start/stop/extend pods only through the user's "
                  "/moni-pod:gpu-start, /moni-pod:gpu-stop, /moni-pod:gpu-extend. Never read or use the RunPod API key.")

def ssh_hint() -> str:
    """Issue 1: without a supported way to get a pod's SSH address, an agent reads the key and calls the API."""
    return ("For a running pod's SSH address (ssh_host/ssh_port/ssh_user), run "
            f'`uv run --quiet --project "{config.PACKAGE_ROOT.as_posix()}" moni-pod status --json` (read-only).')


WINDOWS_POWERSHELL_AUMID =r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"


@dataclass
class PodLine:
    name: str
    state: str  # "running" | "stopped"
    text: str
    stale: bool = False


def summarize(ledger: Ledger, now: datetime, settings: Settings) -> list[PodLine]:
    lines: list[PodLine] = []
    for rec in ledger.load().values():
        if rec.closed:
            continue
        if rec.status == RUNNING:
            dl = parse_iso(rec.deadline)
            left = f"stops itself in {max(0, int((dl - now).total_seconds() // 60))} min" if dl else "no auto-stop recorded"
            rate = rec.cost_per_hr + rec.running_disk_per_hr
            est = cost.estimate(rec, now).total_usd
            lines.append(PodLine(rec.name, "running", f"{rec.name}: RUNNING ${rate:.3f}/h, {left}, ~${est:.2f} so far"))
        elif rec.status == EXITED and rec.volume_gb:
            stopped_at = parse_iso(rec.runs[-1].stop) if rec.runs and rec.runs[-1].stop else None
            hours = (now - stopped_at).total_seconds() / 3600 if stopped_at else 0.0
            per_day = cost.stopped_disk_rate(rec.volume_gb) * 24
            so_far = cost.stopped_disk_rate(rec.volume_gb) * hours
            stale = hours >= settings.stale_stopped_hours
            age = f"{int(hours // 24)}d {int(hours % 24)}h" if hours >= 24 else f"{int(hours)}h {int(hours * 60 % 60)}m"
            lines.append(PodLine(rec.name, "stopped",
                                 f"{'!! ' if stale else ''}{rec.name}: stopped {age}, {rec.volume_gb} GB disk "
                                 f"still billing ${per_day:.3f}/day (${so_far:.3f} so far) - copy results off, "
                                 f"then delete with /moni-pod:gpu-stop", stale))
    lines.sort(key=lambda x: (x.state != "running", not x.stale, x.name))
    return lines


def _message(lines: list[PodLine]) -> str:
    run = sum(1 for x in lines if x.state == "running")
    stop = sum(1 for x in lines if x.state == "stopped")
    head = "moni_pod: " + ", ".join(p for p in (f"{run} pod(s) running" if run else "",
                                                 f"{stop} stopped pod(s) still billing disk" if stop else "") if p)
    return head + "\n" + "\n".join("  " + x.text for x in lines)


def session_start(event: dict, now: datetime | None = None, settings: Settings | None = None) -> dict | None:
    from . import settings as settings_mod
    lines = summarize(Ledger(), now or utcnow(), settings or settings_mod.load())
    if not lines:
        # Task 0002: an agent that cannot find a lookup path reaches for runpodctl or the raw API with the key.
        return {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                       "additionalContext": LOOKUP_CONTEXT + " " + ssh_hint()}}
    msg = _message(lines)
    return {"systemMessage": msg + "\n  Live numbers: /moni-pod:gpu-status",
            "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext":
                                   msg + "\nTell the user about these pods if relevant. Never stop, delete or "
                                         "start pods yourself; point the user to /moni-pod:gpu-status, "
                                         "/moni-pod:gpu-stop or /moni-pod:gpu-extend. For GPU, price or "
                                         "stock lookups use /moni-pod:gpu-list, never the API key. "
                                         + ssh_hint()}}


def desktop_notify(title: str, body: str, run=subprocess.run, timeout: float = 4.0) -> bool:
    """OS notification, run synchronously within the SessionEnd hook's timeout (5 s in hooks.json).

    M5: a detached child was killed with the hook's process tree on Windows (the toast never appeared);
    the same command run in the foreground showed it. A toast stays in Action Center if the screen is gone.
    """
    env = dict(os.environ, MONI_NOTIFY_TITLE=title, MONI_NOTIFY_BODY=body[:240])
    try:
        if sys.platform == "win32":
            # Windows 10+ toast (stays in Action Center if missed). M5: a NotifyIcon balloon from a detached
            # process did not show for the user; this toast did.
            ps = ("$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
                  "ContentType = WindowsRuntime]; "
                  "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
                  "[Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
                  "$t = $x.GetElementsByTagName('text'); "
                  "$null = $t.Item(0).AppendChild($x.CreateTextNode($env:MONI_NOTIFY_TITLE)); "
                  "$null = $t.Item(1).AppendChild($x.CreateTextNode($env:MONI_NOTIFY_BODY)); "
                  "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
                  f"'{WINDOWS_POWERSHELL_AUMID}').Show([Windows.UI.Notifications.ToastNotification]::new($x))")
            r = run(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps],
                    env=env, creationflags=0x08000000,  # CREATE_NO_WINDOW
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
            return r.returncode == 0
        elif sys.platform == "darwin":
            r = run(["osascript", "-e", 'display notification (system attribute "MONI_NOTIFY_BODY") '
                                        'with title (system attribute "MONI_NOTIFY_TITLE")'],
                    env=env, capture_output=True, timeout=timeout)
        else:
            r = run(["notify-send", title, body[:240]], capture_output=True, timeout=timeout)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def session_end(event: dict, now: datetime | None = None, settings: Settings | None = None,
                notifier=None) -> dict | None:
    from . import settings as settings_mod
    now = now or utcnow()
    lines = summarize(Ledger(), now, settings or settings_mod.load())
    if not lines:
        return None
    msg = _message(lines)
    home = config.home_dir()
    home.mkdir(parents=True, exist_ok=True)
    write_json_atomic(home / "last_session_end.json",
                      {"at": to_iso(now), "session_id": event.get("session_id"), "reason": event.get("reason"),
                       "message": msg})
    run = [x for x in lines if x.state == "running"]
    title = "moni_pod: GPU still running" if run else "moni_pod: stopped pods still cost disk"
    notifier = notifier or desktop_notify  # resolved at call time (patchable; a default arg is bound once)
    sent = notifier(title, "\n".join(x.text for x in lines[:3]) + ("\n..." if len(lines) > 3 else ""))
    sys.stderr.write(msg + "\n")  # shown to the user if the terminal is still there
    return {"notified": sent}


def statusline(now: datetime | None = None) -> str:
    """One short line for the Claude Code status line (ledger only; empty when nothing bills)."""
    from . import settings as settings_mod
    now = now or utcnow()
    lines = summarize(Ledger(), now, settings_mod.load())
    if not lines:
        return ""
    led = Ledger().load()
    run = [r for r in led.values() if not r.closed and r.status == RUNNING]
    stop = [r for r in led.values() if not r.closed and r.status == EXITED and r.volume_gb]
    parts = []
    if run:
        rate = sum(r.cost_per_hr + r.running_disk_per_hr for r in run)
        soon = min((parse_iso(r.deadline) for r in run if r.deadline), default=None)
        mins = f", next stop {max(0, int((soon - now).total_seconds() // 60))}m" if soon else ""
        parts.append(f"GPU {len(run)} on ${rate:.2f}/h{mins}")
    if stop:
        day = sum(cost.stopped_disk_rate(r.volume_gb) for r in stop) * 24
        stale = sum(1 for x in lines if x.stale)
        parts.append(f"{len(stop)} stopped ${day:.2f}/day" + (f" ({stale} old!)" if stale else ""))
    return "moni_pod: " + " | ".join(parts)
