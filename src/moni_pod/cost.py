"""Cost estimates from the ledger alone (no RunPod call), plus billing comparison.

Running:  hours running × (cost_per_hr + running_disk_per_hr). cost_per_hr is RunPod's per-pod
          `cost` (USD/h) at creation; running disk = (container + volume GB) × $0.10/GB/month ÷ 730 h.
          Whether `cost` already includes disk is checked in M3; until then disk is added
          (conservative, may double count — ADR-0004).
Stopped:  hours stopped × volume_gb × $0.20 /GB/month ÷ 730 h. Container disk is wiped on
          stop and not billed. Rates: https://docs.runpod.io/accounts-billing/billing
These are estimates; RunPod bills per second and its billing view lags (runs every 5 min).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .ledger import TERMINATED, PodRecord, parse_iso

HOURS_PER_MONTH = 730.0
VOLUME_STOPPED_USD_PER_GB_MONTH = 0.20
DISK_RUNNING_USD_PER_GB_MONTH = 0.10  # container disk and volume disk while running


def stopped_disk_rate(volume_gb: int | float) -> float:
    """USD per hour a stopped pod keeps costing for its persistent volume."""
    return volume_gb * VOLUME_STOPPED_USD_PER_GB_MONTH / HOURS_PER_MONTH


def running_disk_rate(container_gb: int | float, volume_gb: int | float) -> float:
    """USD per hour for container + volume disk while the pod runs."""
    return (container_gb + volume_gb) * DISK_RUNNING_USD_PER_GB_MONTH / HOURS_PER_MONTH


def _hours(a: datetime, b: datetime) -> float:
    return max(0.0, (b - a).total_seconds() / 3600.0)


@dataclass(frozen=True)
class Estimate:
    running_hours: float
    running_usd: float
    stopped_hours: float
    stopped_disk_usd: float

    @property
    def total_usd(self) -> float:
        return self.running_usd + self.stopped_disk_usd


def estimate(rec: PodRecord, now: datetime) -> Estimate:
    end_of_life = parse_iso(rec.terminated_at) if rec.status == TERMINATED else None
    horizon = min(now, end_of_life) if end_of_life else now

    running_h = 0.0
    stopped_h = 0.0
    prev_stop: datetime | None = None
    for run in rec.runs:
        start = parse_iso(run.start)
        stop = parse_iso(run.stop) or horizon
        if prev_stop is not None:
            stopped_h += _hours(prev_stop, start)
        running_h += _hours(start, min(stop, horizon))
        prev_stop = parse_iso(run.stop)
    if prev_stop is not None:  # stopped now (or until termination)
        stopped_h += _hours(prev_stop, horizon)

    return Estimate(
        running_hours=running_h,
        running_usd=running_h * (rec.cost_per_hr + rec.running_disk_per_hr),
        stopped_hours=stopped_h,
        stopped_disk_usd=stopped_h * stopped_disk_rate(rec.volume_gb),
    )


def max_cost(cost_per_hr: float, ttl_sec: int) -> float:
    """Worst case for one run: the pod runs until its TTL stops it."""
    return cost_per_hr * ttl_sec / 3600.0


@dataclass(frozen=True)
class BillingCheck:
    estimate_usd: float
    billed_usd: float

    @property
    def diff_usd(self) -> float:
        return self.billed_usd - self.estimate_usd

    @property
    def diff_ratio(self) -> float | None:
        return None if self.estimate_usd == 0 else self.diff_usd / self.estimate_usd


def billed_total(billing_response: dict, pod_id: str | None = None) -> float:
    """Sum `totalAmount` of /v2/billing/pods records (optionally for one pod)."""
    total = 0.0
    for r in billing_response.get("records", []):
        if pod_id is None or r.get("podId") == pod_id:
            total += float(r.get("totalAmount") or 0.0)
    return total
