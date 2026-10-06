"""/gpu-stop: stop or terminate a pod, settle the ledger, compare with RunPod billing.

- stop: allowed without a token (it only lowers spend, ADR-0002).
- terminate: needs a token minted by the user typing /moni-pod:gpu-stop, AND the user's answer to
  "were the results copied off the pod?". An unretrieved pod is never terminated unless the user
  explicitly chose to discard it (`discard_unretrieved`). PLAN §2.4.
- settle: estimate vs RunPod pod billing for that pod. Billing history posts late (~1 h) and in partial
  increments (M5: a pod showed 30 % of its cost first). A terminated pod's record is `closed` when
  (a) billed ≥ SETTLE_RATIO × estimate, or (b) SETTLE_MAX_HOURS passed since deletion — then it closes
  with a warning if billing is missing or still short. Until then the billed figure is provisional and
  `moni-pod reconcile` updates it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import cost, guard
from .ledger import EXITED, RUNNING, TERMINATED, Ledger, PodRecord, parse_iso, utcnow
from .runpod_api import RunPodClient, RunPodError


SETTLE_RATIO = 0.95  # billing has caught up with the estimate
SETTLE_MAX_HOURS = 24.0  # safety cap: close anyway, with a warning


class StopRefused(RuntimeError):
    pass


@dataclass
class Settlement:
    pod_id: str
    name: str
    status: str
    estimate_usd: float
    billed_usd: float | None
    diff_usd: float | None
    stopped_disk_per_day: float
    closed: bool
    note: str


def settle(rec: PodRecord, client: RunPodClient, now: datetime) -> Settlement:
    est = cost.estimate(rec, now)
    billed = None
    note = ""
    try:
        resp = client.pod_billing(pod_id=rec.pod_id, bucket_size="hour", last_n=24 * 31)
        billed = cost.billed_total(resp, rec.pod_id)
    except (RunPodError, OSError) as e:
        note = f"billing unavailable: {e}"
    if billed == 0:
        billed = None
        note = note or "RunPod billing not posted yet (it lags; run `moni-pod reconcile` later)"
    diff = None if billed is None else round(billed - est.total_usd, 6)
    disk_day = 0.0 if rec.status == TERMINATED else cost.stopped_disk_rate(rec.volume_gb) * 24
    ended = parse_iso(rec.terminated_at)
    timed_out = bool(ended and (now - ended).total_seconds() >= SETTLE_MAX_HOURS * 3600)
    caught_up = billed is not None and billed >= SETTLE_RATIO * est.total_usd
    closed = rec.status == TERMINATED and (caught_up or timed_out)
    if closed and not caught_up:
        note = ("WARNING: closed after {:g} h without matching billing - ".format(SETTLE_MAX_HOURS)
                + ("RunPod shows no charge for this pod" if billed is None else
                   f"billed ${billed:.4f} is {billed / est.total_usd:.0%} of the estimate ${est.total_usd:.4f}"
                   if est.total_usd else f"billed ${billed:.4f}, estimate $0"))
    elif billed is not None and not closed:
        note = note or (f"billed figure is provisional ({billed / est.total_usd:.0%} of estimate so far; "
                        "RunPod posts it in parts)" if est.total_usd else "billed figure is provisional")
    return Settlement(rec.pod_id, rec.name, rec.status, round(est.total_usd, 6), billed, diff,
                      round(disk_day, 6), closed=closed, note=note)


def stop_pod(pod_id: str, *, action: str, session_id: str, client: RunPodClient,
             retrieved: bool | None, discard_unretrieved: bool = False,
             ledger: Ledger | None = None, now: datetime | None = None) -> Settlement:
    if action not in ("stop", "terminate"):
        raise StopRefused("action must be stop or terminate")
    ledger = ledger or Ledger()
    rec = ledger.get(pod_id)
    if rec is None:
        raise StopRefused(f"{pod_id} is not in the moni_pod ledger (not created by /moni-pod:gpu-start)")

    if action == "terminate":
        if retrieved is None:
            raise StopRefused("ask the user whether the results were copied off the pod before deleting")
        if not retrieved and not discard_unretrieved:
            raise StopRefused("results not retrieved: stop instead, or the user must explicitly choose to discard them")
        guard.consume("stop", session_id, now=now)  # user typed /moni-pod:gpu-stop

    live = client.get_pod(pod_id)
    st = live.get("status")
    if action == "stop" and st in ("PROVISIONING", "STARTING", "RUNNING"):
        client.pod_action(pod_id, "stop")
    elif action == "terminate":
        client.pod_action(pod_id, "terminate")  # 204; valid from RUNNING, EXITED and ERROR

    now = now or utcnow()
    if action == "terminate":
        rec = ledger.mark_terminated(pod_id, at=now)
    elif rec.status == RUNNING:
        rec = ledger.mark_stopped(pod_id, at=now)
    rec = ledger.update(pod_id, retrieved=retrieved if retrieved is not None else rec.retrieved)

    s = settle(rec, client, now)
    ledger.update(pod_id, billed_usd=s.billed_usd, closed=s.closed)
    return s


def reconcile(*, client: RunPodClient, ledger: Ledger | None = None, now: datetime | None = None) -> list[Settlement]:
    """Re-read billing for every pod not yet closed; close terminated ones whose billing has posted."""
    ledger = ledger or Ledger()
    now = now or utcnow()
    out = []
    for rec in ledger.load().values():
        if rec.closed:
            continue
        s = settle(rec, client, now)
        ledger.update(rec.pod_id, billed_usd=s.billed_usd, closed=s.closed)
        out.append(s)
    return out


def render(s: Settlement) -> str:
    billed = "pending" if s.billed_usd is None else f"${s.billed_usd:.4f}"
    diff = "" if s.diff_usd is None else f" (diff {s.diff_usd:+.4f})"
    lines = [f"{s.name} [{s.status}]  estimate ${s.estimate_usd:.4f}  RunPod billed {billed}{diff}"]
    if s.status == EXITED:
        lines.append(f"  stopped pod still bills disk: ${s.stopped_disk_per_day:.4f}/day until deleted")
    if s.note:
        lines.append(f"  {s.note}")
    if s.closed:
        lines.append("  ledger closed")
    return "\n".join(lines)
