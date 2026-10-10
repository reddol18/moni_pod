# ADR-0007 — Pod SSH address in `moni-pod status`

- Status: accepted (user decision, 2026-10-10)
- Trigger: https://github.com/reddol18/moni_pod/issues/1 — an agent with a running pod asked for a supported way to get
  its SSH address instead of calling the API with the key

## Context

After `/moni-pod:gpu-start`, an agent has to log in to the pod to do its work. `moni-pod status` showed cost and TTL
but no address, and no other command printed it. PLAN §1 left "using" a pod to RunPod's official tools (MCP pod lookup,
`runpodctl ssh`), but those need their own auth: the hosted MCP takes the API key as a Bearer header or an OAuth
sign-in, and `runpodctl` needs the key. ADR-0006 tells agents never to use the key. So the only remaining routes were
asking the user to copy the address from the console, or reading the key: the same gap `gpu-list` closed for stock.

moni_pod already had the data: `extend.ssh_target()` reads `ssh.direct` for `/moni-pod:gpu-extend`. The REST v2
OpenAPI (`Pod` schema, used by both `GET /v2/pods` and `GET /v2/pods/{id}`) carries `ssh.direct`
(host, port, username, command). It is null unless `22/tcp` is published and the running pod has a public port,
so it is absent while provisioning or stopped.

## Decision

1. `ssh_target()` moves to `status.py` (pure, no imports from `start`/`extend`); `extend.py` imports it.
2. `PodRow` gets `ssh_host`, `ssh_port`, `ssh_user`, filled only for live-running pods with `ssh.direct`; otherwise
   null. Stopped pods do not get a stale address. `--offline` (ledger only) has none.
3. Text status prints `ssh: ssh user@host -p port` under each such pod.
4. Discoverability: the SessionStart context for Claude names the exact command
   (`uv run --quiet --project "<plugin dir>" moni-pod status --json`), as ADR-0006 did for `gpu-list`.
5. No token: read-only, no spend. The lock hook lets `moni-pod status` through (checked by feeding the real hook a
   Bash call).

## Why this is safe to expose

Host, port and user are not secrets. Login needs the user's own private key; moni_pod puts the matching public key
on the pod at start (ADR-0005). The address of a pod in the same account is what the RunPod console shows the user.

## Alternatives considered

- **Bridge through the official MCP**: same REST v2 data, same key (Bearer) or a separate OAuth grant that gives
  the agent full RunPod rights. Adds a hop and widens access for no new information.
- **A separate `moni-pod ssh <pod>` command**: possible later if agents want one target; `status --json` already
  lists every pod and was the field request.
- **`ssh.proxy`** (RunPod's SSH proxy, works without a public port): interactive shell only (no scp/rsync/port
  forwarding per the API description), and its username is a routing token. Not exposed; `direct` is what
  `/gpu-extend` and file copying need.

## Result

243 tests pass (6 new). Real read-only run against a live RTX 4090 pod: text and `--json` showed its direct SSH
host, port and user.
