"""Entry point used by the plugin commands: `moni-pod <subcommand>`."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import config, guard, settings, status
from . import sync as sync_mod
from . import extend as extend_mod
from . import lock, notify
from . import start as start_mod
from . import stop as stop_mod
from .ledger import Ledger, utcnow
from .runpod_api import RunPodClient, RunPodError


def cmd_status(args: argparse.Namespace) -> int:
    now = utcnow()
    ledger_pods = Ledger().load()
    live = None
    errors: list[str] = []
    billed_today = None
    account = None

    if not args.offline:
        key = config.load_api_key()
        if not key:
            errors.append(config.key_not_found_message() + " - showing ledger only")
        else:
            client = RunPodClient(key)
            try:
                live = client.list_pods()
            except (RunPodError, OSError) as e:
                errors.append(f"could not list pods: {e} - showing ledger only")
            if live is not None and not args.no_sync:
                changes = sync_mod.plan(ledger_pods, live, now, client.pod_system_log)
                if changes:
                    sync_mod.apply(Ledger(), changes)
                    ledger_pods = Ledger().load()
                    errors += [f"ledger synced: {c.pod_id} {c.action} at {c.at} ({c.source})" for c in changes]
            if live is not None:
                try:
                    account = client.account()
                except (RunPodError, OSError, ValueError) as e:
                    errors.append(f"could not read account balance: {e}")
            if args.billing and live is not None:
                try:
                    from .cost import billed_total
                    billed_today = billed_total(client.pod_billing(bucket_size="day", last_n=1))
                except (RunPodError, OSError) as e:
                    errors.append(f"could not read billing: {e}")

    rep = status.build(now, ledger_pods, live, session_id=args.session,
                       stale_stopped_hours=settings.load().stale_stopped_hours)
    rep.errors = errors
    rep.billed_today_usd = billed_today
    rep.account = account
    if args.json:
        print(json.dumps(rep.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(status.render_text(rep))
    return 0


def _client() -> RunPodClient:
    key = config.load_api_key()
    if not key:
        raise SystemExit(config.key_not_found_message())
    return RunPodClient(key)


def _spec(args: argparse.Namespace) -> start_mod.Spec:
    return start_mod.Spec(
        gpu=args.gpu, gpu_count=args.count, cpu=args.cpu, vcpu=args.vcpu, cloud=args.cloud.upper(),
        template=args.template, image=args.image, hours=args.hours, budget_usd=args.budget,
        disk_gb=args.disk, volume_gb=args.volume, name=args.name,
        ttl_methods=args.ttl_methods.split(",") if args.ttl_methods else None,
        ssh_pubkey=None if args.no_local_ssh_key else (start_mod.find_local_pubkey() or (None, None))[1])


def _render_quote(q: start_mod.Quote) -> str:
    hw = f"{q.hw_id} x{q.hw_count}" if q.compute == "GPU" else f"CPU {q.hw_id} {q.hw_count} vCPU"
    lines = [
        f"Pod:        {hw}, {q.cloud} cloud",
        f"Image:      {q.image}" + (f"  (template {q.template})" if q.template else ""),
        f"Disk:       container {q.disk_gb} GB, volume {q.volume_gb} GB",
        f"Max time:   {q.ttl_sec / 3600:g} h, then the pod stops itself (TTL)",
        f"Rate:       ${q.compute_per_hr:.4f}/h compute + ${q.running_disk_per_hr:.4f}/h disk",
        f"Max cost:   ${q.max_cost_usd:.2f} for this run",
        f"Budget:     ${q.budget_usd:.2f} per session (already committed: ${q.session_committed_usd:.2f})",
        f"If stopped: disk keeps costing ${q.stopped_disk_per_hr * 24:.4f}/day until deleted",
        f"Command:    {' '.join(q.argv) or '(none - keep alive)'} (wrapped with the TTL watchdog)",
    ]
    if q.ssh_key_source:
        lines.append(f"SSH:        your {q.ssh_key_source} is put on the pod (needed for /moni-pod:gpu-extend)")
    for w in q.warnings:
        lines.append(f"Note:       {w}")
    if q.problems:
        lines.append("REFUSED:")
        lines += [f"  - {p}" for p in q.problems]
    elif q.stock is None:
        lines.append("OK (price only - stock not checked). Needs the user's confirmation.")
    else:
        lines.append(f"OK to start (stock {q.stock}). Needs the user's confirmation.")
        if q.alternatives:
            lines.append("If it fails:  " + " | ".join(q.alternatives))
    return "\n".join(lines)


def cmd_quote(args: argparse.Namespace) -> int:
    client = _client()
    try:
        q = start_mod.quote(_spec(args), client=client, settings=settings.load(), session_id=args.session)
    except (start_mod.StartRefused, RunPodError) as e:
        print(f"REFUSED: {e}")
        return 2
    if args.json:
        print(json.dumps(q.to_dict(), indent=2))
    else:
        print(_render_quote(q))
        try:
            a = client.account()
            print(f"Account:    balance ${a.get('clientBalance', 0):.2f} - with Auto-Pay off this balance is the most "
                  f"you can lose. RunPod account spend limit ${a.get('spendLimit')}/h (info only; users cannot lower it)")
        except (RunPodError, OSError, ValueError):
            pass
    return 0 if q.ok else 2


def _render_resume(p: start_mod.ResumePlan) -> str:
    lines = [f"Resume:     {p.name} for up to {p.hours:g} h, then it stops itself (TTL)",
             f"Rate:       ${p.rate_per_hr:.4f}/h   Max cost: ${p.max_cost_usd:.2f}",
             f"Budget:     ${p.budget_usd:.2f} per session (already committed: ${p.session_committed_usd:.2f})"]
    lines += [f"Note:       {w}" for w in p.warnings]
    lines += (["REFUSED:"] + [f"  - {x}" for x in p.problems]) if p.problems else ["OK to resume (needs the user's confirmation)."]
    return "\n".join(lines)


def cmd_start(args: argparse.Namespace) -> int:
    client = _client()
    if args.resume:
        try:
            plan = start_mod.plan_resume(args.resume, hours=args.hours, session_id=args.session,
                                         settings=settings.load(), budget_usd=args.budget)
            if args.dry_run:
                print(_render_resume(plan))
                return 0 if plan.ok else 2
            pod = start_mod.execute_resume(plan, client=client, session_id=args.session)
        except (start_mod.StartRefused, guard.TokenError, RunPodError) as e:
            print(f"NOT RESUMED: {e}")
            return 2
        print(f"RESUMED {plan.name} status={pod.get('status', '?')}; stops itself after {plan.hours:g} h.")
        return 0
    if args.dry_run:
        return cmd_quote(args)
    try:
        q = start_mod.quote(_spec(args), client=client, settings=settings.load(), session_id=args.session)
        res = start_mod.execute(q, client=client, session_id=args.session)
    except start_mod.CreateFailed as e:
        print(f"NOT STARTED: RunPod refused the create: {e}. Nothing was created or billed.")
        alt = start_mod.secure_alternative(e.quote, client=client, settings=settings.load(), session_id=args.session)
        if alt is not None:
            print("Same spec on SECURE cloud (not started - the user must choose it with /moni-pod:gpu-start, "
                  "it can cost much more):")
            print(_render_quote(alt))
        elif e.quote.alternatives:
            print("In stock instead: " + " | ".join(e.quote.alternatives))
        return 2
    except (start_mod.StartRefused, guard.TokenError, RunPodError) as e:
        print(f"NOT STARTED: {e}")
        return 2
    print(f"STARTED {res.name} ({res.pod_id}) status={res.status} ${res.cost_per_hr:.4f}/h compute; "
          f"auto-stop at {res.deadline} (UTC). Max cost ${q.max_cost_usd:.2f}.")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """Read-only catalog: so an agent never needs the API key to look up GPUs (task 0002)."""
    from . import catalog
    client = _client()
    offers, checked = catalog.gpu_offers(client, args.count)
    balance = None
    try:
        balance = client.account().get("clientBalance")
    except (RunPodError, OSError, ValueError):
        pass
    if args.json:
        print(json.dumps({"stock_checked": checked, "balance": balance, "gpus": [o.__dict__ for o in offers]}, indent=2))
        return 0
    print(catalog.render_list(offers, checked, count=args.count, min_vram=args.min_vram, show_all=args.all,
                              balance=balance))
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    retrieved = {"yes": True, "no": False, None: None}[args.retrieved]
    try:
        s = stop_mod.stop_pod(args.pod, action=args.action, session_id=args.session, client=_client(),
                              retrieved=retrieved, discard_unretrieved=args.discard_unretrieved)
    except (stop_mod.StopRefused, guard.TokenError, RunPodError) as e:
        print(f"NOT DONE: {e}")
        return 2
    print(("TERMINATED " if args.action == "terminate" else "STOPPED ") + stop_mod.render(s))
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    for s in stop_mod.reconcile(client=_client()):
        print(stop_mod.render(s))
    return 0


def cmd_extend(args: argparse.Namespace) -> int:
    try:
        r = extend_mod.extend(args.pod, args.hours, session_id=args.session, client=_client(), settings=settings.load())
    except (extend_mod.ExtendRefused, guard.TokenError, RunPodError) as e:
        print(f"NOT EXTENDED: {e}")
        return 2
    print(f"EXTENDED {r.pod_id}: auto-stop moved {r.old_deadline} -> {r.new_deadline} (UTC), "
          f"extra cost up to ${r.extra_max_usd:.2f}")
    return 0


def explain_spend(sub: str, argv: list[str], session_id: str) -> str:
    """Text for the PreToolUse `ask` prompt, computed by the hook from the command itself
    (not from anything Claude wrote)."""
    from .ledger import Ledger
    if sub == "start":
        a = build_parser().parse_args(["start", *argv])
        if a.resume:
            p = start_mod.plan_resume(a.resume, hours=a.hours, session_id=session_id, settings=settings.load(),
                                      budget_usd=a.budget)
            return (f"RESUME {p.name} for up to {p.hours:g} h (then it stops itself). "
                    f"Max cost ${p.max_cost_usd:.2f} at ${p.rate_per_hr:.3f}/h." + (" REFUSED: " + "; ".join(p.problems) if p.problems else ""))
        q = start_mod.quote(_spec(a), client=_client(), settings=settings.load(), session_id=session_id)
        hw = f"{q.hw_id} x{q.hw_count}" if q.compute == "GPU" else f"CPU {q.hw_id} {q.hw_count} vCPU"
        text = (f"START {hw} ({q.cloud}) for up to {q.ttl_sec / 3600:g} h, then it stops itself. "
                f"Max cost ${q.max_cost_usd:.2f} (${q.compute_per_hr + q.running_disk_per_hr:.3f}/h). "
                f"Session budget ${q.budget_usd:.2f}, already committed ${q.session_committed_usd:.2f}.")
        if q.volume_gb:
            text += f" Stopped, its {q.volume_gb} GB volume costs ${q.stopped_disk_per_hr * 24:.3f}/day until deleted."
        if q.problems:
            text += " REFUSED: " + "; ".join(q.problems)
        return text
    if sub == "stop":
        a = build_parser().parse_args(["stop", *argv])
        rec = Ledger().get(a.pod)
        name = rec.name if rec else a.pod
        kept = "results copied off: " + ({"yes": "yes", "no": "NO - they will be lost"}.get(a.retrieved, "not answered"))
        return f"DELETE pod {name} permanently ({kept}). Everything on it, including the volume, is gone."
    if sub == "extend":
        a = build_parser().parse_args(["extend", *argv])
        rec = Ledger().get(a.pod)
        rate = (rec.cost_per_hr + rec.running_disk_per_hr) if rec else 0.0
        return (f"EXTEND {rec.name if rec else a.pod} by {a.hours:g} h; extra cost up to ${a.hours * rate:.2f} "
                f"(${rate:.3f}/h).")
    return ""


def cmd_hook(args: argparse.Namespace) -> int:
    """Hook entry points. Read the event JSON on stdin, print the hook JSON output."""
    event = json.loads(sys.stdin.read() or "{}")
    trace = os.environ.get("MONI_POD_HOOK_TRACE")  # test aid: record which calls reached the hook
    if trace:
        with open(trace, "a", encoding="utf-8") as f:
            f.write(json.dumps({"event": args.event, "tool": event.get("tool_name"),
                                "input": str(event.get("tool_input"))[:200]}) + "\n")
    if args.event == "user-prompt-expansion":
        out = guard.handle_user_prompt_expansion(event)
    elif args.event == "session-start":
        out = notify.session_start(event)
    elif args.event == "session-end":
        notify.session_end(event)  # desktop notification + stderr; JSON would be discarded
        out = None
    else:
        out = lock.handle_pre_tool_use(event, explain=explain_spend)
    if out:
        print(json.dumps(out))
    return 0


def _add_spec_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--session", required=True, help="Claude Code session id")
    p.add_argument("--gpu", help="GPU type id, e.g. 'NVIDIA GeForce RTX 4090'")
    p.add_argument("--count", type=int, default=1, help="GPU count")
    p.add_argument("--cpu", help="CPU flavor id, e.g. cpu3c")
    p.add_argument("--vcpu", type=int, default=2)
    p.add_argument("--cloud", default="SECURE", help="SECURE or COMMUNITY")
    p.add_argument("--template", help="RunPod template id, e.g. runpod-torch-v280")
    p.add_argument("--image", help="docker image (overrides the template's)")
    p.add_argument("--hours", type=float, help="max run time (TTL); default from settings")
    p.add_argument("--budget", type=float, help="session budget USD; default from settings")
    p.add_argument("--disk", type=int, help="container disk GB")
    p.add_argument("--volume", type=int, help="persistent volume GB (GPU pods only)")
    p.add_argument("--name")
    p.add_argument("--ttl-methods", help=argparse.SUPPRESS)  # e.g. "curl": prove one stop path in tests
    p.add_argument("--no-local-ssh-key", action="store_true", help="do not put ~/.ssh/*.pub on the pod")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="moni-pod")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("status", help="show pods, elapsed time, TTL left and estimated cost (read-only)")
    s.add_argument("--session", help="Claude Code session id, to mark this session's pods")
    s.add_argument("--json", action="store_true")
    s.add_argument("--billing", action="store_true", help="also read today's RunPod pod billing")
    s.add_argument("--offline", action="store_true", help="ledger only; do not call RunPod")
    s.add_argument("--no-sync", action="store_true", help="do not record stops/starts seen on RunPod in the ledger")
    s.set_defaults(func=cmd_status)

    q = sub.add_parser("quote", help="price a pod and check TTL cap and budget (read-only)")
    _add_spec_args(q)
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_quote)

    st = sub.add_parser("start", help="create the pod (needs a token from the user's /moni-pod:gpu-start)")
    _add_spec_args(st)
    st.add_argument("--resume", metavar="POD_ID", help="restart a stopped moni_pod pod for one more TTL run")
    st.add_argument("--dry-run", action="store_true", help="price only (same as quote)")
    st.set_defaults(func=cmd_start)

    sp = sub.add_parser("stop", help="stop (no token) or terminate (token + retrieval answer) a pod, then settle")
    sp.add_argument("--session", required=True)
    sp.add_argument("--pod", required=True)
    sp.add_argument("--action", choices=["stop", "terminate"], required=True)
    sp.add_argument("--retrieved", choices=["yes", "no"], help="user's answer: were the results copied off the pod?")
    sp.add_argument("--discard-unretrieved", action="store_true", help="user explicitly chose to delete unretrieved results")
    sp.set_defaults(func=cmd_stop)

    ex = sub.add_parser("extend", help="push a running pod's auto-stop over SSH (needs /moni-pod:gpu-extend token)")
    ex.add_argument("--session", required=True)
    ex.add_argument("--pod", required=True)
    ex.add_argument("--hours", type=float, required=True)
    ex.set_defaults(func=cmd_extend)

    ls = sub.add_parser("list", help="GPUs: VRAM, SECURE/COMMUNITY price and pod stock, hours the balance covers (read-only)")
    ls.add_argument("--count", type=int, default=1, help="GPUs per pod")
    ls.add_argument("--min-vram", type=int, default=0, help="only GPUs with at least this many GB")
    ls.add_argument("--all", action="store_true", help="include GPUs with no stock")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_list)

    sl = sub.add_parser("statusline", help="one line for the Claude Code status line (ledger only)")
    sl.set_defaults(func=lambda a: (print(notify.statusline()), 0)[1])

    rc = sub.add_parser("reconcile", help="re-read RunPod billing for unsettled pods; close terminated ones")
    rc.set_defaults(func=cmd_reconcile)

    h = sub.add_parser("hook", help="Claude Code hook entry point (reads event JSON on stdin)")
    h.add_argument("event", choices=["user-prompt-expansion", "pre-tool-use", "session-start", "session-end"])
    h.set_defaults(func=cmd_hook)
    return p


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
