"""Read an image's default ENTRYPOINT/CMD from its registry (anonymous pull; public images).

Needed because RunPod templates usually leave the start command empty, meaning "use the image's
own". To add the TTL watchdog we must wrap that command, so we have to know it (ADR-0004).
Supports Docker Hub and any registry speaking the OCI distribution API with bearer-token auth.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


class RegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImageRef:
    registry: str
    repository: str
    reference: str  # tag or digest


@dataclass(frozen=True)
class ImageCommand:
    entrypoint: list[str]
    cmd: list[str]

    @property
    def argv(self) -> list[str]:
        return list(self.entrypoint) + list(self.cmd)


def parse_image(image: str) -> ImageRef:
    name, ref = image, "latest"
    if "@" in name:
        name, ref = name.split("@", 1)
    elif re.search(r":[^/]+$", name):
        name, ref = name.rsplit(":", 1)
    first = name.split("/", 1)[0]
    if "/" in name and ("." in first or ":" in first or first == "localhost"):
        registry, repo = name.split("/", 1)
    else:
        registry, repo = "registry-1.docker.io", name
        if "/" not in repo:
            repo = "library/" + repo
    return ImageRef(registry, repo, ref)


def _get(url: str, headers: dict, timeout: float = 20) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _token(www_auth: str, http_get) -> str | None:
    m = dict(re.findall(r'(\w+)="([^"]*)"', www_auth))
    if "realm" not in m:
        return None
    q = {k: v for k, v in m.items() if k in ("service", "scope")}
    status, _, body = http_get(m["realm"] + "?" + urllib.parse.urlencode(q), {})
    if status != 200:
        return None
    data = json.loads(body)
    return data.get("token") or data.get("access_token")


def fetch_image_command(image: str, platform: str = "linux/amd64", http_get=_get) -> ImageCommand:
    ref = parse_image(image)
    base = f"https://{ref.registry}/v2/{ref.repository}"
    headers = {"Accept": ACCEPT}

    def get(url: str, accept: str | None = None) -> bytes:
        h = dict(headers)
        if accept:
            h["Accept"] = accept
        status, resp_headers, body = http_get(url, h)
        if status == 401 and "Authorization" not in headers:
            auth = {k.lower(): v for k, v in resp_headers.items()}.get("www-authenticate", "")
            tok = _token(auth, http_get)
            if tok:
                headers["Authorization"] = f"Bearer {tok}"
                h["Authorization"] = headers["Authorization"]
                status, _, body = http_get(url, h)
        if status != 200:
            raise RegistryError(f"{url} -> HTTP {status}")
        return body

    manifest = json.loads(get(f"{base}/manifests/{ref.reference}"))
    if "manifests" in manifest:  # index / manifest list → pick the platform's manifest
        os_, arch = platform.split("/")
        chosen = next((m for m in manifest["manifests"]
                       if (m.get("platform") or {}).get("os") == os_
                       and (m.get("platform") or {}).get("architecture") == arch), None)
        if not chosen:
            raise RegistryError(f"no {platform} image in {image}")
        manifest = json.loads(get(f"{base}/manifests/{chosen['digest']}", chosen.get("mediaType")))
    cfg_digest = manifest["config"]["digest"]
    cfg = json.loads(get(f"{base}/blobs/{cfg_digest}", "*/*"))
    c = cfg.get("config") or {}
    return ImageCommand(entrypoint=list(c.get("Entrypoint") or []), cmd=list(c.get("Cmd") or []))
