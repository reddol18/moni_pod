"""Minimal RunPod REST v2 client (https://api.runpod.io/v2, OpenAPI: /v2/openapi.json).

Stdlib only so hooks start fast. The transport is injectable for tests.
M1 exposes read-only calls; create/action calls arrive with M2/M3 behind the gate (ADR-0002).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

BASE_URL = "https://api.runpod.io/v2"
USER_AGENT = "moni-pod/0.1"

# transport(method, url, headers, body_bytes) -> (status, response_bytes)
Transport = Callable[[str, str, dict, bytes | None], tuple[int, bytes]]


class RunPodError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"RunPod API {status}: {message}")
        self.status = status


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class RunPodClient:
    def __init__(self, api_key: str, base_url: str = BASE_URL, transport: Transport | None = None):
        if not api_key:
            raise ValueError("RUNPOD_API_KEY is not set")
        self._key = api_key
        self.base_url = base_url.rstrip("/")
        self._transport = transport or urllib_transport

    def __repr__(self) -> str:  # never show the key
        return f"RunPodClient(base_url={self.base_url!r})"

    def _request(self, method: str, path: str, params: dict | None = None, body: Any = None) -> Any:
        url = self.base_url + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        status, raw = self._transport(method, url, headers, data)
        text = raw.decode("utf-8", errors="replace") if raw else ""
        if status >= 400:
            raise RunPodError(status, self._redact(text)[:500])
        return json.loads(text) if text else None

    def _redact(self, text: str) -> str:
        return text.replace(self._key, "***") if self._key else text

    # ---- read-only --------------------------------------------------------
    def list_pods(self) -> list[dict]:
        pods: list[dict] = []
        cursor = None
        while True:
            page = self._request("GET", "/pods", {"limit": 1000, "cursor": cursor})
            pods.extend(page.get("pods", []))
            pag = page.get("pagination") or {}
            if not pag.get("hasNextPage") or not pag.get("nextCursor"):
                return pods
            cursor = pag["nextCursor"]

    def get_pod(self, pod_id: str) -> dict:
        return self._request("GET", f"/pods/{urllib.parse.quote(pod_id)}")

    def list_gpu_types(self, cloud: str | None = None, count: int = 1) -> list[dict]:
        """GPU catalog with prices. With `cloud`, also stock for pods in that cloud: `availability` is
        NONE/LOW/MEDIUM/HIGH (REST v2 `include=AVAILABILITY&product=POD`, task 0002 / ADR-0006)."""
        params = None
        if cloud:
            params = {"include": "AVAILABILITY", "product": "POD", "cloud": cloud.upper(), "count": count}
        return self._request("GET", "/catalog/gpus", params).get("gpus", [])

    def list_cpu_types(self, vcpu: int | None = None) -> list[dict]:
        """CPU flavors with prices; with `vcpu`, also pod stock (`availability`)."""
        params = {"include": "AVAILABILITY", "product": "POD", "vcpuCount": vcpu} if vcpu else None
        return self._request("GET", "/catalog/cpus", params).get("cpus", [])

    def get_template(self, template_id: str) -> dict:
        """Account template first, then the public catalog (official templates live only there)."""
        try:
            return self._request("GET", f"/templates/{urllib.parse.quote(template_id)}")
        except RunPodError as e:
            if e.status not in (400, 404):
                raise
        for source in ("official", "verified"):
            try:
                found = self._request("GET", "/catalog/templates", {"source": source}).get("templates", [])
            except RunPodError:
                continue
            for t in found:
                if t.get("id") == template_id:
                    return t
        raise RunPodError(404, f"template {template_id!r} not found")

    def account(self) -> dict:
        """Live account numbers: clientBalance, currentSpendPerHr, spendLimit (USD/h cap set in RunPod).

        REST v2 has no balance endpoint, and v2 billing history posts hours late (M3: empty 30+ min after
        spend while the balance had already dropped). The balance and spend rate move in near real time,
        so they are the reference for /gpu-status and settlement. Source: GraphQL `myself` (what
        `runpodctl user` reads).
        """
        query = "query { myself { clientBalance currentSpendPerHr spendLimit } }"
        status, raw = self._transport(
            "POST", "https://api.runpod.io/graphql",
            {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
             "Accept": "application/json", "User-Agent": USER_AGENT},
            json.dumps({"query": query}).encode("utf-8"))
        text = raw.decode("utf-8", errors="replace") if raw else ""
        if status >= 400:
            raise RunPodError(status, self._redact(text)[:300])
        data = json.loads(text)
        if data.get("errors"):
            raise RunPodError(status, self._redact(json.dumps(data["errors"]))[:300])
        return (data.get("data") or {}).get("myself") or {}

    def pod_system_log(self, pod_id: str, tail: int = 300, read_seconds: float = 5.0) -> list[dict]:
        """System log lines (`start container …`, `stop container …`) from the SSE log stream.

        The stream stays open for live logs, so it is read for at most `read_seconds`.
        Container stdout is not kept after a pod stops; the system log is.
        """
        url = (f"{self.base_url}/pods/{urllib.parse.quote(pod_id)}/logs?"
               + urllib.parse.urlencode({"tail": tail, "source": "system"}))
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self._key}", "Accept": "text/event-stream", "User-Agent": USER_AGENT})
        out: list[dict] = []
        deadline = time.monotonic() + read_seconds
        try:
            with urllib.request.urlopen(req, timeout=read_seconds) as resp:
                while time.monotonic() < deadline:
                    raw = resp.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("data:"):
                        try:
                            out.append(json.loads(line[5:]))
                        except json.JSONDecodeError:
                            pass
        except (TimeoutError, OSError):
            pass  # end of the read window
        return out

    # ---- spending: only called from the gated CLI (ADR-0002) ----------------
    def create_pod(self, body: dict) -> dict:
        return self._request("POST", "/pods", body=body)

    def update_pod(self, pod_id: str, body: dict) -> dict:
        """PATCH. Changing env/image/args on a running pod resets its container."""
        return self._request("PATCH", f"/pods/{urllib.parse.quote(pod_id)}", body=body)

    def pod_action(self, pod_id: str, action: str) -> dict | None:
        if action not in ("start", "stop", "restart", "terminate"):
            raise ValueError(action)
        return self._request("POST", f"/pods/{urllib.parse.quote(pod_id)}/action", body={"action": action})

    def pod_billing(self, *, pod_id: str | None = None, bucket_size: str = "day",
                    last_n: int | None = None, start_time: str | None = None,
                    end_time: str | None = None) -> dict:
        if (start_time is None) != (end_time is None):
            raise ValueError("startTime and endTime must be given together (API returns 400 otherwise)")
        return self._request("GET", "/billing/pods", {
            "podId": pod_id, "bucketSize": bucket_size, "lastN": last_n,
            "startTime": start_time, "endTime": end_time,
        })
