"""/gpu-start planning and execution.

`quote()` is read-only: resolves the spec, prices it, checks TTL cap and session budget.
`execute()` spends money: it requires a user-minted token (guard.consume), creates the pod with
the TTL wrapper, and writes the ledger. ADR-0002 / ADR-0004.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from . import catalog, cost, guard, registry, ttl
from .ledger import RUNNING, Ledger, PodRecord, Run, to_iso, utcnow
from .runpod_api import RunPodClient, RunPodError
from .settings import Settings

DEFAULT_GPU_DISK_GB = 20
DEFAULT_CPU_DISK_GB = 10
DEFAULT_GPU_VOLUME_GB = 20  # keeps /workspace across a TTL stop; ~$0.003/h running, $0.13/day stopped
DEFAULT_IMAGE = "runpod/base:1.0.2-ubuntu2404"  # official "Runpod Ubuntu 24.04" template image


class StartRefused(RuntimeError):
    """A rule refused the start; message is shown to the user as the reason."""


class CreateFailed(RuntimeError):
    """RunPod refused the create (e.g. no instances left). Nothing was created; the approval was used."""

    def __init__(self, error: Exception, quote: "Quote"):
        super().__init__(str(error))
        self.quote = quote


def secure_alternative(q: "Quote", *, client: RunPodClient, settings: Settings, session_id: str,
                       ledger: Ledger | None = None, now: datetime | None = None,
                       image_command=registry.fetch_image_command) -> "Quote | None":
    """Task 0002: after a COMMUNITY create fails, price the same spec on SECURE (read-only).
    Never switches by itself - SECURE can cost 2x; the user starts it again with /moni-pod:gpu-start."""
    if q.compute != "GPU" or q.cloud != "COMMUNITY":
        return None
    spec = Spec(**{**q.spec, "cloud": "SECURE"})
    try:
        return quote(spec, client=client, settings=settings, session_id=session_id, ledger=ledger, now=now,
                     image_command=image_command)
    except StartRefused:
        return None


@dataclass
class Spec:
    gpu: str | None = None  # GPU type id, e.g. "NVIDIA GeForce RTX 4090"
    gpu_count: int = 1
    cpu: str | None = None  # CPU flavor id, e.g. "cpu3c"
    vcpu: int = 2
    cloud: str = "SECURE"
    template: str | None = None
    image: str | None = None
    hours: float | None = None
    budget_usd: float | None = None
    disk_gb: int | None = None
    volume_gb: int | None = None
    name: str | None = None
    ttl_methods: list[str] | None = None  # restrict watchdog stop methods (live path tests)
    ssh_pubkey: str | None = None  # public key text to inject as PUBLIC_KEY (see find_local_pubkey)

    @property
    def compute(self) -> str:
        return "GPU" if self.gpu else "CPU"


@dataclass
class Quote:
    spec: dict
    compute: str
    hw_id: str
    hw_count: int
    cloud: str
    image: str
    template: str | None
    disk_gb: int
    volume_gb: int
    ttl_sec: int
    compute_per_hr: float
    running_disk_per_hr: float
    stopped_disk_per_hr: float
    max_cost_usd: float  # (compute + running disk) × TTL hours
    budget_usd: float
    session_committed_usd: float  # this session's pods: spent so far + remaining TTL at current rate
    argv: list[str]  # original command that will be wrapped
    body: dict = field(repr=False, default_factory=dict)
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ssh_key_source: str = ""
    stock: str | None = None  # NONE/LOW/MEDIUM/HIGH for this cloud; None = not checked
    alternatives: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("body")
        return d


def _validate(spec: Spec, settings: Settings) -> list[str]:
    problems = []
    if spec.cpu and spec.cloud != "SECURE":
        problems.append("CPU pods are SECURE cloud only")
    if spec.cloud not in ("SECURE", "COMMUNITY"):
        problems.append("cloud must be SECURE or COMMUNITY")
    if spec.gpu_count < 1:
        problems.append("gpu count must be >= 1")
    if spec.vcpu < 1 or spec.vcpu & (spec.vcpu - 1):
        problems.append("vcpu must be a power of two")
    if spec.hours is not None and spec.hours > settings.ttl_max_hours:
        problems.append(f"requested {spec.hours:g} h exceeds the TTL cap of {settings.ttl_max_hours:g} h "
                        f"(change ttl_max_hours in ~/.moni_pod/config.json)")
    return problems


def _gpu_offer(client: RunPodClient, gpu_id: str, cloud: str, count: int):
    offers, checked = catalog.gpu_offers(client, count)
    for o in offers:
        if o.id == gpu_id:
            if not o.price.get(cloud):
                raise StartRefused(f"{gpu_id} has no {cloud} price (not offered on that cloud)")
            return float(o.price[cloud]), (o.stock.get(cloud) if checked else None), offers
    raise StartRefused(f"unknown GPU type {gpu_id!r} (see /moni-pod:gpu-list)")


def _cpu_price(client: RunPodClient, cpu_id: str, vcpu: int) -> float:
    for c in client.list_cpu_types():
        if c.get("id") == cpu_id:
            lo, hi = (c.get("vcpu") or {}).get("min", 1), (c.get("vcpu") or {}).get("max", 1024)
            if not lo <= vcpu <= hi:
                raise StartRefused(f"{cpu_id} supports {lo}-{hi} vCPU, asked {vcpu}")
            return float((c.get("price") or {}).get("securePerVcpu") or 0) * vcpu
    raise StartRefused(f"unknown CPU flavor {cpu_id!r}")


def session_committed(ledger_pods: dict[str, PodRecord], session_id: str, now: datetime) -> float:
    """Spent so far by this session's pods + what running ones can still spend before their TTL."""
    total = 0.0
    for rec in ledger_pods.values():
        if rec.session_id != session_id:
            continue
        total += cost.estimate(rec, now).total_usd
        if rec.status == RUNNING and rec.deadline:
            left_h = max(0.0, (datetime.fromisoformat(rec.deadline.replace("Z", "+00:00")) - now).total_seconds() / 3600)
            total += left_h * (rec.cost_per_hr + rec.running_disk_per_hr)
    return total


def quote(spec: Spec, *, client: RunPodClient, settings: Settings, session_id: str,
          ledger: Ledger | None = None, now: datetime | None = None,
          image_command=registry.fetch_image_command) -> Quote:
    now = now or utcnow()
    ledger = ledger or Ledger()
    if bool(spec.gpu) == bool(spec.cpu):
        raise StartRefused("choose exactly one of --gpu or --cpu")
    if spec.hours is not None and spec.hours <= 0:
        raise StartRefused("hours must be positive")
    problems = _validate(spec, settings)
    hours = spec.hours if spec.hours is not None else settings.ttl_default_hours
    budget = spec.budget_usd if spec.budget_usd is not None else settings.session_budget_usd

    template = client.get_template(spec.template) if spec.template else None
    image = spec.image or (template or {}).get("image") or DEFAULT_IMAGE

    stock, alts = None, []
    if spec.gpu:
        unit, stock, offers = _gpu_offer(client, spec.gpu, spec.cloud, spec.gpu_count)
        compute_rate = unit * spec.gpu_count
        if stock is None or stock in ("NONE", "LOW"):
            alts = catalog.alternatives(offers, spec.gpu, spec.cloud, spec.gpu_count)
        hw_id, hw_count = spec.gpu, spec.gpu_count
        disk = spec.disk_gb or (template or {}).get("disk") or DEFAULT_GPU_DISK_GB
        tpl_vol = ((template or {}).get("mounts") or {}).get("persistent") or {}
        volume = spec.volume_gb if spec.volume_gb is not None else int(tpl_vol.get("size") or DEFAULT_GPU_VOLUME_GB)
    else:
        compute_rate = _cpu_price(client, spec.cpu, spec.vcpu)
        stock = catalog.cpu_stock(client, spec.cpu, spec.vcpu)
        hw_id, hw_count = spec.cpu, spec.vcpu
        disk = spec.disk_gb or (template or {}).get("disk") or DEFAULT_CPU_DISK_GB
        volume = 0  # persistent mounts are disallowed on CPU pods
        if spec.volume_gb:
            problems.append("CPU pods cannot have a persistent volume")

    # Original command: template override, else the image's own ENTRYPOINT/CMD from its registry.
    t_has_cmd = bool(template and (template.get("entrypoint") or template.get("cmd") or template.get("args")))
    img_cmd = None
    if not t_has_cmd or not (template or {}).get("entrypoint"):
        try:
            ic = image_command(image)
            img_cmd = (ic.entrypoint, ic.cmd)
        except Exception as e:  # registry unreachable / private image
            problems.append(f"cannot read the default command of image {image} ({e}); "
                            "TTL wrapper needs it - pass a template with an explicit start command")
    argv = ttl.original_argv(template, img_cmd)

    ttl_sec = int(round(hours * 3600))
    running_disk = cost.running_disk_rate(disk, volume)
    max_usd = cost.max_cost(compute_rate + running_disk, ttl_sec)
    committed = session_committed(ledger.load(), session_id, now)
    if committed + max_usd > budget + 1e-9:
        problems.append(f"estimated max ${max_usd:.2f} + already committed this session ${committed:.2f} "
                        f"exceeds the session budget ${budget:.2f}")

    warnings: list[str] = []
    what = f"{hw_id} x{hw_count} on {spec.cloud}" if spec.gpu else f"CPU {hw_id} {hw_count} vCPU"
    if stock == "NONE":
        problems.append(f"no stock for {what} right now (RunPod catalog says NONE)"
                        + ("; in stock instead: " + " | ".join(alts) if alts else ""))
    elif stock == "LOW":
        warnings.append(f"stock for {what} is LOW: creating can still fail with 'no instances available'.")
    elif stock is None:
        warnings.append("stock not checked (catalog availability unavailable) - price only.")
    if volume == 0:
        warnings.append("no persistent volume: when the TTL stops this pod, everything on it is wiped "
                        "(container disk is cleared on stop). Copy results off before the deadline"
                        + ("" if spec.cpu else f", or use --volume {DEFAULT_GPU_VOLUME_GB}") + ".")
    warnings.append("the TTL counts from container start; image download time before that may also be billed.")

    name = spec.name or f"moni-{(hw_id.split()[-1] if spec.gpu else hw_id).lower()}-{now:%m%d%H%M}"
    body = {
        "name": name,
        "cloud": spec.cloud,
        "image": image,
        "disk": int(disk),
        "ports": list((template or {}).get("ports") or ["22/tcp"]),
        "env": {**((template or {}).get("env") or {})},
        "startSsh": bool((template or {}).get("startSsh", True)),
    }
    if "22/tcp" not in body["ports"]:
        body["ports"].append("22/tcp")  # ssh.direct is needed for /gpu-extend
    ssh_src = ""
    if spec.ssh_pubkey and body["startSsh"] and "PUBLIC_KEY" not in body["env"]:
        body["env"]["PUBLIC_KEY"] = spec.ssh_pubkey.strip()
        ssh_src = "local public key"
    if (template or {}).get("startJupyter"):
        body["startJupyter"] = True
    if (template or {}).get("registry"):
        body["registry"] = template["registry"]
    if spec.gpu:
        body["gpu"] = {"id": spec.gpu, "count": spec.gpu_count}
        if volume:
            body["mounts"] = {"persistent": {"size": int(volume), "path": tpl_vol.get("path") or "/workspace"}}
    else:
        body["cpu"] = {"id": spec.cpu, "vcpuCount": spec.vcpu}
    wrapped = ttl.wrap(argv, ttl_sec, spec.ttl_methods)
    body["entrypoint"], body["cmd"] = wrapped["entrypoint"], wrapped["cmd"]
    body["env"].update(wrapped["env"])

    return Quote(
        spec=asdict(spec), compute=spec.compute, hw_id=hw_id, hw_count=hw_count, cloud=spec.cloud,
        image=image, template=spec.template, disk_gb=int(disk), volume_gb=int(volume), ttl_sec=ttl_sec,
        compute_per_hr=round(compute_rate, 6), running_disk_per_hr=round(running_disk, 6),
        stopped_disk_per_hr=round(cost.stopped_disk_rate(volume), 6), max_cost_usd=round(max_usd, 4),
        budget_usd=budget, session_committed_usd=round(committed, 4), argv=argv, body=body, problems=problems,
        warnings=warnings, ssh_key_source=ssh_src, stock=stock, alternatives=alts,
    )


def find_local_pubkey(home=None) -> tuple[str, str] | None:
    """(path, key text) of the user's default SSH public key, if any."""
    from pathlib import Path
    base = Path(home) if home else Path.home() / ".ssh"
    for name in ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub"):
        p = base / name
        if p.exists():
            text = p.read_text(encoding="utf-8").strip()
            if text.startswith(("ssh-", "ecdsa-")):
                return str(p), text
    return None


@dataclass
class StartResult:
    pod_id: str
    name: str
    status: str
    cost_per_hr: float
    deadline: str
    token_id: str
    quote: dict


def execute(q: Quote, *, client: RunPodClient, session_id: str, ledger: Ledger | None = None,
            now: datetime | None = None) -> StartResult:
    if not q.ok:
        raise StartRefused("; ".join(q.problems))
    tok = guard.consume("start", session_id, now=now)  # raises TokenError without the user's command
    ledger = ledger or Ledger()
    try:
        pod = client.create_pod(q.body)
    except RunPodError as e:
        raise CreateFailed(e, q) from e
    now = now or utcnow()
    rate = float(pod.get("cost") or 0.0) or q.compute_per_hr
    deadline = to_iso(now + timedelta(seconds=q.ttl_sec))
    ledger.upsert(PodRecord(
        pod_id=pod["id"], name=pod.get("name") or q.body["name"], session_id=session_id,
        created_at=to_iso(now), compute=q.compute, hw_id=q.hw_id, hw_count=q.hw_count,
        cost_per_hr=rate, running_disk_per_hr=q.running_disk_per_hr, container_disk_gb=q.disk_gb, volume_gb=q.volume_gb, ttl_sec=q.ttl_sec,
        deadline=deadline, budget_usd=q.budget_usd, status=RUNNING, runs=[Run(start=to_iso(now))],
    ))
    return StartResult(pod_id=pod["id"], name=pod.get("name") or q.body["name"],
                       status=pod.get("status", "?"), cost_per_hr=rate, deadline=deadline,
                       token_id=tok.id, quote=q.to_dict())


@dataclass
class ResumePlan:
    pod_id: str
    name: str
    hours: float
    rate_per_hr: float
    max_cost_usd: float
    budget_usd: float
    session_committed_usd: float
    problems: list[str]
    warnings: list[str]

    @property
    def ok(self) -> bool:
        return not self.problems


def plan_resume(pod_id: str, *, hours: float | None, session_id: str, settings: Settings,
                ledger: Ledger | None = None, now: datetime | None = None,
                budget_usd: float | None = None) -> ResumePlan:
    """Read-only: price restarting a stopped pod for one more TTL run."""
    ledger = ledger or Ledger()
    now = now or utcnow()
    rec = ledger.get(pod_id)
    if rec is None:
        raise StartRefused(f"{pod_id} is not in the moni_pod ledger")
    problems, warnings = [], []
    if rec.status != "EXITED":
        problems.append(f"pod is {rec.status}; only a stopped pod can be resumed")
    h = hours if hours is not None else rec.ttl_sec / 3600 or settings.ttl_default_hours
    if h <= 0:
        raise StartRefused("hours must be positive")
    if h > settings.ttl_max_hours:
        problems.append(f"requested {h:g} h exceeds the TTL cap of {settings.ttl_max_hours:g} h")
    rate = rec.cost_per_hr + rec.running_disk_per_hr
    max_usd = rate * h
    budget = budget_usd if budget_usd is not None else settings.session_budget_usd
    committed = session_committed(ledger.load(), session_id, now)
    if committed + max_usd > budget + 1e-9:
        problems.append(f"estimated max ${max_usd:.2f} + already committed ${committed:.2f} exceeds the session budget ${budget:.2f}")
    warnings.append("container disk was wiped when the pod stopped; only the volume (/workspace) remains.")
    if rec.compute == "GPU":
        warnings.append("a stopped pod resumes on the same host; if its GPU was taken meanwhile, RunPod may refuse "
                        "or start it without a GPU - check /moni-pod:gpu-status after resuming.")
    return ResumePlan(pod_id, rec.name, h, round(rate, 6), round(max_usd, 4), budget, round(committed, 4),
                      problems, warnings)


def execute_resume(plan: ResumePlan, *, client: RunPodClient, session_id: str,
                   ledger: Ledger | None = None, now: datetime | None = None) -> dict:
    if not plan.ok:
        raise StartRefused("; ".join(plan.problems))
    guard.consume("start", session_id, now=now)
    ledger = ledger or Ledger()
    ttl_sec = int(round(plan.hours * 3600))
    rec = ledger.get(plan.pod_id)
    if ttl_sec != rec.ttl_sec:
        # The watchdog reads MONI_POD_TTL_SEC at container start; set it while the pod is stopped.
        env = dict(client.get_pod(plan.pod_id).get("env") or {})
        env[ttl.TTL_ENV] = str(ttl_sec)
        client.update_pod(plan.pod_id, {"env": env})
    pod = client.pod_action(plan.pod_id, "start") or {}
    ledger.mark_started(plan.pod_id, ttl_sec, at=now or utcnow())
    return pod
