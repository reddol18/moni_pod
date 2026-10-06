"""PreToolUse lock (ADR-0002). The hook does not judge spending; it only locks.

- Our CLI's spending calls (`moni-pod start|extend`, `moni-pod stop --action terminate`):
  no valid user-minted token → deny; with one → **ask** (key 2: Claude Code's own permission prompt,
  which M4 showed is raised in every permission mode incl. bypassPermissions and auto).
- Every other create/start/restart/terminate path → deny: RunPod MCP tools, runpodctl, raw REST/GraphQL.
- Stopping is never blocked (it only lowers spend).
- Writes to ~/.moni_pod (ledger, tokens) by the agent → deny (no self-issued approvals).
Text matching catches the direct paths an agent normally takes, not deliberate obfuscation (README limits).
"""

from __future__ import annotations

import re
import shlex
from typing import Callable

from . import guard

GATE = {"start": "/moni-pod:gpu-start", "stop": "/moni-pod:gpu-stop", "extend": "/moni-pod:gpu-extend"}

MCP_RUNPOD = re.compile(r"^mcp__.*runpod.*__(?P<tool>[\w-]+)$", re.I)
MCP_DENY = {"create-pod", "delete-pod", "update-pod", "create-cluster", "update-cluster", "delete-cluster"}

RUNPODCTL = re.compile(
    r"\brunpodctl(?:\.exe)?\s+(?:pods?\s+)?(?P<verb>create|start|restart|reset|update|delete|remove|rm|terminate)\b", re.I)
READ_LOOKUP = re.compile(r"\brunpodctl(?:\.exe)?\s+(?:gpu|datacenter|dc|cpu)\s+list\b", re.I)
RUNPOD_HOST = re.compile(r"\b(?:api|rest)\.runpod\.(?:io|ai)\b", re.I)
REST_PODS = re.compile(r"/(?:v\d/)?pods\b", re.I)
MUTATING = re.compile(
    r"-X\s*['\"]?(?:POST|DELETE|PATCH)|--request\s+['\"]?(?:POST|DELETE|PATCH)|-Method\s+['\"]?(?:Post|Delete|Patch)"
    r"|--data\b|--post-data|\s-d\s|requests\.(?:post|delete|patch)|method\s*=\s*['\"](?:POST|DELETE|PATCH)"
    r"|\b(?:POST|DELETE|PATCH)\b", re.I)
SPEND_ACTION = re.compile(r"['\"]?action['\"]?\s*:\s*['\"]?(?:start|restart|terminate)", re.I)
STOP_ONLY = re.compile(r"['\"]?action['\"]?\s*:\s*['\"]?stop\b", re.I)
GQL_MUTATION = re.compile(
    r"pod(?:FindAndDeploy\w*|RentInterruptable|Resume|Terminate|EditJob|Reset|Restart)|deployCpuPod", re.I)
LEDGER_PATH = re.compile(r"\.moni_pod[\\/]", re.I)
WRITEISH = re.compile(r">|\b(?:rm|del|mv|cp|tee|sed\s+-i|Remove-Item|Set-Content|Add-Content|Out-File|Move-Item|"
                      r"Copy-Item|New-Item)\b", re.I)
OUR_CLI = re.compile(r"\bmoni-pod(?:\.exe)?\s+(?P<sub>start|stop|extend)\b(?P<rest>[^;&|]*)")


LOOKUP_HINT = " For GPU, price or stock lookups use `moni-pod list` (/moni-pod:gpu-list) instead of the API key."


def deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": "moni_pod: " + reason + LOOKUP_HINT}}


def hint(text: str) -> dict:
    """No decision (the read goes ahead) - just steer Claude to the keyless lookup (task 0002)."""
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "moni_pod: " + text}}


def ask(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                   "permissionDecisionReason": "moni_pod: " + reason}}


def _split(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _opt(args: list[str], name: str) -> str | None:
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def check_mcp(tool: str, tool_input: dict) -> dict | None:
    m = MCP_RUNPOD.match(tool)
    if not m:
        return None
    name = m.group("tool").lower()
    if name in MCP_DENY:
        verb = "create" if name.startswith("create") else ("delete" if name.startswith("delete") else "change")
        return deny(f"{name} is locked. To {verb} a pod the user types {GATE['start'] if verb == 'create' else GATE['stop']}.")
    if name == "pod-action":
        action = str(((tool_input or {}).get("body") or {}).get("action", "")).lower()
        if action != "stop":
            return deny(f"pod-action '{action}' is locked (only stop is allowed). "
                        f"Start/resume: the user types {GATE['start']}; delete: {GATE['stop']}.")
    return None


def check_shell(command: str, session_id: str, explain: Callable[[str, list[str], str], str] | None) -> dict | None:
    if not command:
        return None
    m = RUNPODCTL.search(command)
    if m:
        return deny(f"`runpodctl {m.group('verb')}` is locked. Use {GATE['start']} / {GATE['stop']}. "
                    "Stopping (`runpodctl pod stop`) is allowed.")
    if RUNPOD_HOST.search(command):
        if "graphql" in command.lower() and GQL_MUTATION.search(command):
            return deny("RunPod GraphQL pod mutations are locked. Use the moni_pod commands.")
        if REST_PODS.search(command) and MUTATING.search(command):
            if STOP_ONLY.search(command) and not SPEND_ACTION.search(command):
                return None  # direct stop is allowed
            return deny(f"direct RunPod REST pod changes are locked. Use {GATE['start']} / {GATE['stop']}.")
    if LEDGER_PATH.search(command) and WRITEISH.search(command):
        return deny("~/.moni_pod (ledger, approvals) is written only by moni_pod itself.")
    if READ_LOOKUP.search(command) or (RUNPOD_HOST.search(command) and "catalog" in command.lower()):
        return hint("this RunPod lookup needs the API key; `moni-pod list` (Skill 'moni-pod:gpu-list') "
                    "answers GPU stock and price questions without it.")

    calls = list(OUR_CLI.finditer(command))
    spending = []
    for c in calls:
        args = _split(c.group("rest"))
        sub = c.group("sub")
        if sub == "start" and "--dry-run" in args:
            continue
        if sub == "stop" and _opt(args, "--action") != "terminate":
            continue  # stop: allowed without approval
        spending.append((sub, args))
    if not spending:
        return None
    if len(spending) > 1:
        return deny("run one spending moni-pod command at a time.")
    sub, args = spending[0]
    kind = "stop" if sub == "stop" else sub
    claimed = _opt(args, "--session")
    if claimed and session_id and claimed != session_id:
        return deny("--session must be this Claude Code session's id.")
    if guard.find_valid(kind, session_id) is None:
        return deny(f"no approval for this. The user must type {GATE[kind]} first "
                    "(approvals are per session, single use, 15 min).")
    try:
        reason = explain(sub, args, session_id) if explain else ""
    except (Exception, SystemExit) as e:  # pricing or arg parsing failed: still ask, say so
        reason = f"(could not price it: {e})"
    return ask(reason or f"confirm `moni-pod {sub}`")


def check_write(tool: str, tool_input: dict) -> dict | None:
    path = str((tool_input or {}).get("file_path") or (tool_input or {}).get("notebook_path") or "")
    if LEDGER_PATH.search(path.replace("\\", "/")):
        return deny("~/.moni_pod (ledger, approvals) is written only by moni_pod itself.")
    return None


def handle_pre_tool_use(event: dict, explain=None) -> dict | None:
    tool = event.get("tool_name", "")
    ti = event.get("tool_input") or {}
    if tool.startswith("mcp__"):
        return check_mcp(tool, ti)
    if tool in ("Bash", "PowerShell"):
        return check_shell(str(ti.get("command", "")), event.get("session_id", ""), explain)
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return check_write(tool, ti)
    return None
