"""Gate tokens (ADR-0002, key 1).

A token is minted only by the `UserPromptExpansion` hook, i.e. when the *user types*
`/moni-pod:gpu-start` (or gpu-stop / gpu-extend). The CLI consumes one token per spending action.
Tokens are bound to the Claude Code session, single-use, and expire after TOKEN_TTL_SEC.
"""

from __future__ import annotations

import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from . import config
from .fsutil import file_lock, read_json, write_json_atomic
from .ledger import parse_iso, to_iso, utcnow

TOKEN_TTL_SEC = 15 * 60
KINDS = ("start", "stop", "extend")
COMMAND_KIND = {f"moni-pod:gpu-{k}": k for k in KINDS}


class TokenError(RuntimeError):
    pass


@dataclass
class Token:
    id: str
    kind: str
    session_id: str
    created: str
    expires: str
    used: str | None = None
    args: str = ""


def _paths():
    home = config.home_dir()
    return home / "tokens.json", home / "tokens.lock"


def _load() -> list[Token]:
    """Malformed or tampered content counts as "no approvals" (fail closed), never as an error."""
    path, _ = _paths()
    try:
        raw = read_json(path, {"tokens": []}).get("tokens", [])
        return [Token(**t) for t in raw if isinstance(t, dict)]
    except (ValueError, TypeError, AttributeError):
        return []


def _save(tokens: list[Token], now: datetime) -> None:
    path, _ = _paths()
    keep = [t for t in tokens if parse_iso(t.expires) > now - timedelta(days=1)]  # prune old
    write_json_atomic(path, {"tokens": [asdict(t) for t in keep]})


def mint(kind: str, session_id: str, args: str = "", now: datetime | None = None) -> Token:
    if kind not in KINDS:
        raise ValueError(kind)
    if not session_id:
        raise ValueError("session_id required")
    now = now or utcnow()
    tok = Token(id=secrets.token_hex(4), kind=kind, session_id=session_id, created=to_iso(now),
                expires=to_iso(now + timedelta(seconds=TOKEN_TTL_SEC)), args=args)
    _, lock = _paths()
    with file_lock(lock):
        tokens = _load()
        tokens.append(tok)
        _save(tokens, now)
    return tok


def find_valid(kind: str, session_id: str, now: datetime | None = None) -> Token | None:
    now = now or utcnow()
    for t in reversed(_load()):
        if t.kind == kind and t.session_id == session_id and t.used is None and parse_iso(t.expires) > now:
            return t
    return None


def consume(kind: str, session_id: str, token_id: str | None = None, now: datetime | None = None) -> Token:
    """Mark one valid token used. Raises TokenError with a user-facing reason otherwise."""
    now = now or utcnow()
    _, lock = _paths()
    with file_lock(lock):
        tokens = _load()
        for t in reversed(tokens):
            if t.kind != kind or t.session_id != session_id:
                continue
            if token_id and t.id != token_id:
                continue
            if t.used is not None:
                continue
            if parse_iso(t.expires) <= now:
                continue
            t.used = to_iso(now)
            _save(tokens, now)
            return t
    raise TokenError(
        f"no valid '{kind}' approval for this session. The user must type /moni-pod:gpu-{kind} "
        f"(approvals expire after {TOKEN_TTL_SEC // 60} min and work once).")


def handle_user_prompt_expansion(event: dict, now: datetime | None = None) -> dict | None:
    """Hook body. Returns the hook JSON output, or None when the event is not ours."""
    if event.get("expansion_type") != "slash_command" or event.get("command_source") != "plugin":
        return None
    kind = COMMAND_KIND.get(event.get("command_name", ""))
    if not kind:
        return None
    tok = mint(kind, event.get("session_id", ""), event.get("command_args", ""), now)
    return {"hookSpecificOutput": {
        "hookEventName": "UserPromptExpansion",
        "additionalContext": (f"moni_pod: the user typed /moni-pod:gpu-{kind}; one-time approval token "
                              f"{tok.id} issued for this session, valid until {tok.expires}."),
    }}
