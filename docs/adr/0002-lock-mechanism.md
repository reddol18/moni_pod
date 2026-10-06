# ADR-0002 — Lock mechanism (gate token)

- Status: **accepted — 2026-10-06 HQ approval (지휘부 승인)**
- Verified against: Claude Code 2.1.290 (docs `hooks.md` fetched 2026-10-06 + headless experiments
  with a fake plugin), RunPod MCP server `4.0.0 [specgen]` (`tools/list`), runpodctl 2.14.0

## Context

PLAN §2.2: hooks do not judge, they only lock. Pod create / start / terminate must happen only
inside a user-typed `/gpu-start` / `/gpu-stop` flow, after the user saw the estimate. All other
paths (official MCP, `runpodctl`, raw REST/GraphQL) are refused.
PLAN §4 proposed: the command writes a one-time token after the user confirms; the hook allows
exactly one matching call.

Weak point of that proposal: the "user confirmed" step is performed by the agent (it writes the
token), so an agent can mint a token without the user. M0 found two Claude Code primitives that
put the user, not the agent, at both ends.

## Facts established in M0

| # | Fact | How verified |
|---|---|---|
| F1 | Plugin MCP tools are named `mcp__plugin_<plugin>_<server>__<tool>`. Official plugin → `mcp__plugin_runpod_runpod__create-pod` etc. A user-added server (`claude mcp add runpod`) → `mcp__runpod__create-pod`. | docs (hooks.md) + experiment: fake plugin `fakerp` server `runpod` produced `mcp__plugin_fakerp_runpod__create-pod`; official server shows as `plugin:runpod:runpod` |
| F2 | Matchers are regex; `if` takes one permission rule (`Bash(runpodctl *)`), checks every subcommand incl. `$()`/backticks, strips leading `VAR=`. | docs |
| F3 | PreToolUse `permissionDecision:"deny"` blocks **even in `bypassPermissions`**; reason is shown to Claude. Precedence `deny > defer > ask > allow`. | docs + experiment (bypassPermissions run: create-pod blocked, list-pods passed; Claude saw "hook error: <reason>") |
| F4 | PreToolUse `"ask"` shows the native permission prompt with `permissionDecisionReason` and a `[plugin:<name>]` label; in auto mode the classifier cannot silently approve it. In `-p` runs it becomes a denial. | docs (interactive behaviour to be confirmed in M4) |
| F5 | `UserPromptExpansion` fires when the **user types** a slash command; input has `command_name` (namespaced, e.g. `fakerp:gpu-start`), `command_args`, `command_source:"plugin"`, `expansion_type:"slash_command"`. | experiment (`-p "/fakerp:gpu-start rtx4090 2h"`) |
| F6 | A command with `disable-model-invocation: true` cannot be invoked by Claude via the Skill tool. | experiment (Claude reported it as unknown) |
| F7 | MCP mutating tools relevant to Pod spend: `create-pod{body}`, `pod-action{id, body:{action: start\|stop\|restart\|terminate}}`, `delete-pod{id}`, `update-pod{id, body}` (env/image change resets the container), `create-cluster`/`update-cluster`/`delete-cluster` (multi-Pod). | live `tools/list` (77 tools) |
| F8 | runpodctl 2.14 verbs: `pod create\|start\|stop\|delete` (alias `remove`/`rm`), legacy `create pod`, `start pod`, `remove pod`. | `runpodctl --help` |

## Decision

**Two-key gate: the user turns key 1 by typing the command, and key 2 by clicking the native
permission prompt. The agent holds neither.**

1. **Mint (key 1)** — `UserPromptExpansion` hook, matcher `^moni-pod:gpu-(start|stop|extend)$`.
   Writes a token `{id, kind, session_id, created, expires (+15 min), used:false}` to
   `~/.moni_pod/tokens.json`. Only a user-typed command produces this event (F5, F6).
2. **Execute** — the command prompt tells Claude to gather the spec, show the estimate, then run
   our CLI, e.g. `uv run moni-pod start --gpu ... --hours 2 --budget 5 --token <id>`.
   Our CLI is the only code that calls RunPod's create / start / terminate (REST v2).
3. **Confirm (key 2)** — PreToolUse hook on `Bash`/`PowerShell` with `if: Bash(*moni-pod start*)` (etc.):
   - no valid unused token for this session and kind → **deny** ("type /moni-pod:gpu-start");
   - otherwise → **ask**, `permissionDecisionReason` = "RTX 4090 ×1, max 2 h, ≈ $1.38 (+disk $0.0x/h), TTL auto-stop at 15:42" computed by the hook itself from the CLI args and the live price, not from Claude's text.
   The CLI re-checks and **consumes** the token (single use) before calling RunPod.
4. **Lock** — PreToolUse deny, no token exception (our CLI does not use these paths):
   - MCP: matcher `^mcp__.*runpod.*__(create-pod|delete-pod|update-pod|create-cluster|update-cluster|delete-cluster)$`
     and `pod-action` with `body.action` ∈ {`start`,`restart`,`terminate`}. **`stop` is allowed** — it only reduces spend.
   - Bash/PowerShell: `runpodctl` with `pod create|start|delete|remove|rm`, legacy `create pod|start pod|remove pod`;
     `curl`/`wget`/`Invoke-RestMethod`/`Invoke-WebRequest`/`python -c` whose text hits
     `api.runpod.io/v2/pods`, `rest.runpod.io/v1/pods` or `api.runpod.io/graphql` (with
     `podFindAndDeploy|podRentInterruptable|podResume|podTerminate`) using POST/DELETE/PATCH.
   - Read-only calls (`list-*`, `get-*`, `pod stop`, billing) pass.
5. Every deny reason names the gate command, so the agent tells the user what to type.

## Rationale

- Token minted by a user-only event instead of by the agent closes the "agent writes its own
  approval" hole of the PLAN §4 draft, without changing PLAN §2 principles.
- `ask` puts the spend confirmation in Claude Code's own UI, which the agent cannot answer.
- Locking by deny (not by token-matching MCP arguments) keeps the hook dumb: one allow path, our CLI.

## Alternatives

- PLAN §4 draft (agent-written token after in-chat confirmation) — weaker; kept only as fallback if `ask` turns out not to prompt in some mode.
- Gate MCP `create-pod` with a token matched on `body` — rejected: brittle spec matching, and
  the token would still be agent-written.
- Remove the official plugin's MCP — out of scope; users keep it for read-only use.

## Known limits (README "guard limits")

- Hidden calls defeat text matching: a script file the agent writes then runs, base64, an SDK call
  inside `python script.py`, a second MCP client, the RunPod web console. The guard catches the
  direct paths an agent normally takes, not an adversary.
- An agent could write `~/.moni_pod/tokens.json` itself. Mitigation: deny Write/Edit/Bash
  targeting that path; the `ask` prompt still requires the user. Not airtight.
- Last lines of defence stay outside Claude Code: in-Pod TTL (ADR-0003) and account-side limits
  (prepaid credit, auto-pay off).

## To confirm in M4

- `ask` actually prompts in default / acceptEdits / auto / bypassPermissions interactive sessions.
- Hook `if` patterns on the PowerShell tool on Windows.

## M4 results (2026-10-06)

- **Condition A confirmed.** A PreToolUse `ask` was routed to the permission prompt (observed through
  `--permission-prompt-tool`) and the command did not run without an answer, in all five modes:
  default, acceptEdits, dontAsk, **bypassPermissions**, and **auto** (auto verified on Sonnet; on Haiku the
  `-p` run silently fell back to default). The two-step `/moni-pod:gpu-confirm` fallback is not needed.
- Live gate run (bypassPermissions, real plugin): user command → token → `quote` ran **without** a prompt →
  `moni-pod start` was the **only** call that prompted → approved → pod created. The agent used the PowerShell
  tool, so the PowerShell path is covered too.
- Live deny run (real plugin + a fake MCP server named `runpod`): `runpodctl pod create`, REST POST, MCP
  `create-pod`, MCP `pod-action start`, a mismatched `--session` → denied; MCP `pod-action stop` → allowed.
- **Finding:** hook `if` filters such as `Bash(*moni_pod*)` match command words but **not redirect targets**
  (`echo {} > ~/.moni_pod/tokens.json` slipped through and overwrote the token file). The token loader now fails
  closed on malformed content.
- **Performance decision (HQ, after M4)**: running the hook on every shell call cost ~359 ms/call (uv + Python).
  A bash-builtin prefilter measured 115 ms (unrelated) and the floor of *any* shell-form hook on Windows
  (`bash -c exit` under Git Bash) is 69 ms, above the 50 ms target. Only `if` filters avoid spawning a process:
  unrelated calls then cost **0 ms** (verified: `ls`, `git`, a normal Write and a PowerShell call never reached the
  hook). Final config: `Bash(*runpod*|*RunPod*|*moni-pod*|*moni_pod*)`, the same for PowerShell, and
  `Write|Edit(~/.moni_pod/**)` (verified: Write of a new file there reached the hook and was denied).
- **Accepted residual**: a shell **redirect** into `~/.moni_pod` is not seen by `if` filters. Impact is bounded: a
  forged token only replaces key 1; every spending call still needs key 2, Claude Code's own permission prompt,
  which the agent cannot answer and which M4 showed is raised in **every** permission mode (default, acceptEdits,
  dontAsk, bypassPermissions, auto). HQ accepted this residual on 2026-10-06. Listed in README limits with other text-matching gaps.
- Out of scope, noted: serverless endpoints (`create-endpoint`, `deploy-hub-repo`) are not locked (PLAN: pods only).
