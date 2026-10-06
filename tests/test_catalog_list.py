"""Task 0002: catalog stock, `moni-pod list`, COMMUNITY create failure → SECURE quote."""
import json

import pytest

from moni_pod import catalog, cli, guard, lock, start
from moni_pod.ledger import Ledger
from moni_pod.runpod_api import RunPodError
from tests.test_start import IMAGES, FakeClient, NOW, SETTINGS, q


def test_placeholder_prices_dropped_where_not_offered():
    class C(FakeClient):
        def list_gpu_types(self, cloud=None, count=1):
            return [{"id": "AMD Instinct MI350 OAM", "memory": 294, "secure": True, "community": False,
                     "price": {"secure": 5.49, "community": 0.5}, "availability": "LOW"}]
    offers, checked = catalog.gpu_offers(C())
    assert checked and offers[0].price == {"SECURE": 5.49, "COMMUNITY": None}


def test_gpu_offers_falls_back_to_prices_only():
    offers, checked = catalog.gpu_offers(FakeClient(stock_error=True))
    assert not checked and all(o.stock == {"SECURE": None, "COMMUNITY": None} for o in offers)


def test_render_list_filters_sorts_and_shows_balance_hours():
    c = FakeClient(stock={("NVIDIA RTX A2000", "SECURE"): "NONE", ("NVIDIA RTX A2000", "COMMUNITY"): "NONE",
                          ("NVIDIA H100 80GB HBM3", "SECURE"): "NONE"})
    offers, checked = catalog.gpu_offers(c)
    text = catalog.render_list(offers, checked, min_vram=16, balance=10.0)
    rows = [ln for ln in text.splitlines() if ln.startswith("NVIDIA")]
    assert [r.split()[1] for r in rows] == ["GeForce", "L4"]  # 4090 community $0.34 first, then L4 $0.44
    assert "29.4h" in rows[0]  # 10.0 / 0.34
    assert "A2000" not in text and "H100" not in text  # < 16 GB / no stock
    assert "H100" in catalog.render_list(offers, checked, show_all=True)


def test_render_list_unchecked_says_so():
    offers, checked = catalog.gpu_offers(FakeClient(stock_error=True))
    assert "STOCK NOT CHECKED" in catalog.render_list(offers, checked)


def test_cli_list(monkeypatch, capsys):
    class C(FakeClient):
        def __init__(self, key): super().__init__()
        def account(self): return {"clientBalance": 10.0}
    monkeypatch.setenv("RUNPOD_API_KEY", "fake")
    monkeypatch.setattr(cli, "RunPodClient", C)
    assert cli.main(["list", "--min-vram", "20"]) == 0
    out = capsys.readouterr().out
    assert "balance $10.00" in out and "NVIDIA L4" in out and "A2000" not in out
    assert cli.main(["list", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["stock_checked"] and data["balance"] == 10.0 and len(data["gpus"]) == 4


def test_list_needs_no_approval():
    assert lock.handle_pre_tool_use({"tool_name": "Bash", "session_id": "s",
                                     "tool_input": {"command": 'uv run --project "/p/moni-pod" moni-pod list --min-vram 24'}}) is None


def test_deny_messages_point_to_gpu_list():
    out = lock.handle_pre_tool_use({"tool_name": "Bash", "session_id": "s",
                                    "tool_input": {"command": "runpodctl pod create --image x"}})
    assert "/moni-pod:gpu-list" in out["hookSpecificOutput"]["permissionDecisionReason"]


NO_CAPACITY = RunPodError(500, "There are no longer any instances available with the requested specifications.")


def test_community_create_failure_offers_secure_quote_without_starting():
    c = FakeClient(create_error=NO_CAPACITY)
    qt = q(start.Spec(gpu="NVIDIA GeForce RTX 4090", cloud="COMMUNITY", hours=1), client=c)
    guard.mint("start", "sess-A", now=NOW)
    with pytest.raises(start.CreateFailed) as ei:
        start.execute(qt, client=c, session_id="sess-A", now=NOW)
    assert Ledger().load() == {} and c.created == []
    alt = start.secure_alternative(ei.value.quote, client=c, settings=SETTINGS, session_id="sess-A", now=NOW,
                                   image_command=lambda i: IMAGES[i])
    assert alt.cloud == "SECURE" and alt.compute_per_hr == pytest.approx(0.74)
    assert alt.max_cost_usd > qt.max_cost_usd  # SECURE is dearer: never switched automatically
    assert guard.find_valid("start", "sess-A", now=NOW) is None  # the approval was used; user re-types


def test_secure_alternative_only_for_community_gpu():
    qt = q(start.Spec(gpu="NVIDIA L4", cloud="SECURE", hours=1))
    assert start.secure_alternative(qt, client=FakeClient(), settings=SETTINGS, session_id="s", now=NOW) is None


def test_cli_start_prints_secure_quote_on_community_failure(monkeypatch, capsys):
    class C(FakeClient):
        def __init__(self, key): super().__init__(create_error=NO_CAPACITY)
        def get_template(self, tid): raise AssertionError
        def account(self): return {}
    monkeypatch.setenv("RUNPOD_API_KEY", "fake")
    monkeypatch.setattr(cli, "RunPodClient", C)
    real_quote = start.quote  # image_command is a bound default: inject the fake through kwargs

    def quote_with_images(spec, **kw):
        kw.setdefault("image_command", lambda i: IMAGES["runpod/base:1.0.2-ubuntu2404"])
        return real_quote(spec, **kw)
    monkeypatch.setattr(start, "quote", quote_with_images)
    guard.mint("start", "sess-A")
    rc = cli.main(["start", "--session", "sess-A", "--gpu", "NVIDIA GeForce RTX 4090", "--cloud", "COMMUNITY",
                   "--hours", "1", "--no-local-ssh-key"])
    out = capsys.readouterr().out
    assert rc == 2 and "NOT STARTED: RunPod refused the create" in out and "Nothing was created" in out
    assert "Same spec on SECURE cloud (not started" in out and "SECURE cloud" in out
