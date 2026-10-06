# ADR-0005 — In-place extend over SSH; resume as fallback

- Status: proposed (M3, 2026-10-06), implements HQ's conditional approval of ADR-0004 §6
- Verified live 2026-10-06 (M3 run, RTX A2000 community, `runpod-torch-v280`)

## Context

ADR-0004 replaced `/gpu-extend` with "TTL stop → resume". HQ: that breaks real use — with volume 0 the
container disk is wiped on stop, and a community GPU can be gone on resume. SSH exists anyway in real use.

## Decision

1. **Watchdog polls a deadline file** instead of one `sleep`: on container start it writes
   `now + MONI_POD_TTL_SEC` (epoch, pod clock) to `/tmp/moni_pod_deadline` and re-reads it every ≤ 20 s.
   Missing/corrupt file → the initial deadline (never "no deadline").
2. **`/moni-pod:gpu-extend`** (user-typed → `extend` token): checks run length ≤ TTL cap and extra cost within the
   session budget, then over `ssh.direct` runs a one-liner that adds N seconds to the file and prints the new
   epoch; the ledger deadline is set from that epoch (pod clock, no local skew). Token is consumed only after the
   file moved; refusals and SSH failures keep it.
3. **SSH access without touching the account**: the user's local public key (`~/.ssh/id_ed25519.pub`, else ecdsa/rsa)
   is injected per pod as `PUBLIC_KEY` (`--no-local-ssh-key` to skip). `22/tcp` is always exposed. Host keys go to
   `~/.moni_pod/known_hosts`, not `~/.ssh/known_hosts`.
4. **Windows**: use `C:\Windows\System32\OpenSSH\ssh.exe` (talks to the Windows ssh-agent service). Git Bash's `ssh`
   cannot reach that agent, so a passphrase key fails with `Permission denied (publickey)` — seen live. Override
   with `MONI_POD_SSH`.
5. **Fallback** (no `ssh.direct`, SSH fails): stop at TTL → `/moni-pod:gpu-start --resume` (gated, re-priced,
   new TTL via PATCH env while stopped). Warned: container disk wiped; community GPU may be gone.
6. **Quote changes**: GPU pods default to a 20 GB volume when the template has none (≈ $0.003/h running,
   $0.13/day stopped); volume 0 shows a "results are wiped at TTL stop" warning; every quote notes that image
   download before container start may be billed. `/gpu-status` warns 10 min before auto-stop.

## Live evidence (M3)

- Wrapped `nvidia_entrypoint.sh /start.sh` → `sshd` listening, SSH login OK with the injected key, `nvidia-smi`
  shows the GPU, PID 1 is our `sh -c` wrapper, deadline file present.
- Extend +5 min over SSH: deadline 02:47:22Z → 02:52:22Z (pod clock). Self-stop result: see M3 report.

## Consequences

- `/gpu-extend` needs an SSH key usable non-interactively (no passphrase, or loaded in ssh-agent).
- The deadline lives on the container disk: a container restart re-arms the full TTL (unchanged from ADR-0003).
