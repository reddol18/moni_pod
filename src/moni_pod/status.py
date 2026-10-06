"""Status view: ledger merged with live RunPod pods.

`build` is pure. The CLI runs sync.py first (records stops/starts that happened outside our
commands), so `sync pending` notes appear only with `--no-sync` or when sync could not run.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

from . import cost
from .ledger import EXITED, RUNNING, TERMINATED, PodRecord, parse_iso

# RunPod statuses that mean "compute is being billed".
LIVE_RUNNING = {"PROVISIONING", "STARTING", "RUNNING"}
TTL_GRACE_SEC = 300
TTL_WARN_SEC = 600  # "ending soon" warning (HQ M3 condition 1)
NETWORK_VOLUME_NOTE = "Network volumes are not tracked (billed separately by RunPod, even with no pod)."


@dataclass
class PodRow:
    pod_id: str
    name: str
    managed: bool  # created through moni_pod (present in ledger)
    this_session: bool
    hardware: str
    status: str  # RunPod status if known, else ledger status
    rate_per_hr: float  # current compute rate; 0 when stopped
    stopped_disk_per_hr: float
    elapsed_sec: int | None  # current run
    ttl_left_sec: int | None
    est_usd: float | None  # ledger estimate (managed) or this-run estimate (unmanaged)
    budget_usd: float | None
    notes: list[str] = field(default_factory=list)


@dataclass
class StatusReport:
    now: str
    rows: list[PodRow]
    billed_today_usd: float | None = None
    errors: list[str] = field(default_factory=list)
    expected_spend_per_hr: float = 0.0  # what RunPod's currentSpendPerHr should show for these pods
    account: dict | None = None  # RunPod clientBalance / currentSpendPerHr / spendLimit

    @property
    def running(self) -> list[PodRow]:
        return [r for r in self.rows if r.status in LIVE_RUNNING]

    @property
    def stopped(self) -> list[PodRow]:
        return [r for r in self.rows if r.status in ("EXITED", "ERROR")]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["totals"] = self.totals()
        return d

    def totals(self) -> dict:
        return {
            "running_count": len(self.running),
            "stopped_count": len(self.stopped),
            "rate_per_hr": round(sum(r.rate_per_hr for r in self.running), 4),
            "stopped_disk_per_day": round(sum(r.stopped_disk_per_hr for r in self.stopped) * 24, 4),
            "est_usd": round(sum(r.est_usd or 0.0 for r in self.rows), 4),
        }


def _hardware(pod: dict | None, rec: PodRecord | None) -> str:
    if pod and pod.get("gpu"):
        g = pod["gpu"]
        return f"{g.get('id', '?')} x{g.get('count', 1)}"
    if pod and pod.get("cpu"):
        c = pod["cpu"]
        return f"CPU {c.get('id', '?')} {c.get('vcpuCount', '?')}vCPU"
    if rec:
        return f"{rec.hw_id} x{rec.hw_count}" if rec.compute == "GPU" else f"CPU {rec.hw_id}"
    return "?"


def _volume_gb(pod: dict | None, rec: PodRecord | None) -> int:
    if pod:
        persistent = (pod.get("mounts") or {}).get("persistent") or {}
        if persistent.get("size"):
            return int(persistent["size"])
    return rec.volume_gb if rec else 0


_LEDGER_FOR_LIVE = {"PROVISIONING": RUNNING, "STARTING": RUNNING, "RUNNING": RUNNING,
                    "EXITED": EXITED, "ERROR": EXITED, "TERMINATED": TERMINATED}


def build(now: datetime, ledger_pods: dict[str, PodRecord], live_pods: list[dict] | None,
          session_id: str | None = None, stale_stopped_hours: float = 24.0) -> StatusReport:
    """`live_pods=None` means RunPod was not reachable/asked: ledger-only view."""
    rows: list[PodRow] = []
    live_by_id = {p["id"]: p for p in (live_pods or [])}

    for pod_id in list(live_by_id) + [p for p in ledger_pods if p not in live_by_id]:
        pod = live_by_id.get(pod_id)
        rec = ledger_pods.get(pod_id)
        if pod is None and rec is not None and (rec.closed or rec.status == TERMINATED):
            continue  # closed history; not shown in status

        notes: list[str] = []
        status = pod["status"] if pod else rec.status
        running = status in LIVE_RUNNING
        rate = float(pod.get("cost") or 0.0) if pod else (rec.cost_per_hr if running else 0.0)
        vol = _volume_gb(pod, rec)
        disk_rate = 0.0 if running else cost.stopped_disk_rate(vol)

        started = parse_iso(pod.get("startedAt")) if pod else (
            parse_iso(rec.runs[-1].start) if rec and rec.runs and rec.runs[-1].stop is None else None)
        elapsed = int((now - started).total_seconds()) if running and started else None

        ttl_left = None
        est = None
        budget = None
        if rec:
            budget = rec.budget_usd
            est = cost.estimate(rec, now).total_usd
            deadline = parse_iso(rec.deadline)
            if running and deadline:
                ttl_left = int((deadline - now).total_seconds())
                if ttl_left < -TTL_GRACE_SEC:
                    notes.append("TTL passed but pod still running - auto-stop failed, stop it now")
                elif 0 <= ttl_left <= TTL_WARN_SEC:
                    notes.append(f"auto-stop in {ttl_left // 60} min - copy results off or /moni-pod:gpu-extend")
            if budget is not None and est > budget:
                notes.append(f"over budget (${est:.2f} > ${budget:.2f})")
            if live_pods is not None:
                if pod is None:
                    notes.append("not found on RunPod - terminated outside moni_pod? (ledger not updated)")
                elif _LEDGER_FOR_LIVE.get(status) != rec.status:
                    notes.append(f"ledger says {rec.status}, RunPod says {status} (sync pending)")
        else:
            if running and started:
                est = rate * (now - started).total_seconds() / 3600.0
            notes.append("not created by moni_pod")
        if not running and vol:
            stopped_at = parse_iso(rec.runs[-1].stop) if rec and rec.runs and rec.runs[-1].stop else None
            if stopped_at:
                h = (now - stopped_at).total_seconds() / 3600
                age = f"{int(h // 24)}d {int(h % 24)}h" if h >= 24 else f"{int(h)}h {int(h * 60 % 60)}m"
                notes.append(f"{'!! ' if h >= stale_stopped_hours else ''}stopped {age}: {vol} GB disk has cost "
                             f"${disk_rate * h:.3f} so far (${disk_rate * 24:.3f}/day) - copy results off, "
                             "then delete with /moni-pod:gpu-stop")
            else:
                notes.append(f"stopped but still billing disk ({vol} GB, ${disk_rate * 24:.3f}/day)")

        rows.append(PodRow(
            pod_id=pod_id,
            name=(pod.get("name") if pod else rec.name) or "",
            managed=rec is not None,
            this_session=bool(rec and session_id and rec.session_id == session_id),
            hardware=_hardware(pod, rec),
            status=status,
            rate_per_hr=rate if running else 0.0,
            stopped_disk_per_hr=disk_rate,
            elapsed_sec=elapsed,
            ttl_left_sec=ttl_left,
            est_usd=None if est is None else round(est, 4),
            budget_usd=budget,
            notes=notes,
        ))
    rows.sort(key=lambda r: (not r.this_session, r.status not in LIVE_RUNNING, r.name))
    expected = 0.0
    for r in rows:
        rec = ledger_pods.get(r.pod_id)
        if r.status in LIVE_RUNNING:
            expected += r.rate_per_hr + (rec.running_disk_per_hr if rec else 0.0)
        else:
            expected += r.stopped_disk_per_hr
    return StatusReport(now=now.isoformat(timespec="seconds"), rows=rows, expected_spend_per_hr=round(expected, 4))


def _dur(sec: int | None) -> str:
    if sec is None:
        return "-"
    sign = "-" if sec < 0 else ""
    sec = abs(sec)
    h, rem = divmod(sec, 3600)
    return f"{sign}{h}h{rem // 60:02d}m"


def render_text(rep: StatusReport) -> str:
    t = rep.totals()
    out = [f"moni_pod status @ {rep.now}"]
    out.append(f"Running: {t['running_count']}  (${t['rate_per_hr']:.3f}/h now)   "
               f"Stopped: {t['stopped_count']}  (disk ${t['stopped_disk_per_day']:.3f}/day)   "
               f"Estimated so far: ${t['est_usd']:.2f}")
    if rep.account:
        a = rep.account
        spend = a.get("currentSpendPerHr")
        line = (f"RunPod account: balance ${a.get('clientBalance', 0):.2f}, spending ${spend or 0:.3f}/h now "
                f"(these pods should be ${rep.expected_spend_per_hr:.3f}/h)")
        if a.get("spendLimit") is not None:
            line += f", RunPod spend limit ${a['spendLimit']}/h"
        out.append(line)
        if spend is not None and spend > rep.expected_spend_per_hr + 0.01:
            out.append("! RunPod is spending more than these pods explain - other resources (network volumes, "
                       "serverless, pods in other accounts/tools) are running")
    if rep.billed_today_usd:
        out.append(f"RunPod billing history today (posts hours late): ${rep.billed_today_usd:.2f}")
    for e in rep.errors:
        out.append(("  " if e.startswith("ledger synced") else "! ") + e)
    if not rep.rows:
        out.append("No pods.")
        out.append(NETWORK_VOLUME_NOTE)
        return "\n".join(out)
    out.append("")
    header = f"{'':2}{'name':<20} {'hardware':<28} {'status':<9} {'$/h':>7} {'elapsed':>8} {'TTL left':>8} {'est $':>8}"
    out.append(header)
    for r in rep.rows:
        mark = "*" if r.this_session else ("+" if r.managed else " ")
        rate = f"{r.rate_per_hr:.3f}" if r.rate_per_hr else (f"d{r.stopped_disk_per_hr:.4f}" if r.stopped_disk_per_hr else "0")
        est = "-" if r.est_usd is None else f"{r.est_usd:.2f}"
        out.append(f"{mark:<2}{r.name[:20]:<20} {r.hardware[:28]:<28} {r.status:<9} {rate:>7} "
                   f"{_dur(r.elapsed_sec):>8} {_dur(r.ttl_left_sec):>8} {est:>8}")
        for n in r.notes:
            out.append(f"{'':4}- {n}")
    out.append("")
    out.append("* this session  + created by moni_pod  d = stopped-disk $/h.  Estimates; final figure comes from RunPod billing.")
    out.append(NETWORK_VOLUME_NOTE)
    return "\n".join(out)
