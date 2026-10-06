---
description: Stop or delete a RunPod pod started with moni_pod, after confirming results were copied off; shows the final cost.
argument-hint: "[pod name or id]"
disable-model-invocation: true
---

The user typed `/moni-pod:gpu-stop`. A one-time approval token for deleting was just issued by the moni_pod hook.
Request: $ARGUMENTS

Prefix every command with `uv run --quiet --project "${CLAUDE_PLUGIN_ROOT}" moni-pod` (call it `MONI`).
Session id: `${CLAUDE_SESSION_ID}`

Current pods:

!`uv run --quiet --project "${CLAUDE_PLUGIN_ROOT}" moni-pod status --session "${CLAUDE_SESSION_ID}"`

Steps:

1. Pick the pod. If the request does not name exactly one pod from the list above, ask the user (AskUserQuestion).
   Only pods marked `*` or `+` (created by moni_pod) can be handled here.
2. Ask the user (AskUserQuestion), in this order:
   a. "Have you copied the results you need off this pod?" — yes / no.
   b. "Stop (keeps the disk; disk still costs money) or delete (everything on the pod is gone)?"
      If the answer to (a) was **no**, recommend Stop. Offer Delete only together with an explicit
      "delete anyway, I don't need the results" option, and use `--discard-unretrieved` only if the user picks it.
3. Run one of:
   - `MONI stop --session "${CLAUDE_SESSION_ID}" --pod <id> --action stop --retrieved <yes|no>`
   - `MONI stop --session "${CLAUDE_SESSION_ID}" --pod <id> --action terminate --retrieved <yes|no> [--discard-unretrieved]`
4. Show the result: estimated cost vs RunPod billed amount. If billing is still pending (RunPod posts it late),
   say so and that `MONI reconcile` (or the next `/moni-pod:gpu-status`) will settle it.
   For a stopped pod, repeat the disk cost per day it keeps charging.

Never stop or delete pods with the RunPod MCP tools, `runpodctl`, or direct API calls, and never delete
without the user's answers above.
