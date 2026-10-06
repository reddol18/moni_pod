# ADR-0006 — Stock in quotes, a keyless lookup command, and what the lock does not cover

- Status: proposed (task 0002, 2026-10-07), pending HQ review
- Trigger: first real use (an agent in another project folder, 2026-10-06)

## Context

Field report: `quote` said `OK to start` for 10 GPU options, but only 2 could be created; COMMUNITY RTX 4090 failed
twice and A5000 once (`no longer any instances available`), SECURE 4090 worked first time. With no lookup path,
the agent read `moni_pod/.env` and called REST/GraphQL with the key itself. Run from another folder, `moni-pod`
did not find `.env` at all (cwd-only lookup).

## Decision

1. **Stock source: REST v2 only.** `GET /v2/catalog/gpus?include=AVAILABILITY&product=POD&cloud=SECURE|COMMUNITY&count=N`
   returns `availability` NONE/LOW/MEDIUM/HIGH per GPU and cloud (CPU: `/v2/catalog/cpus?include=AVAILABILITY&product=POD&vcpuCount=N`).
   Not GraphQL `lowestPrice` (being retired; its empty-when-unavailable behaviour is undocumented).
   The catalog also lists placeholder prices for clouds a GPU is not offered on; prices are kept only where the
   `secure`/`community` flag is true.
2. **Quote rules**: NONE → refused before anything is created, with in-stock alternatives (same GPU on the other
   cloud first, then the cheapest GPUs with at least the same VRAM). LOW → allowed with a warning (the field run saw
   COMMUNITY 4090 fail while LOW). Query failed → `OK (price only - stock not checked)`.
3. **Create failure on COMMUNITY** → nothing is created; the same spec is priced on SECURE and shown, and the user
   must start it again with `/moni-pod:gpu-start`. Never switched automatically (SECURE can cost 2x).
4. **`moni-pod list` / `/moni-pod:gpu-list`**: read-only, model-invocable, no token. VRAM, SECURE/COMMUNITY $/h,
   stock, hours the balance covers. Purpose: no reason left for an agent to use the key.
   Discoverability, verified headless: with the command alone, Haiku guessed `runpod:gpu-list`, then fell back to
   the official plugin's `runpodctl gpu list`. Added: a one-line SessionStart context ("for RunPod GPU stock/prices
   invoke 'moni-pod:gpu-list'") and a non-blocking PreToolUse hint on `runpodctl gpu|datacenter|cpu list` and
   catalog URLs. After that the same question went straight to `moni-pod:gpu-list`.
5. **`.env` lookup independent of cwd**: env var → `${CLAUDE_PLUGIN_ROOT}/.env` → moni_pod's own folder →
   `~/.moni_pod/.env` → cwd → Claude project dir. Another project's `.env` can no longer win over moni_pod's.

## What the lock does not do (stated in README)

The lock blocks **spending calls**; it does not stop **use of the key**. An agent running as the same OS user can
read `.env` (or the environment) and call any read API, or a spending API from code the hooks cannot see. Key 2
(the native permission prompt) only guards calls that go through Claude Code tools the hooks match.

## Alternatives considered (not implemented)

- **OS-level isolation of the key**: run moni_pod's API calls as a separate OS user, or keep the key in the
  Windows Credential Manager / macOS Keychain and fetch it only inside moni_pod. Both stop casual `.env` reads but
  not a determined agent with the same user's rights (it can call the credential store too), and add install
  complexity. Revisit for M6 if needed.
- **Block read-only RunPod lookups** (deny `runpodctl gpu list`): rejected — PLAN §2.2 says hooks lock spending,
  and a keyless alternative plus a hint removes the incentive without new denials.
