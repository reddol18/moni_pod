import json

import pytest

from moni_pod import registry


@pytest.mark.parametrize("image,expected", [
    ("ubuntu:22.04", ("registry-1.docker.io", "library/ubuntu", "22.04")),
    ("ubuntu", ("registry-1.docker.io", "library/ubuntu", "latest")),
    ("runpod/pytorch:1.0.2-cu1281", ("registry-1.docker.io", "runpod/pytorch", "1.0.2-cu1281")),
    ("ghcr.io/org/img:v1", ("ghcr.io", "org/img", "v1")),
    ("localhost:5000/img", ("localhost:5000", "img", "latest")),
    ("img@sha256:abc", ("registry-1.docker.io", "library/img", "sha256:abc")),
])
def test_parse_image(image, expected):
    r = registry.parse_image(image)
    assert (r.registry, r.repository, r.reference) == expected


def fake_registry(entrypoint, cmd):
    calls = []

    def get(url, headers):
        calls.append((url, headers.get("Authorization")))
        if url.startswith("https://auth.example/token"):
            return 200, {}, json.dumps({"token": "T"}).encode()
        if "Authorization" not in headers:
            return 401, {"WWW-Authenticate": 'Bearer realm="https://auth.example/token",service="reg",scope="repository:runpod/pytorch:pull"'}, b""
        if url.endswith("/manifests/tag"):
            return 200, {}, json.dumps({"manifests": [
                {"digest": "sha256:arm", "mediaType": "m", "platform": {"os": "linux", "architecture": "arm64"}},
                {"digest": "sha256:amd", "mediaType": "m", "platform": {"os": "linux", "architecture": "amd64"}},
            ]}).encode()
        if url.endswith("/manifests/sha256:amd"):
            return 200, {}, json.dumps({"config": {"digest": "sha256:cfg"}}).encode()
        if url.endswith("/blobs/sha256:cfg"):
            return 200, {}, json.dumps({"config": {"Entrypoint": entrypoint, "Cmd": cmd}}).encode()
        return 404, {}, b""

    return get, calls


def test_fetch_image_command_with_token_and_index():
    get, calls = fake_registry(["/opt/nvidia/nvidia_entrypoint.sh"], ["/start.sh"])
    ic = registry.fetch_image_command("runpod/pytorch:tag", http_get=get)
    assert ic.entrypoint == ["/opt/nvidia/nvidia_entrypoint.sh"] and ic.cmd == ["/start.sh"]
    assert any(a == "Bearer T" for _, a in calls)


def test_fetch_image_command_nulls():
    get, _ = fake_registry(None, None)
    ic = registry.fetch_image_command("runpod/pytorch:tag", http_get=get)
    assert ic.argv == []


def test_fetch_image_missing_platform():
    get, _ = fake_registry([], [])
    with pytest.raises(registry.RegistryError):
        registry.fetch_image_command("runpod/pytorch:tag", platform="linux/s390x", http_get=get)
