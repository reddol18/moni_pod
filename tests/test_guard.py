from datetime import timedelta

import pytest

from moni_pod import guard
from moni_pod.ledger import parse_iso

NOW = parse_iso("2026-10-06T10:00:00Z")


def expansion(cmd="moni-pod:gpu-start", session="sess-A", source="plugin", etype="slash_command"):
    return {"session_id": session, "hook_event_name": "UserPromptExpansion", "expansion_type": etype,
            "command_name": cmd, "command_args": "4090 2h", "command_source": source,
            "prompt": f"/{cmd} 4090 2h"}


def test_user_typed_command_mints_token():
    out = guard.handle_user_prompt_expansion(expansion(), now=NOW)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    tok = guard.find_valid("start", "sess-A", now=NOW)
    assert tok and tok.id in ctx and tok.args == "4090 2h"
    assert tok.expires == "2026-10-06T10:15:00Z"


@pytest.mark.parametrize("event", [
    expansion(cmd="moni-pod:gpu-status"),
    expansion(cmd="other:gpu-start"),
    expansion(source="user"),
    expansion(etype="mcp_prompt"),
])
def test_other_expansions_mint_nothing(event):
    assert guard.handle_user_prompt_expansion(event, now=NOW) is None
    assert guard.find_valid("start", "sess-A", now=NOW) is None


def test_token_single_use():
    guard.mint("start", "sess-A", now=NOW)
    guard.consume("start", "sess-A", now=NOW)
    with pytest.raises(guard.TokenError, match="must type /moni-pod:gpu-start"):
        guard.consume("start", "sess-A", now=NOW)


def test_token_bound_to_session_and_kind():
    guard.mint("start", "sess-A", now=NOW)
    with pytest.raises(guard.TokenError):
        guard.consume("start", "sess-B", now=NOW)
    with pytest.raises(guard.TokenError):
        guard.consume("stop", "sess-A", now=NOW)
    assert guard.consume("start", "sess-A", now=NOW)


def test_token_expires():
    guard.mint("start", "sess-A", now=NOW)
    with pytest.raises(guard.TokenError):
        guard.consume("start", "sess-A", now=NOW + timedelta(minutes=16))


def test_consume_specific_id():
    a = guard.mint("start", "sess-A", now=NOW)
    b = guard.mint("start", "sess-A", now=NOW)
    assert guard.consume("start", "sess-A", token_id=a.id, now=NOW).id == a.id
    assert guard.consume("start", "sess-A", now=NOW).id == b.id


def test_mint_requires_session():
    with pytest.raises(ValueError):
        guard.mint("start", "", now=NOW)


def test_hook_cli_roundtrip(monkeypatch, capsys):
    import io, json
    from moni_pod import cli
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(expansion())))
    assert cli.main(["hook", "user-prompt-expansion"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptExpansion"
    assert guard.find_valid("start", "sess-A") is not None


@pytest.mark.parametrize("content", ["{}", "[]", "not json", '{"tokens": "x"}', '{"tokens": [1, 2]}'])
def test_tampered_token_file_fails_closed(isolated_home, content):
    isolated_home.mkdir(parents=True, exist_ok=True)
    (isolated_home / "tokens.json").write_text(content)
    assert guard.find_valid("start", "sess-A", now=NOW) is None
    with pytest.raises(guard.TokenError):
        guard.consume("start", "sess-A", now=NOW)
    guard.mint("start", "sess-A", now=NOW)  # minting repairs the file
    assert guard.find_valid("start", "sess-A", now=NOW) is not None
