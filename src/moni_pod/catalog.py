"""GPU/CPU offers: price + live pod stock per cloud, from the official REST v2 catalog (ADR-0006).

`availability` comes from `GET /v2/catalog/gpus?include=AVAILABILITY&product=POD&cloud=…&count=…`
(NONE / LOW / MEDIUM / HIGH). It is a stock *level*, not a reservation: task 0002's field run saw RTX 4090
COMMUNITY fail twice while reported LOW. So NONE refuses before creating, LOW warns, and a failed
COMMUNITY create is answered with a SECURE quote (start.py).
"""

from __future__ import annotations

from dataclasses import dataclass

from .runpod_api import RunPodClient, RunPodError

LEVELS = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}


@dataclass
class GpuOffer:
    id: str
    memory_gb: int
    price: dict  # {"SECURE": float|None, "COMMUNITY": float|None} per GPU per hour
    stock: dict  # {"SECURE": level|None, "COMMUNITY": level|None}; None = not checked / not offered

    def has_stock(self, cloud: str) -> bool:
        return bool(self.price.get(cloud)) and LEVELS.get(self.stock.get(cloud) or "", 0) > 0


def gpu_offers(client: RunPodClient, count: int = 1) -> tuple[list[GpuOffer], bool]:
    """(offers, stock_checked). Falls back to prices only if the availability query fails."""
    try:
        per_cloud = {c: client.list_gpu_types(cloud=c, count=count) for c in ("SECURE", "COMMUNITY")}
        checked = True
    except (RunPodError, OSError):
        per_cloud = {"": client.list_gpu_types()}  # prices only
        checked = False
    by_id: dict[str, GpuOffer] = {}
    for cloud, rows in per_cloud.items():
        for g in rows:
            gid = g.get("id")
            if not gid or gid == "unknown":
                continue
            p = g.get("price") or {}
            # A price is real only where the GPU is offered: the catalog lists placeholder prices (e.g. $0.50)
            # for clouds whose `secure`/`community` flag is false.
            sec = p.get("secure") if g.get("secure", True) else None
            com = p.get("community") if g.get("community", True) else None
            o = by_id.setdefault(gid, GpuOffer(gid, int(g.get("memory") or 0),
                                               {"SECURE": sec or None, "COMMUNITY": com or None},
                                               {"SECURE": None, "COMMUNITY": None}))
            if cloud:
                o.stock[cloud] = g.get("availability")
    return list(by_id.values()), checked


def cpu_stock(client: RunPodClient, cpu_id: str, vcpu: int) -> str | None:
    try:
        for c in client.list_cpu_types(vcpu=vcpu):
            if c.get("id") == cpu_id:
                return c.get("availability")
    except (RunPodError, OSError):
        return None
    return None


def alternatives(offers: list[GpuOffer], gpu_id: str, cloud: str, count: int = 1, limit: int = 3) -> list[str]:
    """Same GPU on the other cloud (if in stock), then the cheapest in-stock GPUs with at least its VRAM."""
    want = next((o for o in offers if o.id == gpu_id), None)
    out: list[str] = []
    other = "SECURE" if cloud == "COMMUNITY" else "COMMUNITY"
    if want and want.has_stock(other):
        out.append(f"{gpu_id} on {other}: ${want.price[other] * count:.2f}/h, stock {want.stock[other]}")
    min_mem = want.memory_gb if want else 0
    cands = []
    for o in offers:
        if o.id == gpu_id or o.memory_gb < min_mem:
            continue
        for c in ("SECURE", "COMMUNITY"):
            if o.has_stock(c):
                cands.append((o.price[c] * count, o, c))
    for price, o, c in sorted(cands, key=lambda x: x[0])[:limit]:
        out.append(f"{o.id} ({o.memory_gb} GB) on {c}: ${price:.2f}/h, stock {o.stock[c]}")
    return out


def render_list(offers: list[GpuOffer], checked: bool, *, count: int = 1, min_vram: int = 0,
                show_all: bool = False, balance: float | None = None) -> str:
    rows = [o for o in offers if o.memory_gb >= min_vram and (o.price["SECURE"] or o.price["COMMUNITY"])]
    if checked and not show_all:
        rows = [o for o in rows if o.has_stock("SECURE") or o.has_stock("COMMUNITY")]
    def best(o: GpuOffer) -> float:
        in_stock = [o.price[c] for c in ("SECURE", "COMMUNITY") if o.has_stock(c)]
        return min(in_stock or [p for p in o.price.values() if p])
    rows.sort(key=best)

    def cell(o: GpuOffer, cloud: str) -> str:
        p = o.price[cloud]
        if not p:
            return "-"
        stock = (o.stock[cloud] or "?") if checked else "?"
        hrs = f" {balance / (p * count):5.1f}h" if balance and (not checked or o.has_stock(cloud)) else ""
        return f"${p * count:5.2f} {stock:<6}{hrs}"

    head = f"GPUs for pods, x{count}" + ("" if checked else "  (STOCK NOT CHECKED - prices only)")
    if balance is not None:
        head += f"   balance ${balance:.2f} -> 'h' = hours the balance covers at that rate (compute only)"
    lines = [head, f"{'GPU':34s} {'VRAM':>5s}  {'SECURE $/h stock':<24s} {'COMMUNITY $/h stock':<24s}"]
    for o in rows:
        lines.append(f"{o.id[:34]:34s} {o.memory_gb:>4d}G  {cell(o, 'SECURE'):<24s} {cell(o, 'COMMUNITY'):<24s}")
    if not rows:
        lines.append("(no GPU with stock matches)" if checked else "(no GPU matches)")
    lines.append("Stock is a level (LOW/MEDIUM/HIGH), not a reservation: LOW can still fail at create. "
                 "Start one with /moni-pod:gpu-start.")
    return "\n".join(lines)
