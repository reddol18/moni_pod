"""/gpu-extend: push a running pod's TTL deadline in place over SSH (HQ M3 condition 1).

Gate: a token minted by the user typing /moni-pod:gpu-extend; consumed only after the deadline
file in the pod was actually moved. Rules: the run's total length stays within ttl_max_hours, and
the extra time at the pod's rate fits the session budget. Without SSH, the user is told to let
the TTL stop the pod and resume it with /moni-pod:gpu-start --resume (fallback, ADR-0004).
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from . import config, guard, ttl
from .ledger import RUNNING, Ledger, parse_iso, to_iso, utcnow
from .runpod_api import RunPodClient
from .settings import Settings
from .start import session_committed


class ExtendRefused(RuntimeError):
    pass


@dataclass
class ExtendResult:
    pod_id: str
    old_deadline: str | None
    new_deadline: str
    added_sec: int
    extra_max_usd: float


def ssh_target(pod: dict) -> tuple[str, int, str] | None:
    direct = ((pod.get("ssh") or {}).get("direct")) or {}
    if direct.get("host") and direct.get("port"):
        return direct["host"], int(direct["port"]), direct.get("username") or "root"
    return None


WINDOWS_OPENSSH = r"C:\Windows\System32\OpenSSH\ssh.exe"


def ssh_binary() -> str:
    """`MONI_POD_SSH` if set; on Windows the system OpenSSH (it talks to the Windows ssh-agent service,
    which Git Bash's ssh cannot - a passphrase key then fails with 'Permission denied (publickey)')."""
    override = os.environ.get("MONI_POD_SSH")
    if override:
        return override
    if sys.platform == "win32" and os.path.exists(WINDOWS_OPENSSH):
        return WINDOWS_OPENSSH
    return "ssh"


def run_ssh(host: str, port: int, user: str, command: str, timeout: float = 30) -> subprocess.CompletedProcess:
    # Pods are short-lived hosts: keep their host keys out of the user's ~/.ssh/known_hosts.
    home = config.home_dir()
    home.mkdir(parents=True, exist_ok=True)
    known = (home / "known_hosts").as_posix()
    return subprocess.run(
        [ssh_binary(), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={known}",
         "-o", "ConnectTimeout=15",
         "-p", str(port), f"{user}@{host}", command],
        capture_output=True, text=True, timeout=timeout)


def extend(pod_id: str, add_hours: float, *, session_id: str, client: RunPodClient, settings: Settings,
           ledger: Ledger | None = None, now: datetime | None = None, ssh=run_ssh) -> ExtendResult:
    ledger = ledger or Ledger()
    now = now or utcnow()
    if add_hours <= 0:
        raise ExtendRefused("hours to add must be positive")
    rec = ledger.get(pod_id)
    if rec is None:
        raise ExtendRefused(f"{pod_id} is not in the moni_pod ledger")
    if rec.status != RUNNING or not rec.runs or rec.runs[-1].stop is not None:
        raise ExtendRefused("only a running pod can be extended; for a stopped pod use /moni-pod:gpu-start --resume")
    if guard.find_valid("extend", session_id, now=now) is None:
        raise guard.TokenError("no valid 'extend' approval for this session. The user must type /moni-pod:gpu-extend.")

    add_sec = int(round(add_hours * 3600))
    run_start = parse_iso(rec.runs[-1].start)
    old_deadline = parse_iso(rec.deadline) or now
    total_h = (old_deadline - run_start).total_seconds() / 3600 + add_hours
    if total_h > settings.ttl_max_hours + 1e-9:
        raise ExtendRefused(f"this run would last {total_h:.2f} h, over the TTL cap of {settings.ttl_max_hours:g} h")
    extra = add_hours * (rec.cost_per_hr + rec.running_disk_per_hr)
    budget = rec.budget_usd if rec.budget_usd is not None else settings.session_budget_usd
    committed = session_committed(ledger.load(), session_id, now)
    if committed + extra > budget + 1e-9:
        raise ExtendRefused(f"extra up to ${extra:.2f} + committed ${committed:.2f} exceeds the session budget ${budget:.2f}")

    target = ssh_target(client.get_pod(pod_id))
    if target is None:
        raise ExtendRefused("pod has no direct SSH (22/tcp) - cannot extend in place. Let the TTL stop it, "
                            "then /moni-pod:gpu-start --resume (container disk is wiped on stop; /workspace stays)")
    proc = ssh(*target, ttl.extend_command(add_sec))
    out = (proc.stdout or "").strip().splitlines()
    if proc.returncode != 0 or not out or not out[-1].isdigit():
        raise ExtendRefused(f"SSH extend failed (exit {proc.returncode}): {(proc.stderr or '').strip()[:200]} "
                            "- check that your SSH key is on the pod; fallback: stop then resume")
    new_dl = datetime.fromtimestamp(int(out[-1]), timezone.utc)

    guard.consume("extend", session_id, now=now)
    ledger.update(pod_id, deadline=to_iso(new_dl), ttl_sec=rec.ttl_sec + add_sec)
    return ExtendResult(pod_id, rec.deadline, to_iso(new_dl), add_sec, round(extra, 4))
