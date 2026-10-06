import json

import pytest

from moni_pod.runpod_api import RunPodClient, RunPodError

FAKE_KEY = "fake-key-for-tests"


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        status, payload = self.responses.pop(0)
        return status, (json.dumps(payload) if not isinstance(payload, str) else payload).encode()


def test_auth_header_and_base_url():
    t = FakeTransport([(200, {"gpus": [{"id": "g", "price": {"secure": 0.4}}]})])
    c = RunPodClient(FAKE_KEY, transport=t)
    assert c.list_gpu_types()[0]["id"] == "g"
    method, url, headers, body = t.calls[0]
    assert method == "GET" and url == "https://api.runpod.io/v2/catalog/gpus"
    assert headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert body is None


def test_list_pods_follows_pagination():
    t = FakeTransport([
        (200, {"pods": [{"id": "p1"}], "pagination": {"hasNextPage": True, "nextCursor": "c2"}}),
        (200, {"pods": [{"id": "p2"}], "pagination": {"hasNextPage": False, "nextCursor": None}}),
    ])
    pods = RunPodClient(FAKE_KEY, transport=t).list_pods()
    assert [p["id"] for p in pods] == ["p1", "p2"]
    assert "cursor=c2" in t.calls[1][1]
    assert "cursor" not in t.calls[0][1]  # None params are dropped


def test_billing_params():
    t = FakeTransport([(200, {"records": [], "metadata": {}})])
    RunPodClient(FAKE_KEY, transport=t).pod_billing(pod_id="p1", bucket_size="hour", last_n=3)
    url = t.calls[0][1]
    assert url.startswith("https://api.runpod.io/v2/billing/pods?")
    for part in ("podId=p1", "bucketSize=hour", "lastN=3"):
        assert part in url
    assert "startTime" not in url


def test_error_is_raised_and_key_redacted():
    t = FakeTransport([(401, f"invalid token {FAKE_KEY}")])
    with pytest.raises(RunPodError) as ei:
        RunPodClient(FAKE_KEY, transport=t).get_pod("p1")
    assert ei.value.status == 401
    assert FAKE_KEY not in str(ei.value)


def test_repr_hides_key_and_empty_key_rejected():
    assert FAKE_KEY not in repr(RunPodClient(FAKE_KEY, transport=FakeTransport([])))
    with pytest.raises(ValueError):
        RunPodClient("")


def test_account_graphql():
    t = FakeTransport([(200, {"data": {"myself": {"clientBalance": 10.5, "currentSpendPerHr": 0.127, "spendLimit": 80}}})])
    a = RunPodClient(FAKE_KEY, transport=t).account()
    assert a == {"clientBalance": 10.5, "currentSpendPerHr": 0.127, "spendLimit": 80}
    method, url, headers, body = t.calls[0]
    assert method == "POST" and url == "https://api.runpod.io/graphql"
    assert b"clientBalance" in body and headers["User-Agent"].startswith("moni-pod")


def test_account_graphql_errors_redacted():
    t = FakeTransport([(200, {"errors": [{"message": f"bad key {FAKE_KEY}"}]})])
    with pytest.raises(RunPodError) as ei:
        RunPodClient(FAKE_KEY, transport=t).account()
    assert FAKE_KEY not in str(ei.value)


def test_billing_requires_both_bounds():
    with pytest.raises(ValueError):
        RunPodClient(FAKE_KEY, transport=FakeTransport([])).pod_billing(start_time="2026-10-06T00:00:00Z")
