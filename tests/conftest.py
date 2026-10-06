import pytest

from moni_pod.ledger import PodRecord, Run


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Never touch the real ~/.moni_pod or a real API key in tests."""
    monkeypatch.setenv("MONI_POD_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    # The real moni_pod/.env (with the real key) sits at PACKAGE_ROOT: point it at an empty dir in tests.
    from moni_pod import config
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    monkeypatch.setattr(config, "PACKAGE_ROOT", pkg)
    # Never show real desktop notifications from tests (they did pop up on the user's screen before this guard).
    from moni_pod import notify
    monkeypatch.setattr(notify, "desktop_notify", lambda title, body, **kw: True)
    return tmp_path / "home"


def make_record(**kw) -> PodRecord:
    base = dict(
        pod_id="pod-test-1", name="t1", session_id="sess-A", created_at="2026-10-06T10:00:00Z",
        compute="GPU", hw_id="NVIDIA GeForce RTX 4090", hw_count=1, cost_per_hr=0.44,
        volume_gb=20, ttl_sec=7200,
    )
    base.update(kw)
    if "runs" in base:
        base["runs"] = [r if isinstance(r, Run) else Run(**r) for r in base["runs"]]
    return PodRecord(**base)
