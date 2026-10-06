# ADR-0003 — In-Pod TTL auto-stop

- Status: **accepted — 2026-10-06 HQ approval (지휘부 승인)**; mechanics to be proven by the M2 real run
- Sources: docs.runpod.io (`pods/references/environment-variables`, `pods/manage-pods`,
  `runpodctl/overview`, `api-reference-v2/migrate-from-v1`, `accounts-billing/billing`),
  REST v2 OpenAPI, https://github.com/runpod/runpodctl/pull/330

## Context

PLAN §2.3: a Pod must stop itself when the user-agreed TTL passes, even if this PC is off.

## Facts established in M0

- **No working native TTL.** `stopAfter`/`terminateAfter` existed only in GraphQL, "never worked"
  (Pods kept running and billing past the deadline) and were removed from runpodctl in v2.12
  (https://github.com/runpod/runpodctl/pull/330). REST v2 has no such field.
- RunPod injects `RUNPOD_POD_ID` and `RUNPOD_API_KEY` ("Pod-scoped API key") into every Pod, and
  "Every Pod you deploy comes preinstalled with `runpodctl`".
- Official recipe (manage-pods, "Schedule a stop"): `sleep 2h; runpodctl pod stop $RUNPOD_POD_ID &`.
- REST v2 create takes the container command in `args` / `entrypoint` / `cmd`; overriding it replaces
  the image's own start (`/start.sh` in RunPod images, which starts SSH/Jupyter), so our wrapper must
  chain to it.
- REST v1 is retired on 2026-11-15 → all our calls use v2 (`https://api.runpod.io/v2`).

## Decision

> Superseded in part by ADR-0004: the original command is resolved (template or image ENTRYPOINT+CMD)
> and wrapped as is, instead of the hard-coded `/start.sh` below.

On create, `ttl.py` sets `entrypoint: ["/bin/bash","-c"]`, `cmd: [<script>]` with env
`MONI_POD_TTL_SEC`, where the script is roughly:

```bash
( sleep "$MONI_POD_TTL_SEC"
  runpodctl pod stop "$RUNPOD_POD_ID" \
  || curl -fsS -X POST -H "Authorization: Bearer $RUNPOD_API_KEY" -H 'Content-Type: application/json' \
       -d '{"action":"stop"}' "https://api.runpod.io/v2/pods/$RUNPOD_POD_ID/action" ) &
if [ -x /start.sh ]; then exec /start.sh; else exec sleep infinity; fi
```

- **Stop, never terminate** (PLAN §2.4).
- TTL is "max continuous run per start": a restart of the container re-arms the full TTL. A resume
  is only possible via `/gpu-start` (gated), which re-confirms cost. The ledger keeps the absolute
  deadline for display; `/gpu-status` flags a Pod still RUNNING past deadline + 5 min as
  "TTL failed — stop it now".
- `/gpu-extend`: update env through `update-pod` would reset the container (container disk wiped),
  so extension is implemented in M2 by one of: (a) in-Pod deadline file the watchdog re-reads
  (written over SSH/`runpodctl`), or (b) reset with warning. Choice recorded in M2 after testing.
- Official Idle Monitor: **not auto-attached** (see Consequences).

## Alternatives

- Native `stopAfter` — broken / removed.
- Watchdog on this PC — fails when the PC is off (violates §2.3). Kept only as a display check.
- A cheap always-on "supervisor" CPU Pod polling all Pods — costs money 24/7; rejected.

## Consequences / open items for M2

- Must verify in the real run: the Pod-scoped key is allowed to stop its own Pod; `runpodctl` is on
  PATH in the chosen image; `/start.sh` still brings up SSH.
- Images without bash get no TTL → `/gpu-start` refuses non-bash custom images or warns loudly.
- `Runpod-Idle-Pod-Monitor` has **no LICENSE file** (GitHub API: license=None), is installed to `/tmp`,
  uses GraphQL and is interactive → cannot be bundled or auto-attached. README links to it as an
  optional manual add-on. (Deviation from PLAN §3 "optional: attach Idle Monitor".)
