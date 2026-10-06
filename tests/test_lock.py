import json

import pytest

from moni_pod import guard, lock

SESSION = "sess-A"


def decision(out):
    return None if out is None else out["hookSpecificOutput"].get("permissionDecision")


def shell(cmd, tool="Bash", session=SESSION, explain=None):
    return lock.handle_pre_tool_use({"tool_name": tool, "tool_input": {"command": cmd}, "session_id": session},
                                    explain=explain)


def mcp(tool, **ti):
    return lock.handle_pre_tool_use({"tool_name": tool, "tool_input": ti, "session_id": SESSION})


# ---- RunPod MCP ----------------------------------------------------------------------

@pytest.mark.parametrize("prefix", ["mcp__plugin_runpod_runpod__", "mcp__runpod__", "mcp__my-runpod__"])
@pytest.mark.parametrize("tool", sorted(lock.MCP_DENY))
def test_mcp_mutations_denied(prefix, tool):
    out = mcp(prefix + tool, body={"name": "x"})
    assert decision(out) == "deny" and "moni_pod:" in out["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("action,expected", [("start", "deny"), ("restart", "deny"), ("terminate", "deny"),
                                             ("stop", None), ("STOP", None)])
def test_mcp_pod_action(action, expected):
    assert decision(mcp("mcp__plugin_runpod_runpod__pod-action", id="p", body={"action": action})) == expected


@pytest.mark.parametrize("tool", ["mcp__plugin_runpod_runpod__list-pods", "mcp__plugin_runpod_runpod__get-pod",
                                  "mcp__plugin_runpod_runpod__list-pod-billing", "mcp__other__create-pod"])
def test_mcp_reads_and_other_servers_pass(tool):
    assert mcp(tool) is None


# ---- runpodctl -----------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "runpodctl pod create --gpu-id 'NVIDIA GeForce RTX 4090' --image x",
    "runpodctl create pod --name x",
    "runpodctl create pods --podCount 2",
    "runpodctl pod start abc", "runpodctl start pod abc",
    "runpodctl pod delete abc", "runpodctl remove pod abc", "runpodctl pod rm abc",
    "runpodctl pod restart abc", "runpodctl pod reset abc", "runpodctl pod update abc --image y",
    "cd x && RUNPOD_API_KEY=k runpodctl pod create --image y",
    "C:/Users/u/.local/bin/runpodctl.exe pod create --image y",
])
def test_runpodctl_mutations_denied(cmd):
    assert decision(shell(cmd)) == "deny"
    assert decision(shell(cmd, tool="PowerShell")) == "deny"


@pytest.mark.parametrize("cmd", ["runpodctl pod list", "runpodctl pod get abc", "runpodctl pod stop abc",
                                 "runpodctl stop pod abc", "runpodctl gpu list", "runpodctl billing pods",
                                 "runpodctl user", "runpodctl pod create --help"])
def test_runpodctl_reads_and_stop_pass(cmd):
    expected = "deny" if "create" in cmd else None  # --help on create is still a create verb: denied (safe side)
    assert decision(shell(cmd)) == expected


# ---- raw REST / GraphQL ----------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "curl -X POST https://api.runpod.io/v2/pods -H 'Authorization: Bearer $K' -d '{\"name\":\"x\"}'",
    "curl -X DELETE https://rest.runpod.io/v1/pods/abc",
    "curl --request POST https://api.runpod.io/v2/pods/abc/action -d '{\"action\":\"start\"}'",
    "curl -X POST https://api.runpod.io/v2/pods/abc/action -d '{\"action\":\"terminate\"}'",
    "Invoke-RestMethod -Method Post -Uri https://api.runpod.io/v2/pods -Body $b",
    "python -c \"import requests; requests.post('https://api.runpod.io/v2/pods', json={})\"",
    "curl https://api.runpod.io/graphql -d '{\"query\":\"mutation { podFindAndDeployOnDemand(input:{}) { id } }\"}'",
    "curl https://api.runpod.io/graphql?api_key=$K --data '{\"query\":\"mutation { podResume(input:{podId:\\\"x\\\"}) {id}}\"}'",
])
def test_raw_api_mutations_denied(cmd):
    assert decision(shell(cmd)) == "deny"


@pytest.mark.parametrize("cmd", [
    "curl https://api.runpod.io/v2/pods -H 'Authorization: Bearer $K'",
    "curl -X POST https://api.runpod.io/v2/pods/abc/action -d '{\"action\":\"stop\"}'",
    "curl https://api.runpod.io/v2/billing/pods?lastN=1",
    "curl https://api.runpod.io/graphql -d '{\"query\":\"query { myself { clientBalance } }\"}'",
])
def test_raw_api_reads_and_stop_pass(cmd):
    assert shell(cmd) is None


# ---- ledger / approvals tamper ---------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "echo '{}' > ~/.moni_pod/tokens.json", "rm ~/.moni_pod/ledger.json",
    "Set-Content $HOME/.moni_pod/tokens.json '{}'", "sed -i s/used/x/ ~/.moni_pod/tokens.json",
])
def test_ledger_writes_denied(cmd):
    assert decision(shell(cmd)) == "deny"


def test_ledger_reads_pass_and_write_tool_denied():
    assert shell("cat ~/.moni_pod/ledger.json") is None
    for tool in ("Write", "Edit"):
        out = lock.handle_pre_tool_use({"tool_name": tool, "tool_input": {"file_path": r"C:\Users\u\.moni_pod\tokens.json"}})
        assert decision(out) == "deny"
    assert lock.handle_pre_tool_use({"tool_name": "Write", "tool_input": {"file_path": "/repo/src/x.py"}}) is None


# ---- our CLI: key 2 --------------------------------------------------------------------

MONI = 'uv run --quiet --project "/p/moni-pod" moni-pod'


def test_cli_reads_pass():
    for sub in ("status --session sess-A", "quote --session sess-A --cpu cpu3c", "reconcile",
                "start --session sess-A --cpu cpu3c --dry-run", "stop --session sess-A --pod p --action stop"):
        assert shell(f"{MONI} {sub}") is None


@pytest.mark.parametrize("sub,kind", [("start --session sess-A --cpu cpu3c --hours 1", "start"),
                                      ("start --session sess-A --resume p1", "start"),
                                      ("stop --session sess-A --pod p --action terminate --retrieved yes", "stop"),
                                      ("extend --session sess-A --pod p --hours 1", "extend")])
def test_cli_spend_without_token_denied_with_token_asks(sub, kind):
    out = shell(f"{MONI} {sub}")
    assert decision(out) == "deny" and f"/moni-pod:gpu-{kind}" in out["hookSpecificOutput"]["permissionDecisionReason"]
    guard.mint(kind, SESSION)
    seen = []
    out = shell(f"{MONI} {sub}", explain=lambda s, a, sid: seen.append((s, a, sid)) or "PRICE TEXT")
    assert decision(out) == "ask"
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == "moni_pod: PRICE TEXT"
    assert seen[0][2] == SESSION and "--session" in seen[0][1]
    assert guard.find_valid(kind, SESSION) is not None  # the hook never consumes; the CLI does


def test_cli_explain_failure_still_asks():
    guard.mint("start", SESSION)

    def boom(*a):
        raise SystemExit(2)
    out = shell(f"{MONI} start --session sess-A --cpu cpu3c", explain=boom)
    assert decision(out) == "ask" and "could not price" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_cli_other_session_or_double_spend_denied():
    guard.mint("start", SESSION)
    out = shell(f"{MONI} start --session sess-OTHER --cpu cpu3c")
    assert decision(out) == "deny" and "this Claude Code session" in out["hookSpecificOutput"]["permissionDecisionReason"]
    out = shell(f"{MONI} start --session sess-A --cpu cpu3c && {MONI} start --session sess-A --gpu x")
    assert decision(out) == "deny"


def test_token_from_other_session_not_enough():
    guard.mint("start", "sess-B")
    assert decision(shell(f"{MONI} start --session sess-A --cpu cpu3c")) == "deny"


def test_hook_cli_entry(monkeypatch, capsys):
    import io
    from moni_pod import cli
    ev = {"tool_name": "Bash", "tool_input": {"command": "runpodctl pod create --image x"}, "session_id": SESSION}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(ev)))
    assert cli.main(["hook", "pre-tool-use"]) == 0
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    ev["tool_input"]["command"] = "ls"
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(ev)))
    cli.main(["hook", "pre-tool-use"])
    assert capsys.readouterr().out == ""


def test_explain_texts_for_stop_extend_resume():
    from moni_pod import cli
    from moni_pod.ledger import EXITED, Ledger
    from tests.conftest import make_record
    Ledger().upsert(make_record(pod_id="p1", name="my-gpu", cost_per_hr=0.12, running_disk_per_hr=0.007,
                                runs=[{"start": "2026-10-06T10:00:00Z"}]))
    t = cli.explain_spend("stop", ["--session", SESSION, "--pod", "p1", "--action", "terminate", "--retrieved", "no"], SESSION)
    assert "DELETE pod my-gpu" in t and "NO - they will be lost" in t
    t = cli.explain_spend("extend", ["--session", SESSION, "--pod", "p1", "--hours", "2"], SESSION)
    assert "EXTEND my-gpu by 2 h" in t and "$0.25" in t
    Ledger().upsert(make_record(pod_id="p2", name="old", status=EXITED, ttl_sec=3600, cost_per_hr=0.5,
                                runs=[{"start": "2026-10-06T09:00:00Z", "stop": "2026-10-06T10:00:00Z"}]))
    t = cli.explain_spend("start", ["--session", SESSION, "--resume", "p2"], SESSION)
    assert t.startswith("RESUME old for up to 1 h") and "$0.50" in t


@pytest.mark.parametrize("cmd", ["runpodctl gpu list -o json", "runpodctl datacenter list",
                                 "curl -s https://api.runpod.io/v2/catalog/gpus?include=AVAILABILITY"])
def test_lookups_pass_with_a_hint(cmd):
    out = shell(cmd)
    assert decision(out) is None  # not blocked
    assert "moni-pod:gpu-list" in out["hookSpecificOutput"]["additionalContext"]


def test_session_start_always_points_to_gpu_list():
    from moni_pod import notify
    out = notify.session_start({"source": "startup"})
    assert "systemMessage" not in out  # nothing for the user to see when no pods
    assert "moni-pod:gpu-list" in out["hookSpecificOutput"]["additionalContext"]
