---
description: Push back the auto-stop time of a running moni_pod pod (in place, over SSH), with the extra cost confirmed first.
argument-hint: "[pod name or id] [hours to add]"
disable-model-invocation: true
---

The user typed `/moni-pod:gpu-extend`. A one-time approval token was just issued by the moni_pod hook.
Request: $ARGUMENTS

Prefix every command with `uv run --quiet --project "${CLAUDE_PLUGIN_ROOT}" moni-pod` (call it `MONI`).
Session id: `${CLAUDE_SESSION_ID}`

Current pods:

!`uv run --quiet --project "${CLAUDE_PLUGIN_ROOT}" moni-pod status --session "${CLAUDE_SESSION_ID}"`

Steps:

1. Pick the running pod and the hours to add. Ask (AskUserQuestion) if either is missing.
2. Tell the user the extra cost: hours × the pod's $/h shown above, and the new auto-stop time.
   Ask for an explicit yes (AskUserQuestion).
3. Run `MONI extend --session "${CLAUDE_SESSION_ID}" --pod <id> --hours <h>` and report the result line.
4. If it says `NOT EXTENDED`, report the reason. If SSH is the problem, explain the fallback: let the pod stop at
   its time (copy results to `/workspace` first; the container disk is wiped on stop), then
   `/moni-pod:gpu-start --resume <pod>`. Do not try other ways to keep the pod running.
