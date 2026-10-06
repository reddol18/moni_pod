import pytest

from moni_pod import guard, start
from moni_pod.ledger import Ledger, parse_iso
from moni_pod.registry import ImageCommand
from moni_pod.settings import Settings
from tests.conftest import make_record

NOW = parse_iso("2026-10-06T10:00:00Z")
SETTINGS = Settings()  # 2 h default, 8 h cap, $2 budget

TEMPLATES = {
    "runpod-torch-v280": {"id": "runpod-torch-v280", "image": "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404",
                          "args": "", "entrypoint": None, "cmd": None, "ports": ["8888/http", "22/tcp"],
                          "disk": 30, "mounts": {"persistent": {"path": "/workspace", "size": 50}},
                          "startSsh": True, "env": {"A": "1"}},
    "runpod-ubuntu-2404": {"id": "runpod-ubuntu-2404", "image": "runpod/base:1.0.2-ubuntu2404", "args": "",
                           "ports": ["8888/http", "22/tcp"], "disk": 10, "mounts": {}, "startSsh": True},
}
IMAGES = {
    "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404": ImageCommand(["/opt/nvidia/nvidia_entrypoint.sh"], ["/start.sh"]),
    "runpod/base:1.0.2-ubuntu2404": ImageCommand([], ["/start.sh"]),
}


class FakeClient:
    """stock: {(gpu_id, cloud): level}; default HIGH. stock_error: availability query raises."""

    def __init__(self, stock=None, stock_error=False, cpu_stock="HIGH", create_error=None):
        self.created = []
        self.stock = stock or {}
        self.stock_error = stock_error
        self.cpu_stock = cpu_stock
        self.create_error = create_error

    def list_gpu_types(self, cloud=None, count=1):
        rows = [{"id": "NVIDIA RTX A2000", "memory": 6, "price": {"secure": 0.2, "community": 0.12}},
                {"id": "NVIDIA GeForce RTX 4090", "memory": 24, "price": {"secure": 0.74, "community": 0.34}},
                {"id": "NVIDIA L4", "memory": 24, "price": {"secure": 0.49, "community": 0.44}},
                {"id": "NVIDIA H100 80GB HBM3", "memory": 80, "price": {"secure": 3.49, "community": None}}]
        if cloud:
            if self.stock_error:
                from moni_pod.runpod_api import RunPodError
                raise RunPodError(500, "boom")
            for r in rows:
                r["availability"] = self.stock.get((r["id"], cloud), "HIGH")
        return rows

    def list_cpu_types(self, vcpu=None):
        row = {"id": "cpu3c", "vcpu": {"min": 2, "max": 32}, "price": {"securePerVcpu": 0.03}}
        if vcpu:
            row["availability"] = self.cpu_stock
        return [row]

    def get_template(self, tid):
        return TEMPLATES[tid]

    def create_pod(self, body):
        if self.create_error:
            raise self.create_error
        self.created.append(body)
        return {"id": "pod-new-1", "name": body["name"], "status": "PROVISIONING", "cost": 0.13}


def q(spec, client=None, session="sess-A", **kw):
    return start.quote(spec, client=client or FakeClient(), settings=SETTINGS, session_id=session,
                       now=NOW, image_command=lambda i: IMAGES[i], **kw)


def test_quote_gpu_template_hand_values():
    # A2000 community $0.12/h; disk 30 + volume 50 GB × $0.10/730 = $0.0109589/h; 2 h default
    qt = q(start.Spec(gpu="NVIDIA RTX A2000", cloud="COMMUNITY", template="runpod-torch-v280"))
    assert qt.ok
    assert qt.compute_per_hr == pytest.approx(0.12)
    assert qt.running_disk_per_hr == pytest.approx(0.010959, abs=1e-6)
    assert qt.max_cost_usd == pytest.approx(round((0.12 + 80 * 0.1 / 730) * 2, 4))
    assert qt.stopped_disk_per_hr == pytest.approx(50 * 0.2 / 730, abs=1e-6)
    assert qt.argv == ["/opt/nvidia/nvidia_entrypoint.sh", "/start.sh"]
    b = qt.body
    assert b["gpu"] == {"id": "NVIDIA RTX A2000", "count": 1} and "cpu" not in b
    assert b["mounts"] == {"persistent": {"size": 50, "path": "/workspace"}}
    assert b["cmd"] == ["/opt/nvidia/nvidia_entrypoint.sh", "/start.sh"]
    assert b["entrypoint"][:2] == ["/bin/sh", "-c"]
    assert b["env"] == {"A": "1", "MONI_POD_TTL_SEC": "7200"}
    assert "templateId" not in b  # template resolved client-side so the wrapper is explicit


def test_quote_cpu_pod():
    # cpu3c 2 vCPU × $0.03 = $0.06/h; 10 GB disk → $0.00137/h; 10 min
    qt = q(start.Spec(cpu="cpu3c", vcpu=2, template="runpod-ubuntu-2404", hours=10 / 60))
    assert qt.ok and qt.ttl_sec == 600
    assert qt.compute_per_hr == pytest.approx(0.06)
    assert qt.max_cost_usd == pytest.approx(round((0.06 + 10 * 0.1 / 730) / 6, 4))
    assert qt.body["cpu"] == {"id": "cpu3c", "vcpuCount": 2} and "mounts" not in qt.body
    assert qt.argv == ["/start.sh"]


def test_ttl_cap_refused():
    qt = q(start.Spec(cpu="cpu3c", hours=9))
    assert not qt.ok and any("TTL cap" in p for p in qt.problems)


def test_budget_refused():
    qt = q(start.Spec(gpu="NVIDIA GeForce RTX 4090", hours=3))  # 0.74×3 > 2
    assert any("exceeds the session budget" in p for p in qt.problems)


def test_budget_counts_session_commitments():
    led = Ledger()
    # running pod of this session: $1/h, started 9:30, deadline 11:00 → 0.5 spent + 1.0 left = 1.5 committed
    led.upsert(make_record(pod_id="p1", session_id="sess-A", cost_per_hr=1.0, volume_gb=0,
                           runs=[{"start": "2026-10-06T09:30:00Z"}], deadline="2026-10-06T11:00:00Z"))
    led.upsert(make_record(pod_id="p2", session_id="sess-B", cost_per_hr=5.0, volume_gb=0,
                           runs=[{"start": "2026-10-06T09:30:00Z"}], deadline="2026-10-06T11:00:00Z"))
    qt = q(start.Spec(cpu="cpu3c", hours=8))  # ≈ 0.49 + 1.5 = 1.99 ≤ 2 → ok
    assert qt.session_committed_usd == pytest.approx(1.5)
    assert qt.ok
    qt = q(start.Spec(gpu="NVIDIA RTX A2000", cloud="COMMUNITY", hours=8))  # 0.98 + 1.5 > 2
    assert not qt.ok


@pytest.mark.parametrize("spec", [start.Spec(), start.Spec(gpu="NVIDIA RTX A2000", cpu="cpu3c")])
def test_exactly_one_compute_refused(spec):
    with pytest.raises(start.StartRefused, match="exactly one"):
        q(spec)


@pytest.mark.parametrize("spec,msg", [
    (start.Spec(cpu="cpu3c", vcpu=3), "power of two"),
    (start.Spec(cpu="cpu3c", cloud="COMMUNITY"), "SECURE cloud only"),
])
def test_validation_problems(spec, msg):
    assert any(msg in p for p in q(spec).problems)


def test_nonpositive_hours_refused():
    with pytest.raises(start.StartRefused, match="positive"):
        q(start.Spec(cpu="cpu3c", hours=0))


def test_unknown_gpu_or_wrong_cloud_refused():
    with pytest.raises(start.StartRefused, match="unknown GPU"):
        q(start.Spec(gpu="NVIDIA H900"))
    with pytest.raises(start.StartRefused, match="no COMMUNITY price"):
        q(start.Spec(gpu="NVIDIA H100 80GB HBM3", cloud="COMMUNITY"))


def test_unreadable_image_is_a_problem():
    def boom(_):
        raise RuntimeError("private")
    qt = start.quote(start.Spec(cpu="cpu3c", image="private/img:1"), client=FakeClient(), settings=SETTINGS,
                     session_id="sess-A", now=NOW, image_command=boom)
    assert any("cannot read the default command" in p for p in qt.problems)


def test_execute_requires_user_token():
    client = FakeClient()
    qt = q(start.Spec(cpu="cpu3c", hours=0.5), client=client)
    with pytest.raises(guard.TokenError):
        start.execute(qt, client=client, session_id="sess-A", now=NOW)
    assert client.created == []  # nothing was created


def test_execute_creates_and_records():
    client = FakeClient()
    qt = q(start.Spec(cpu="cpu3c", hours=0.5), client=client)
    guard.mint("start", "sess-A", now=NOW)
    res = start.execute(qt, client=client, session_id="sess-A", now=NOW)
    assert res.pod_id == "pod-new-1" and res.deadline == "2026-10-06T10:30:00Z"
    assert client.created == [qt.body]
    rec = Ledger().get("pod-new-1")
    assert rec.session_id == "sess-A" and rec.cost_per_hr == 0.13 and rec.ttl_sec == 1800
    assert rec.running_disk_per_hr == qt.running_disk_per_hr and rec.budget_usd == 2.0
    assert rec.runs[0].start == "2026-10-06T10:00:00Z"
    with pytest.raises(guard.TokenError):  # token was single-use
        start.execute(qt, client=client, session_id="sess-A", now=NOW)


def test_execute_refuses_bad_quote_before_token():
    client = FakeClient()
    guard.mint("start", "sess-A", now=NOW)
    qt = q(start.Spec(cpu="cpu3c", hours=9), client=client)
    with pytest.raises(start.StartRefused):
        start.execute(qt, client=client, session_id="sess-A", now=NOW)
    assert guard.find_valid("start", "sess-A", now=NOW) is not None  # token not burned


def test_gpu_without_template_volume_gets_default_volume():
    qt = q(start.Spec(gpu="NVIDIA RTX A2000", cloud="COMMUNITY", image="runpod/base:1.0.2-ubuntu2404", hours=1))
    assert qt.volume_gb == start.DEFAULT_GPU_VOLUME_GB
    assert qt.body["mounts"]["persistent"]["size"] == 20
    assert not any("no persistent volume" in w for w in qt.warnings)


def test_volume_zero_warns_and_pull_note_always():
    qt = q(start.Spec(gpu="NVIDIA RTX A2000", cloud="COMMUNITY", template="runpod-torch-v280", volume_gb=0, hours=1))
    assert any("no persistent volume" in w and "--volume 20" in w for w in qt.warnings)
    assert any("image download" in w for w in qt.warnings)
    cpu = q(start.Spec(cpu="cpu3c", hours=1))
    assert any("no persistent volume" in w and "--volume" not in w for w in cpu.warnings)


def test_local_ssh_key_injected_and_22_port_added():
    qt = q(start.Spec(cpu="cpu3c", image="runpod/base:1.0.2-ubuntu2404", hours=1, ssh_pubkey="ssh-ed25519 AAAA test\n"))
    assert qt.body["env"]["PUBLIC_KEY"] == "ssh-ed25519 AAAA test"
    assert "22/tcp" in qt.body["ports"] and qt.ssh_key_source == "local public key"
    assert "PUBLIC_KEY" not in q(start.Spec(cpu="cpu3c", hours=1)).body["env"]


def test_find_local_pubkey(tmp_path):
    assert start.find_local_pubkey(tmp_path) is None
    (tmp_path / "id_rsa.pub").write_text("ssh-rsa AAAB x\n")
    (tmp_path / "id_ed25519.pub").write_text("ssh-ed25519 AAAC y\n")
    path, text = start.find_local_pubkey(tmp_path)
    assert path.endswith("id_ed25519.pub") and text == "ssh-ed25519 AAAC y"



# ---- stock (task 0002) ------------------------------------------------------------------

def test_stock_none_refuses_and_suggests_alternatives():
    c = FakeClient(stock={("NVIDIA GeForce RTX 4090", "COMMUNITY"): "NONE", ("NVIDIA L4", "COMMUNITY"): "NONE",
                          ("NVIDIA L4", "SECURE"): "LOW", ("NVIDIA H100 80GB HBM3", "SECURE"): "NONE"})
    qt = q(start.Spec(gpu="NVIDIA GeForce RTX 4090", cloud="COMMUNITY", hours=1), client=c)
    assert not qt.ok and qt.stock == "NONE"
    msg = qt.problems[0]
    assert msg.startswith("no stock for NVIDIA GeForce RTX 4090 x1 on COMMUNITY")
    assert "NVIDIA GeForce RTX 4090 on SECURE: $0.74/h, stock HIGH" in msg  # same GPU, other cloud first
    assert "NVIDIA L4 (24 GB) on SECURE: $0.49/h, stock LOW" in msg  # >= 24 GB, in stock, cheapest
    assert "A2000" not in msg  # 6 GB < 24 GB
    assert "H100" not in msg  # no stock anywhere


def test_stock_low_warns_but_allows():
    c = FakeClient(stock={("NVIDIA GeForce RTX 4090", "COMMUNITY"): "LOW"})
    qt = q(start.Spec(gpu="NVIDIA GeForce RTX 4090", cloud="COMMUNITY", hours=1), client=c)
    assert qt.ok and qt.stock == "LOW"
    assert any("stock for NVIDIA GeForce RTX 4090 x1 on COMMUNITY is LOW" in w for w in qt.warnings)
    assert qt.alternatives and qt.alternatives[0].startswith("NVIDIA GeForce RTX 4090 on SECURE")


def test_stock_unchecked_is_price_only():
    qt = q(start.Spec(gpu="NVIDIA L4", cloud="SECURE", hours=1), client=FakeClient(stock_error=True))
    assert qt.ok and qt.stock is None
    assert any("stock not checked" in w for w in qt.warnings)


def test_stock_count_is_passed():
    seen = []

    class C(FakeClient):
        def list_gpu_types(self, cloud=None, count=1):
            seen.append((cloud, count))
            return super().list_gpu_types(cloud, count)
    q(start.Spec(gpu="NVIDIA L4", gpu_count=2, cloud="SECURE", hours=1), client=C())
    assert ("SECURE", 2) in seen and ("COMMUNITY", 2) in seen


def test_cpu_stock_none_refuses():
    qt = q(start.Spec(cpu="cpu3c", hours=1), client=FakeClient(cpu_stock="NONE"))
    assert not qt.ok and "no stock for CPU cpu3c 2 vCPU" in qt.problems[0]
