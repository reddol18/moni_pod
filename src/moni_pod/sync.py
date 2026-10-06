"""Ledger ← RunPod reconciliation (HQ condition B).

Stops done outside our commands (TTL watchdog, direct `pod-action stop`, runpodctl, console) leave
the ledger RUNNING. Sync records them as facts; it never calls a spending action.

Stop time precedence: last `stop container` in the pod's system log → the TTL deadline (if passed)
→ now. Start time for a pod that is running again: RunPod `startedAt`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .ledger import EXITED, RUNNING, TERMINATED, Ledger, PodRecord, parse_iso

LIVE_RUNNING = {"PROVISIONING", "STARTING", "RUNNING"}


@dataclass
class Change:
    pod_id: str
    action: str  # "stopped" | "started" | "terminated"
    at: str
    source: str  # where the timestamp came from


def last_stop_time(events: list[dict]) -> datetime | None:
    stops = [parse_iso(e.get("ts")) for e in events
             if str(e.get("line", "")).startswith("stop container") and e.get("ts")]
    return max(stops) if stops else None


def plan(ledger_pods: dict[str, PodRecord], live_pods: list[dict], now: datetime,
         system_log: Callable[[str], list[dict]] | None = None) -> list[Change]:
    live = {p["id"]: p for p in live_pods}
    changes: list[Change] = []
    for pid, rec in ledger_pods.items():
        if rec.closed or rec.status == TERMINATED:
            continue
        pod = live.get(pid)
        if pod is None:
            changes.append(Change(pid, "terminated", now.isoformat(), "not listed by RunPod"))
            continue
        st = pod.get("status")
        if rec.status == RUNNING and st in ("EXITED", "ERROR"):
            at, src = None, ""
            if system_log:
                try:
                    at, src = last_stop_time(system_log(pid)), "system log"
                except Exception:
                    at = None
            deadline = parse_iso(rec.deadline)
            if at is None and deadline and deadline <= now:
                at, src = deadline, "TTL deadline"
            if at is None:
                at, src = now, "sync time"
            changes.append(Change(pid, "stopped", at.isoformat(), src))
        elif rec.status == EXITED and st in LIVE_RUNNING:
            at = parse_iso(pod.get("startedAt")) or now
            changes.append(Change(pid, "started", at.isoformat(), "RunPod startedAt"))
    return changes


def apply(ledger: Ledger, changes: list[Change]) -> None:
    for c in changes:
        at = parse_iso(c.at)
        if c.action == "stopped":
            ledger.mark_stopped(c.pod_id, at=at)
        elif c.action == "terminated":
            ledger.mark_terminated(c.pod_id, at=at)
        elif c.action == "started":
            rec = ledger.get(c.pod_id)
            # Started outside the gate: record it; the TTL watchdog re-arms on container start.
            ledger.mark_started(c.pod_id, rec.ttl_sec or 0, at=at)
