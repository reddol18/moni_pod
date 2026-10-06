# moni-pod

[![CI](https://github.com/reddol18/moni_pod/actions/workflows/ci.yml/badge.svg)](https://github.com/reddol18/moni_pod/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)
![Platforms](https://img.shields.io/badge/tested-Linux%20%7C%20Windows%20%7C%20macOS-informational.svg)
[![Claude Code plugin](https://img.shields.io/badge/Claude%20Code-plugin-d97757.svg)](https://code.claude.com/docs/en/plugins)

**English** | [한국어](README.ko.md)

A Claude Code plugin that keeps an AI agent from running up a RunPod GPU bill.
You start and stop pods yourself with explicit commands; every other path an agent could use to create
or start a pod is locked; and each pod stops itself when the agreed time runs out, even if your PC is off.

```
/moni-pod:gpu-list 24           # which 24 GB+ GPUs are in stock, $/h, hours your balance covers
/moni-pod:gpu-start 4090 2h     # quote -> you confirm -> pod starts with a 2 h auto-stop
/moni-pod:gpu-status            # running/stopped pods, TTL left, cost so far, disk still billing
/moni-pod:gpu-extend pod-1 1h   # push back the auto-stop, extra cost confirmed first
/moni-pod:gpu-stop pod-1        # "results copied off?" -> stop or delete -> final cost vs billing
```

## Why

Once an agent can create cloud GPUs from a conversation (RunPod's official Claude Code plugin does exactly
that), the cost is no longer in any single tool call. A pod-create call costs $0 when it runs. The money
builds up for **as long as the pod stays on**, and nothing in the loop owns that time.

| Existing tool | What it does | What it leaves open |
|---|---|---|
| [RunPod official Claude Code plugin](https://github.com/runpod/runpod-plugins-official) | Skills + hosted MCP: create/stop/delete pods, prices, spend | An agent can turn GPUs on by itself; nothing limits spend |
| [Runpod Idle Pod Monitor](https://github.com/runpod/Runpod-Idle-Pod-Monitor) | Stops a pod when CPU/GPU/memory use stays low | Looks at utilization only. A busy pod nobody needs keeps billing, and it has no budget or owner |
| Per-call agent budget guards | Block a tool call by the cost of that call | The create call costs $0, so a per-call guard cannot stop time-based spend |
| RunPod `stopAfter` | Was meant to stop a pod at a set time | Never fired and was removed ([runpod/runpodctl PR 330](https://github.com/runpod/runpodctl/pull/330)) |
| [aniket-desh/agents](https://github.com/aniket-desh/agents) | A personal RunPod + Claude Code setup with an allowance ledger and watchdog | Not a packaged plugin |

moni-pod covers the part those tools leave open: the **lifetime** of a pod an agent works with. You agree on
the start and the max cost, the spend is tracked while it runs, and you close it out yourself. It does not do
idle detection; use the official Idle Monitor for that.

## How it works

1. **You start and stop pods yourself.** `/gpu-start`, `/gpu-stop` and `/gpu-extend` cannot be invoked by the
   model. Typing one issues a one-time approval token, and the command shows a quote you confirm.
2. **Hooks lock everything else, and never decide anything themselves.** Create, start, update and delete calls through the RunPod
   MCP, `runpodctl` or raw REST/GraphQL in a shell are denied unless they carry that token. Every spending call
   also raises Claude Code's own permission prompt. This was verified in every permission mode, including
   `bypassPermissions` and `auto`.
3. **The last line of defense runs inside the pod.** The pod's entrypoint is wrapped with a timer, and the pod
   stops itself when the agreed time (TTL) runs out. It does not depend on your PC or Claude Code being alive.
   Default 2 h, cap 8 h, session budget $2; all of these can be changed in settings.
4. **Nothing is deleted automatically.** Deleting happens only in `/gpu-stop`, after you answer whether the results
   were copied off.
5. **Stopped does not mean free.** A stopped pod still pays for its disk; status, session start and the status line
   show how much and for how long.

Measured on real pods during development: the ledger estimate was within **0.4 %** of the actual charge
(7 pods, $0.1003 actual vs $0.1007 estimated). TTL self-stop, stop, delete, resume and in-place extend were each
tested on a live pod.

## Install

Requirements: [Claude Code](https://claude.com/claude-code), [uv](https://docs.astral.sh/uv/), Python 3.11+,
and a RunPod API key.

In Claude Code:

```
/plugin marketplace add reddol18/moni_pod
/plugin install moni-pod@moni-pod
```

Put the key where moni-pod can find it, for example `~/.moni_pod/.env`:

```
RUNPOD_API_KEY=your-key
```

Lookup order: the `RUNPOD_API_KEY` environment variable, then `.env` in the plugin folder,
`~/.moni_pod/.env`, the working directory, and the Claude project folder.

**Also on the RunPod side: keep Auto-Pay off.** With prepaid credit and Auto-Pay disabled, the most you can
lose is your balance. When the balance hits $0, RunPod stops your pods (pods without a network volume are
terminated). RunPod's spend limit (default $80/h) is shown in quotes and status for information only. You cannot
lower it ([RunPod billing docs](https://docs.runpod.io/accounts-billing/billing)).

### Optional

- **`/gpu-extend` needs non-interactive SSH**: a key without a passphrase, or one loaded in ssh-agent. Your local
  public key (`~/.ssh/id_ed25519.pub` and so on) is put on each pod; your RunPod account settings are not changed.
  Opt out with `--no-local-ssh-key`. On Windows, moni-pod uses the built-in OpenSSH (`C:\Windows\System32\OpenSSH\ssh.exe`);
  override with `MONI_POD_SSH`.
- **Status line** (plugins cannot set it; add it to `~/.claude/settings.json`):
  ```json
  { "statusLine": { "type": "command", "command": "uv run --quiet --project <plugin dir> moni-pod statusline", "refreshInterval": 60 } }
  ```
  Output example: `moni_pod: GPU 1 on $0.13/h, next stop 36m | 1 stopped $0.13/day (1 old!)`. It prints nothing when nothing is billing.
- **Session warnings**: when a session ends, you get an OS notification (Windows toast, macOS, Linux `notify-send`).
  When the next session starts, you get a list of running pods and of stopped pods still paying for disk. Both only
  warn; neither stops or deletes anything.

## Limits

These are the known gaps. Each one was found during development and is written up in [`docs/adr/`](docs/adr) and
[`docs/reports/`](docs/reports).

- The lock matches the direct paths an agent normally uses: RunPod MCP, `runpodctl`, and REST/GraphQL in a shell
  command. It cannot see calls hidden in a script file, an SDK, another MCP client, or the web console. The in-pod
  TTL and your prepaid balance are the backstops.
- The lock blocks spending, not reading the key. An agent running as your OS user can read `.env`. moni-pod removes
  the reason to: `/moni-pod:gpu-list` answers stock and price questions without the key, and Claude is told so at
  session start. OS-level key isolation is not implemented ([ADR-0006](docs/adr/0006-stock-lookup-and-key-access.md)).
- A shell redirect into `~/.moni_pod` can forge the approval token. The native permission prompt still guards every
  spending call ([ADR-0002](docs/adr/0002-lock-mechanism.md)).
- Pods only. Serverless endpoints and network volumes are not locked or tracked.
- Estimates use the price at creation, and community prices can drift. RunPod's billing history posts hours late,
  so `/gpu-status` uses the account balance and current spend rate as the live reference.
- The TTL counts from container start. Image pull time before that may be billed.
- Stock is RunPod's catalog level (NONE/LOW/MEDIUM/HIGH), not a reservation. NONE is refused before creating;
  LOW can still fail. If a COMMUNITY create fails, the SECURE price for the same spec is shown, and you choose
  whether to start it.
- Without a volume, everything on the pod is wiped when the TTL stops it. GPU pods get a 20 GB volume by default.

## Development

```
uv run pytest
```

Tests use a mock RunPod client; no test calls the real API. Design notes are in [`docs/PLAN.md`](docs/PLAN.md)
and [`docs/adr/`](docs/adr), and milestone reports with measurements are in [`docs/reports/`](docs/reports).
The plan, reports and task files are partly in Korean.

## License

[MIT](LICENSE)
