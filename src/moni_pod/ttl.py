"""In-pod TTL auto-stop (ADR-0003, generalized in ADR-0004).

The pod's original command (image ENTRYPOINT + CMD, or the template's override) is kept and
wrapped: a POSIX `sh -c` script starts a background watchdog, then `exec`s the original argv.

    entrypoint = ["/bin/sh", "-c", WATCHDOG, "moni-pod-ttl"]
    cmd        = <original argv>            # becomes "$@" inside the script

The watchdog writes a deadline (now + MONI_POD_TTL_SEC, epoch) to /tmp/moni_pod_deadline and
polls it every ≤20 s, so /gpu-extend can push it over SSH (HQ M3 condition 1). At the deadline it stops the pod with the pod-scoped key RunPod injects
(RUNPOD_POD_ID, RUNPOD_API_KEY): `runpodctl pod stop`, else the REST v2 action via curl/wget/python3.
`MONI_POD_TTL_METHODS` (space-separated) restricts/reorders the methods; used to prove each
fallback path in live tests. It only ever stops; it never terminates (PLAN §2.4).
"""

from __future__ import annotations

TTL_ENV = "MONI_POD_TTL_SEC"
ARGV0 = "moni-pod-ttl"
DEADLINE_FILE = "/tmp/moni_pod_deadline"  # epoch seconds; container-local, rewritten on every container start


def extend_command(add_sec: int, deadline_file: str = DEADLINE_FILE) -> str:
    """Remote shell command run over SSH by /gpu-extend: push the deadline, print the new epoch.

    Uses the pod's own clock and current deadline, so local clock skew does not matter.
    """
    if add_sec <= 0:
        raise ValueError("add_sec must be positive")
    f = deadline_file
    return (f'cur=$(cat {f} 2>/dev/null); case "$cur" in \'\'|*[!0-9]*) echo "no deadline file" >&2; exit 3;; esac; '
            f'new=$((cur + {int(add_sec)})); echo "$new" > {f}.tmp && mv {f}.tmp {f} && cat {f}')

WATCHDOG = r"""
dl_file="${MONI_POD_DEADLINE_FILE:-/tmp/moni_pod_deadline}"
initial=$(( $(date +%s) + ${MONI_POD_TTL_SEC:?} ))
echo "$initial" > "$dl_file"
(
  # Re-read the deadline file so /moni-pod:gpu-extend can move it over SSH. A missing or
  # corrupt file falls back to the initial deadline, never to "no deadline".
  while :; do
    dl=$(cat "$dl_file" 2>/dev/null)
    case "$dl" in ''|*[!0-9]*) dl=$initial ;; esac
    now=$(date +%s)
    [ "$now" -ge "$dl" ] && break
    left=$(( dl - now ))
    [ "$left" -gt 20 ] && left=20
    sleep "$left"
  done
  echo "[moni_pod] TTL reached - stopping pod ${RUNPOD_POD_ID}"
  url="https://api.runpod.io/v2/pods/${RUNPOD_POD_ID}/action"
  ua="moni-pod-ttl"  # the API edge rejects requests without a User-Agent (403)
  n=0
  while [ "$n" -lt 10 ]; do
    for m in ${MONI_POD_TTL_METHODS:-runpodctl curl wget python3}; do
      command -v "$m" >/dev/null 2>&1 || continue
      case "$m" in
        runpodctl)
          runpodctl pod stop "$RUNPOD_POD_ID" && echo "[moni_pod] stopped via runpodctl" && exit 0
          runpodctl stop pod "$RUNPOD_POD_ID" && echo "[moni_pod] stopped via runpodctl (legacy)" && exit 0 ;;
        curl)
          curl -fsS -X POST -A "$ua" -H "Authorization: Bearer ${RUNPOD_API_KEY}" -H "Content-Type: application/json" \
            -d '{"action":"stop"}' "$url" && echo "[moni_pod] stopped via curl" && exit 0 ;;
        wget)
          wget -q -O- -U "$ua" --header="Authorization: Bearer ${RUNPOD_API_KEY}" --header="Content-Type: application/json" \
            --post-data='{"action":"stop"}' "$url" && echo "[moni_pod] stopped via wget" && exit 0 ;;
        python3)
          MONI_URL="$url" python3 -c 'import os,urllib.request as u;u.urlopen(u.Request(os.environ["MONI_URL"],data=b"{\"action\":\"stop\"}",headers={"Authorization":"Bearer "+os.environ["RUNPOD_API_KEY"],"Content-Type":"application/json","User-Agent":"moni-pod-ttl"}))' \
            && echo "[moni_pod] stopped via python3" && exit 0 ;;
      esac
    done
    n=$((n+1))
    echo "[moni_pod] stop attempt $n failed; retrying in 30s"
    sleep 30
  done
  echo "[moni_pod] TTL stop FAILED - stop this pod manually"
) &
if [ "$#" -gt 0 ]; then exec "$@"; fi
exec sleep infinity
""".strip()

# Bare shells exit at once without a TTY, which would make the pod restart in a loop.
_BARE_SHELLS = {"/bin/bash", "/bin/sh", "bash", "sh", "/usr/bin/bash", "/usr/bin/env bash"}


def keep_alive_argv(argv: list[str]) -> list[str]:
    if not argv or (len(argv) == 1 and argv[0] in _BARE_SHELLS):
        return []  # → `exec sleep infinity`
    return list(argv)


def split_args(args: str | None) -> list[str]:
    """Template `args` as a bare shell string (RunPod treats it as CMD, split into arguments)."""
    import shlex
    return shlex.split(args) if args else []


def original_argv(template: dict | None, image_cmd: tuple[list[str], list[str]] | None) -> list[str]:
    """Resolve the command the pod would run without moni_pod.

    Precedence matches Docker: template entrypoint overrides image ENTRYPOINT; template cmd/args
    overrides image CMD; an overridden entrypoint drops the image CMD unless cmd is given.
    """
    img_ep, img_cmd = image_cmd or ([], [])
    t = template or {}
    t_ep = t.get("entrypoint") or []
    t_cmd = t.get("cmd") or (split_args(t.get("args")) if not t_ep else [])
    if t_ep:
        return list(t_ep) + list(t_cmd)
    return list(img_ep) + list(t_cmd or img_cmd)


METHODS = ("runpodctl", "curl", "wget", "python3")


def wrap(argv: list[str], ttl_sec: int, methods: list[str] | None = None) -> dict:
    """Body fragment for POST /v2/pods: entrypoint, cmd, env."""
    if ttl_sec <= 0:
        raise ValueError("ttl_sec must be positive")
    env = {TTL_ENV: str(int(ttl_sec))}
    if methods:
        bad = [m for m in methods if m not in METHODS]
        if bad:
            raise ValueError(f"unknown stop method(s) {bad}; allowed {METHODS}")
        env["MONI_POD_TTL_METHODS"] = " ".join(methods)
    return {"entrypoint": ["/bin/sh", "-c", WATCHDOG, ARGV0], "cmd": keep_alive_argv(argv), "env": env}
