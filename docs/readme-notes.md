# Notes to fold into the README (M6)

Collected while building M0–M5. Each item has its evidence in a report/ADR.

## Requirements
- `uv`, Python ≥ 3.11, a RunPod API key in `RUNPOD_API_KEY` or `.env`.
- `/moni-pod:gpu-extend` needs SSH that works non-interactively: a key without passphrase, or one loaded
  in ssh-agent. On Windows moni_pod uses `C:\Windows\System32\OpenSSH\ssh.exe` (talks to the Windows
  ssh-agent service); Git Bash's `ssh` cannot reach it (M3: `Permission denied (publickey)`). Override with `MONI_POD_SSH`.
- Your local public key (`~/.ssh/id_ed25519.pub`…) is put on each pod as `PUBLIC_KEY`; your RunPod account
  settings are not changed. Opt out with `--no-local-ssh-key`.

## RunPod account safety net (outside Claude Code)
- **The real cap is your prepaid balance: keep Auto-Pay disabled.** With Auto-Pay off, the most you can lose is
  the balance; at $0 RunPod stops your pods (pods without a network volume are terminated - data lost).
- The account **spend limit** (default $80/h across all resources) is shown in `/gpu-start` quotes and
  `/gpu-status` for information only. Users cannot lower it: RunPod raises it automatically with account history,
  and raising it faster needs a support request (https://docs.runpod.io/accounts-billing/billing).

## Guard limits (honest list)
- The lock matches the direct paths an agent normally uses (RunPod MCP, `runpodctl`, raw REST/GraphQL in a
  shell command). Calls hidden in a script file, an SDK, another MCP client, or the web console are not seen.
- Network volumes are not tracked (billed separately, even with no pod).
- Estimates use the rate at creation; community prices can drift. RunPod's billing history posts hours late
  (M3: still empty 40+ min after spend); the account balance and current spend rate move within ~5 min and are
  the live reference shown by `/moni-pod:gpu-status`.
- TTL counts from container start; time spent pulling the image before that may be billed.
- Without a volume, everything on the pod is wiped when the TTL stops it. GPU pods default to a 20 GB volume.
- A stopped community GPU pod resumes on the same host; if the GPU was taken meanwhile it may fail to resume.

## Prior art to cite
- https://github.com/aniket-desh/agents — personal RunPod + Claude Code setup with an allowance ledger and
  watchdog (not a packaged plugin).
- https://github.com/runpod/Runpod-Idle-Pod-Monitor — idle-based stop; no license, so linked, not bundled.
- RunPod's own `stopAfter` never fired and was removed: https://github.com/runpod/runpodctl/pull/330

## Status line (opt-in; plugins cannot set the main status line)
Add to `~/.claude/settings.json`:
```json
{ "statusLine": { "type": "command", "command": "uv run --quiet --project <plugin dir> moni-pod statusline", "refreshInterval": 60 } }
```
Output example: `moni_pod: GPU 1 on $0.13/h, next stop 36m | 1 stopped $0.13/day (1 old!)` — empty when nothing bills.
Ledger only (no network); ~0.4 s per refresh because of `uv run`.

## Session warnings
- Session end: Windows toast (stays in Action Center), macOS `osascript`, Linux `notify-send`; also
  `~/.moni_pod/last_session_end.json`. Claude Code discards SessionEnd output, so nothing appears in the chat.
- Session start: a message listing running pods and stopped pods still billing disk (days stopped, $ so far),
  `!!` after `stale_stopped_hours` (default 24, `~/.moni_pod/config.json`). Never stops or deletes anything.

## More guard limits found in M4
- Hooks filter shell commands by text (`*runpod*`, `*moni-pod*`, `*moni_pod*`) so unrelated calls cost 0 ms.
  A shell **redirect** into `~/.moni_pod` is not seen by these filters, so key 1 (the approval token) can be
  forged that way. Key 2 still stops spending: every spending call raises Claude Code's own permission prompt,
  and M4 verified it appears in every permission mode, including bypassPermissions and auto.
- Serverless endpoints are not locked or tracked (pods only).

## Task 0002 additions (first real use)
- **The lock blocks spending, not key use.** An agent running as your OS user can read `.env` or the environment and
  call RunPod read APIs (or spending APIs from code the hooks cannot see). moni_pod removes the *reason* to do so:
  `/moni-pod:gpu-list` answers GPU stock and price questions without the key, Claude is told so at session start,
  and lookups through `runpodctl gpu list` get a pointer to it. OS-level key isolation is not implemented (ADR-0006).
- Agent-facing line: "For RunPod GPU stock, prices or which GPU to pick, use the Skill `moni-pod:gpu-list`.
  Never read or use the RunPod API key."
- Stock is RunPod's catalog level (NONE/LOW/MEDIUM/HIGH), not a reservation. NONE is refused before creating;
  LOW can still fail. A failed COMMUNITY create shows the SECURE price for the same spec; you choose it again.
- Key lookup order: `RUNPOD_API_KEY` env var, then `.env` in the plugin folder, moni_pod's folder,
  `~/.moni_pod/.env` (recommended for an installed plugin), the working directory, the Claude project folder.
