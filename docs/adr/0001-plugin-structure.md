# ADR-0001 — Plugin structure

- Status: **accepted — 2026-10-06 HQ approval (지휘부 승인)**
- Verified against: Claude Code 2.1.290, docs fetched 2026-10-06
  (`code.claude.com/docs/en/plugins-reference`, `/hooks`, `/statusline`),
  RunPod plugin `runpod@runpod` 1.4.0, RunPod MCP server `4.0.0 [specgen]`, REST v2 OpenAPI (`https://api.runpod.io/v2/openapi.json`)

## Context

moni_pod must ship user commands (`/gpu-start`, `/gpu-status`, `/gpu-stop`, `/gpu-extend`),
lock hooks, a session-end warning and, optionally, a status line, as a single Claude Code plugin
that sits next to the official RunPod plugin.

## Facts established in M0

| Topic | Finding | Source |
|---|---|---|
| Manifest | `.claude-plugin/plugin.json`; only `name` required (kebab-case). Other dirs (`commands/`, `skills/`, `hooks/`) live at plugin root | plugins-reference |
| Commands | `commands/*.md` still supported; docs say "Prefer `skills/` for new plugins". Plugin commands are namespaced `/<plugin>:<name>` | plugins-reference |
| Hooks | `hooks/hooks.json` (wrapped in top-level `"hooks"`) is always loaded; `${CLAUDE_PLUGIN_ROOT}` and `${CLAUDE_PLUGIN_DATA}` expand inside hook commands | plugins-reference |
| Persistent data | `${CLAUDE_PLUGIN_DATA}` = `~/.claude/plugins/data/<id>/`, kept across updates, **deleted on uninstall** by default | plugins-reference |
| Status line | Plugin `settings` honours only `agent` and `subagentStatusLine`. **A plugin cannot set the main `statusLine`**; user must add it to their `settings.json` | plugins-reference |
| Status line runtime | Any command; stdin JSON includes `session_id`, `cost.*`; event-driven, 300 ms debounce, optional `refreshInterval` (≥1 s). On Windows runs via Git Bash if present, else PowerShell | statusline |
| Local dev | `claude --plugin-dir <dir>`; `claude plugin validate <dir>` | experiment (validated fake plugin) |

## Decision

```
moni_pod/
├─ .claude-plugin/plugin.json        # name: "moni-pod"
├─ .claude-plugin/marketplace.json   # single-plugin marketplace → `/plugin marketplace add <repo>`
├─ commands/gpu-start.md, gpu-status.md, gpu-stop.md, gpu-extend.md
├─ hooks/hooks.json                  # PreToolUse lock, UserPromptExpansion token mint,
│                                    # SessionStart reminder, SessionEnd warning (see ADR-0002, M5)
├─ src/moni_pod/ …                   # as PLAN §4 (ledger, runpod_api, cost, guard, ttl, cli)
└─ statusline/moni_pod_status.py     # opt-in; README shows the 3-line settings.json snippet
```

1. **Commands in `commands/`** (flat .md). Docs: "Custom commands have been merged into skills";
   command files "still work" and support the same frontmatter except `name`/`paths`. Both are
   model-invocable by default, so **every spending command sets `disable-model-invocation: true`**
   (Claude must not call `/gpu-start` on its own). Token minting additionally relies on
   `UserPromptExpansion`, which fires only for user-typed commands (ADR-0002).
2. **Ledger lives at `~/.moni_pod/ledger.json`, not `${CLAUDE_PLUGIN_DATA}`.** Uninstalling the
   plugin must not erase the record of Pods that are still billing. (Deviation-free: PLAN already says `~/.moni_pod`.)
3. **RunPod calls go through our own REST v2 client (`runpod_api.py`, base `https://api.runpod.io/v2`)**,
   not through the official MCP. The MCP server is itself generated from REST v2
   (`create-pod{body}`, `pod-action{id, body:{action}}`, `delete-pod{id}`, `list-pod-billing{…}`),
   so we lose nothing, and our CLI is the only path the lock lets through (ADR-0002).
4. **Python entry point run with `uv run`** from `${CLAUDE_PLUGIN_ROOT}`; hooks use the same interpreter.
   Hook commands avoid Git-Bash-specific syntax (forward-slash paths, quoted).
5. **Status line is opt-in and documented, not auto-installed**, because plugins cannot set it.
   `/gpu-status` remains the primary view.

## Alternatives considered

- `skills/<name>/SKILL.md` instead of `commands/` — equivalent; flat command files are enough (no supporting files needed). Revisit if a command needs bundled scripts.
- Driving RunPod through the official MCP from our commands — rejected: the lock would have to
  distinguish "our" MCP call from the agent's identical one; a separate CLI path is simpler to gate.
- Ledger in `${CLAUDE_PLUGIN_DATA}` — rejected (deleted on uninstall).

## Consequences

- Command names appear as `/moni-pod:gpu-start` etc. (plugin namespace) — README must say so.
- Users who want the status line edit `settings.json` once.
- We depend on REST v2 contract; pin field names in tests from the OpenAPI document.
