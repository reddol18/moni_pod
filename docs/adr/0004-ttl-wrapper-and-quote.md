# ADR-0004 — Generalized TTL wrapper, quote rules, extend

- Status: proposed (M2, 2026-10-06), pending HQ review
- Refines ADR-0003 per HQ condition C, and HQ M1 notes 1–2.

## Context

ADR-0003 hard-coded `exec /start.sh`. M2 found that RunPod official templates leave the start
command empty (`args: ""`) — the pod runs the **image's** ENTRYPOINT + CMD — and these differ:

| Image (official template) | ENTRYPOINT | CMD |
|---|---|---|
| `runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404` (`runpod-torch-v280`) | `/opt/nvidia/nvidia_entrypoint.sh` | `/start.sh` |
| `runpod/pytorch:2.4.0-…` (`runpod-torch-v240`) | `/opt/nvidia/nvidia_entrypoint.sh` | `/start.sh` |
| `runpod/base:1.0.2-ubuntu2404` (`runpod-ubuntu-2404`, CPU) | – | `/start.sh` |
| `ubuntu:22.04` | – | `/bin/bash` |

`exec /start.sh` would have skipped the NVIDIA entrypoint on every PyTorch pod.

## Decision

1. **Resolve the original command, then wrap it unchanged.**
   - Template `entrypoint`/`cmd`/`args` if set; otherwise read the image config (ENTRYPOINT, CMD) from its
     registry (anonymous OCI distribution API, `linux/amd64`) — `registry.py`. Docker precedence applies.
   - Create body: `entrypoint = ["/bin/sh","-c", WATCHDOG, "moni-pod-ttl"]`, `cmd = <original argv>`;
     the script backgrounds the watchdog and `exec "$@"`. A bare shell (`/bin/bash`) or empty command becomes
     `sleep infinity` so the pod does not restart-loop.
   - If the command cannot be read (private image, registry down) → **refuse** with the reason. No guessing.
2. **Templates are resolved client-side** (image, ports, env, disk, persistent mount, startSsh) and sent as an
   explicit body, not `templateId`. This makes the wrapper explicit and allows template + CPU pod, which v2
   `templateId` cannot express.
3. **Watchdog**: `sleep $MONI_POD_TTL_SEC`, then stop via `runpodctl pod stop` (legacy `stop pod`), else
   REST v2 `POST /pods/$RUNPOD_POD_ID/action {"action":"stop"}` via curl / wget / python3, retry 10 × 30 s.
   Stop only. Runs under `/bin/sh` (POSIX), verified locally with a fake `runpodctl`.
4. **Quote / budget** (HQ note 2, conservative): `max_cost = (compute + running disk) × TTL`, running disk =
   (container + volume GB) × $0.10/GB/month ÷ 730. Budget check = this session's committed spend (estimate so
   far + remaining TTL of running pods) + new max_cost ≤ session budget ($2 default). TTL > cap (8 h) refused.
   Disk is added on top of RunPod's pod `cost`; M2 observation: `cost` equals the catalog compute price
   (cpu3c 2 vCPU 0.06, A2000 community 0.12), so it likely excludes disk — confirm with billing in M3.
5. **Gate in M2**: `moni-pod start` consumes a token minted by the `UserPromptExpansion` hook (ADR-0002 key 1);
   in-chat AskUserQuestion confirmation until M4 adds the PreToolUse `ask` (key 2). A refused quote does not burn
   the token; a failed create after consume does (user re-types the command).
6. **`/gpu-extend`: not implemented in place for v0.1.** Every in-place channel is destructive or needs SSH:
   changing env/args via PATCH resets the container (container disk wiped); writing a deadline file needs SSH
   keys. Instead: let the TTL stop the pod (volume `/workspace` survives), then **resume via a gated
   `/moni-pod:gpu-start --resume <pod>`** (M3, new approval, fresh TTL). Revisit an SSH-based extend after M5.

## Consequences

- Private images need a template with an explicit start command (documented).
- Registry reads add ~1 s to a quote; only Docker Hub / OCI registries with anonymous pull are supported.
- `/gpu-extend` becomes "stop → resume" (PLAN §3 marked it optional). Needs HQ approval.
