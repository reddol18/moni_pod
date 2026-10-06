---
description: Start a RunPod GPU/CPU pod with an agreed max run time (auto-stop) and a cost estimate you confirm first.
argument-hint: "[gpu or cpu] [hours] [template/image] [budget]"
disable-model-invocation: true
---

The user typed `/moni-pod:gpu-start`. A one-time approval token for this session was just issued by
the moni_pod hook. Request: $ARGUMENTS

Run every command below exactly with this prefix (call it `MONI`):
`uv run --quiet --project "${CLAUDE_PLUGIN_ROOT}" moni-pod`
Session id: `${CLAUDE_SESSION_ID}`

Resuming a stopped moni_pod pod (request mentions resume/restart of an existing pod): run
`MONI start --session "${CLAUDE_SESSION_ID}" --resume <pod id> [--hours h] --dry-run`, show it, ask for an explicit
yes (AskUserQuestion), then run the same without `--dry-run`. Otherwise:

Steps:

1. Work out the spec from the request. Needed: `--gpu "<GPU type id>"` (with `--count`, `--cloud SECURE|COMMUNITY`)
   **or** `--cpu <flavor> --vcpu <n>`; optional `--template <id>` or `--image <image>`, `--hours <max run time>`,
   `--budget <USD>`, `--disk`, `--volume`. If the GPU/CPU choice is missing or ambiguous, ask the user
   (AskUserQuestion) - `MONI list --min-vram <GB>` shows GPUs with live stock and prices to offer as options.
   Do not invent a budget or hours the user did not give; the defaults come from settings.
2. Price it (read-only): `MONI quote --session "${CLAUDE_SESSION_ID}" <spec>`. Show the output to the user as is.
3. If it says `REFUSED`, explain the reasons and stop. Do not look for another way to create the pod.
4. Ask the user to confirm with AskUserQuestion, quoting the hardware, max time and **max cost**.
   Proceed only on an explicit yes.
5. Start: `MONI start --session "${CLAUDE_SESSION_ID}" <exactly the same spec>`. Report the result line.
   If it says `NOT STARTED`, report the reason and stop. If it printed a SECURE quote after a COMMUNITY failure,
   show it and say the user can choose it with `/moni-pod:gpu-start` - do not start it yourself.
6. Tell the user: the pod stops itself at the shown time; check with `/moni-pod:gpu-status`;
   stop or delete with `/moni-pod:gpu-stop`; a stopped pod still costs disk money.

Never create, start or delete pods with the RunPod MCP tools, `runpodctl`, or direct API calls.
