---
description: Show RunPod pods - running/stopped, elapsed time, TTL left, estimated cost. Read-only.
disable-model-invocation: true
---

## moni_pod status (already fetched, read-only)

!`uv run --quiet --project "${CLAUDE_PLUGIN_ROOT}" moni-pod status --billing --session "${CLAUDE_SESSION_ID}"`

Show the status above to the user, in the user's language. Rules:

- Do not call any RunPod tool, `runpodctl`, or API to "fix" anything. This command is read-only.
- Lead with running pods and their TTL left. Point out every note line (TTL failed, over budget, sync pending, stopped-but-billing-disk).
- Remind the user that a stopped pod still costs disk money, and that the figures are estimates; RunPod billing is final and lags.
- If something needs action, tell the user which command to type (`/moni-pod:gpu-stop`, `/moni-pod:gpu-extend`); do not do it yourself.
