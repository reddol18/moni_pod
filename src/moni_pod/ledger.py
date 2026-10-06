"""Ledger: ~/.moni_pod/ledger.json — one record per pod moni_pod created.

A record keeps what the user agreed to (GPU, rate, TTL, budget) and the pod's run intervals,
so cost can be estimated without asking RunPod. Writes are atomic (temp file + os.replace)
and serialized with a lock file (fsutil).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .fsutil import LockTimeout, file_lock, write_json_atomic

SCHEMA_VERSION = 1

RUNNING = "RUNNING"
EXITED = "EXITED"
TERMINATED = "TERMINATED"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Run:
    start: str
    stop: str | None = None


@dataclass
class PodRecord:
    pod_id: str
    name: str
    session_id: str
    created_at: str
    compute: str  # "GPU" | "CPU"
    hw_id: str  # GPU type id or CPU flavor id
    hw_count: int
    cost_per_hr: float  # running rate in USD/h at creation (RunPod `cost`)
    running_disk_per_hr: float = 0.0  # disk charge while running, added on top (cost.py)
    container_disk_gb: int = 0
    volume_gb: int = 0  # mounts.persistent.size; billed while stopped
    ttl_sec: int = 0
    deadline: str | None = None  # absolute auto-stop time of the current run
    budget_usd: float | None = None
    status: str = RUNNING
    runs: list[Run] = field(default_factory=list)
    terminated_at: str | None = None
    retrieved: bool | None = None  # user confirmed results were copied off the pod
    billed_usd: float | None = None  # RunPod billing figure at close
    closed: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "PodRecord":
        d = dict(d)
        d["runs"] = [Run(**r) for r in d.get("runs", [])]
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_dict(self) -> dict:
        return asdict(self)


LedgerLockTimeout = LockTimeout  # backwards-compatible name


class Ledger:
    def __init__(self, home: Path | None = None):
        self.home = Path(home) if home else config.home_dir()
        self.path = self.home / "ledger.json"
        self.lock_path = self.home / "ledger.lock"

    # ---- read -------------------------------------------------------------
    def load(self) -> dict[str, PodRecord]:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return {pid: PodRecord.from_dict(rec) for pid, rec in data.get("pods", {}).items()}

    def get(self, pod_id: str) -> PodRecord | None:
        return self.load().get(pod_id)

    # ---- write ------------------------------------------------------------
    def _locked(self, timeout: float = 10.0):
        return file_lock(self.lock_path, timeout)

    def _save(self, pods: dict[str, PodRecord]) -> None:
        payload = {"version": SCHEMA_VERSION, "pods": {pid: r.to_dict() for pid, r in pods.items()}}
        write_json_atomic(self.path, payload)

    def upsert(self, record: PodRecord) -> None:
        with self._locked():
            pods = self.load()
            pods[record.pod_id] = record
            self._save(pods)

    def update(self, pod_id: str, **fields) -> PodRecord:
        with self._locked():
            pods = self.load()
            rec = pods[pod_id]
            for k, v in fields.items():
                if k not in PodRecord.__dataclass_fields__:
                    raise AttributeError(k)
                setattr(rec, k, v)
            self._save(pods)
            return rec

    def mark_stopped(self, pod_id: str, at: datetime | None = None) -> PodRecord:
        """Close the open run. Idempotent for an already-stopped pod."""
        at_s = to_iso(at or utcnow())
        with self._locked():
            pods = self.load()
            rec = pods[pod_id]
            if rec.runs and rec.runs[-1].stop is None:
                rec.runs[-1].stop = at_s
            rec.status = EXITED
            rec.deadline = None
            self._save(pods)
            return rec

    def mark_started(self, pod_id: str, ttl_sec: int, at: datetime | None = None) -> PodRecord:
        at_dt = at or utcnow()
        with self._locked():
            pods = self.load()
            rec = pods[pod_id]
            if not rec.runs or rec.runs[-1].stop is not None:
                rec.runs.append(Run(start=to_iso(at_dt)))
            rec.status = RUNNING
            rec.ttl_sec = ttl_sec
            rec.deadline = to_iso(datetime.fromtimestamp(at_dt.timestamp() + ttl_sec, timezone.utc))
            self._save(pods)
            return rec

    def mark_terminated(self, pod_id: str, at: datetime | None = None) -> PodRecord:
        at_s = to_iso(at or utcnow())
        with self._locked():
            pods = self.load()
            rec = pods[pod_id]
            if rec.runs and rec.runs[-1].stop is None:
                rec.runs[-1].stop = at_s
            rec.status = TERMINATED
            rec.deadline = None
            rec.terminated_at = at_s
            self._save(pods)
            return rec
