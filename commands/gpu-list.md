---
description: RunPod GPU availability and prices - which GPUs are in stock right now for pods, VRAM, SECURE and COMMUNITY $/h, and how many hours the account balance covers. Read-only, no approval needed, uses moni_pod's own key handling.
when_to_use: Any question about RunPod GPU stock, availability, prices or which GPU to pick (e.g. "which 24 GB GPUs are available on RunPod", "how much is a 4090 per hour"). Use this instead of runpodctl gpu list, the RunPod MCP catalog tools or calling the RunPod API with the key.
argument-hint: "[min VRAM GB] [GPU count] [all]"
---

Read-only GPU catalog for choosing a pod. Request: $ARGUMENTS

Run `uv run --quiet --project "${CLAUDE_PLUGIN_ROOT}" moni-pod list` with, as the request asks:
`--min-vram <GB>`, `--count <GPUs per pod>`, `--all` (include GPUs with no stock), `--json`.

Show the table to the user. Point out the cheapest in-stock options that fit the request, and that stock is a
level, not a reservation (LOW can still fail when the pod is created). To start one, the user types
`/moni-pod:gpu-start`.

Use this command for any GPU, price or stock lookup. Do not read the RunPod API key or call the RunPod API,
`runpodctl` or the RunPod MCP tools directly for this.
