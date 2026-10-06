import os
import shutil
import subprocess
import time

import pytest

from moni_pod import ttl


def test_original_argv_image_default():
    # runpod/pytorch: ENTRYPOINT nvidia_entrypoint.sh, CMD /start.sh
    img = (["/opt/nvidia/nvidia_entrypoint.sh"], ["/start.sh"])
    assert ttl.original_argv({"args": "", "entrypoint": None, "cmd": None}, img) == \
        ["/opt/nvidia/nvidia_entrypoint.sh", "/start.sh"]
    assert ttl.original_argv(None, ([], ["/start.sh"])) == ["/start.sh"]


def test_original_argv_template_args_override_cmd_keep_entrypoint():
    img = (["/opt/nvidia/nvidia_entrypoint.sh"], ["/start.sh"])
    assert ttl.original_argv({"args": "bash -c 'sleep 5'"}, img) == \
        ["/opt/nvidia/nvidia_entrypoint.sh", "bash", "-c", "sleep 5"]


def test_original_argv_template_entrypoint_drops_image_cmd():
    img = (["/ep"], ["/start.sh"])
    assert ttl.original_argv({"entrypoint": ["/my"], "cmd": None}, img) == ["/my"]
    assert ttl.original_argv({"entrypoint": ["/my"], "cmd": ["x"]}, img) == ["/my", "x"]


def test_wrap_structure():
    w = ttl.wrap(["/start.sh"], 600)
    assert w["entrypoint"][:2] == ["/bin/sh", "-c"] and w["entrypoint"][3] == "moni-pod-ttl"
    assert w["cmd"] == ["/start.sh"]
    assert w["env"] == {"MONI_POD_TTL_SEC": "600"}
    assert "terminate" not in w["entrypoint"][2]  # stop only
    with pytest.raises(ValueError):
        ttl.wrap(["/start.sh"], 0)


@pytest.mark.parametrize("argv", [[], ["/bin/bash"], ["bash"]])
def test_bare_shell_becomes_keep_alive(argv):
    assert ttl.wrap(argv, 60)["cmd"] == []


GIT_SH = (r"C:\Program Files\Git\usr\bin\sh.exe", r"C:\Program Files\Git\bin\sh.exe")
SH = shutil.which("sh") or next((p for p in GIT_SH if os.path.exists(p)), None)


@pytest.mark.skipif(SH is None, reason="needs a POSIX sh")
def test_watchdog_runs_original_command_and_stops(tmp_path):
    """Run the real wrapper script with a fake runpodctl on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "stop.log"
    fake = bindir / "runpodctl"
    fake.write_text(f'#!/bin/sh\necho "$@" >> "{log.as_posix()}"\nexit 0\n', newline="\n")
    fake.chmod(0o755)
    out = tmp_path / "ran.txt"
    w = ttl.wrap(["sh", "-c", f'echo "orig:$0" > "{out.as_posix()}"; sleep 3', "x"], 1)
    env = dict(os.environ, MONI_POD_TTL_SEC="1", RUNPOD_POD_ID="pod-fake", RUNPOD_API_KEY="k",
               MONI_POD_DEADLINE_FILE=(tmp_path / "deadline").as_posix(),
               # sh's own dir too: on Windows, Git's usr/bin (sleep, sh) is not always on PATH
               PATH=os.pathsep.join([bindir.as_posix(), os.path.dirname(SH), os.environ["PATH"]]))
    proc = subprocess.run([SH, "-c", w["entrypoint"][2], w["entrypoint"][3], *w["cmd"]],
                          env=env, capture_output=True, text=True, timeout=30)
    deadline = time.time() + 10
    while time.time() < deadline and not log.exists():
        time.sleep(0.2)
    assert out.read_text().strip() == "orig:x"  # original command ran via exec "$@"
    assert log.read_text().strip() == "pod stop pod-fake"
    assert "TTL reached" in proc.stdout


def test_wrap_methods_env():
    assert ttl.wrap(["/start.sh"], 60, ["curl"])["env"]["MONI_POD_TTL_METHODS"] == "curl"
    assert "MONI_POD_TTL_METHODS" not in ttl.wrap(["/start.sh"], 60)["env"]
    with pytest.raises(ValueError):
        ttl.wrap(["/start.sh"], 60, ["rm"])


@pytest.mark.skipif(SH is None, reason="needs a POSIX sh")
def test_watchdog_methods_restrict_path(tmp_path):
    """With MONI_POD_TTL_METHODS=curl, runpodctl (present) must not be used; curl gets a User-Agent."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    for name in ("runpodctl", "curl"):
        f = bindir / name
        f.write_text(f'#!/bin/sh\necho "{name} $@" >> "{log.as_posix()}"\nexit 0\n', newline="\n")
        f.chmod(0o755)
    w = ttl.wrap([], 1, ["curl"])
    env = dict(os.environ, RUNPOD_POD_ID="pod-fake", RUNPOD_API_KEY="k", **w["env"],
               MONI_POD_DEADLINE_FILE=(tmp_path / "deadline").as_posix(),
               PATH=os.pathsep.join([bindir.as_posix(), os.path.dirname(SH), os.environ["PATH"]]))
    # Original command outlives the 1 s TTL but ends by itself: with no command the wrapper runs
    # `sleep infinity`, which on Windows survives killing sh and was left running after every test run.
    # Output goes to a file: a pipe stays open while any child holds it.
    out_file = tmp_path / "out.txt"
    with open(out_file, "w") as out:
        subprocess.run([SH, "-c", w["entrypoint"][2], w["entrypoint"][3], "sleep", "4"], env=env,
                       stdout=out, stderr=subprocess.STDOUT, timeout=30)
    deadline = time.time() + 10
    while time.time() < deadline and not log.exists():
        time.sleep(0.2)
    assert log.exists(), f"no stop call logged; sh={SH}\n{out_file.read_text()}"
    calls = log.read_text()
    assert calls.startswith("curl ") and "runpodctl" not in calls
    assert "-A moni-pod-ttl" in calls and "https://api.runpod.io/v2/pods/pod-fake/action" in calls


def test_extend_command_validation():
    assert "/tmp/moni_pod_deadline" in ttl.extend_command(600)
    with pytest.raises(ValueError):
        ttl.extend_command(0)


@pytest.mark.skipif(SH is None, reason="needs a POSIX sh")
def test_deadline_file_extension_delays_stop(tmp_path):
    """TTL 2 s, extended by 4 s via extend_command → stop no earlier than ~6 s after start."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    f = bindir / "runpodctl"
    f.write_text(f'#!/bin/sh\necho "$@" >> "{log.as_posix()}"\nexit 0\n', newline="\n")
    f.chmod(0o755)
    dl = (tmp_path / "deadline").as_posix()
    w = ttl.wrap([], 2)
    env = dict(os.environ, RUNPOD_POD_ID="pod-fake", RUNPOD_API_KEY="k", **w["env"], MONI_POD_DEADLINE_FILE=dl,
               PATH=os.pathsep.join([bindir.as_posix(), os.path.dirname(SH), os.environ["PATH"]]))
    t0 = time.time()
    # a finite original command, not `sleep infinity` (see test_watchdog_methods_restrict_path)
    proc = subprocess.Popen([SH, "-c", w["entrypoint"][2], w["entrypoint"][3], "sleep", "10"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    while not os.path.exists(tmp_path / "deadline"):
        time.sleep(0.05)
    before = int((tmp_path / "deadline").read_text())
    out = subprocess.run([SH, "-c", ttl.extend_command(4, dl)], env=env, capture_output=True, text=True)
    assert int(out.stdout.strip()) == before + 4
    while time.time() - t0 < 20 and not log.exists():
        time.sleep(0.2)
    elapsed = time.time() - t0
    proc.kill()
    assert log.exists() and elapsed >= 5.0, elapsed


@pytest.mark.skipif(SH is None, reason="needs a POSIX sh")
def test_extend_without_deadline_file_fails(tmp_path):
    out = subprocess.run([SH, "-c", ttl.extend_command(60, (tmp_path / "nope").as_posix())],
                         capture_output=True, text=True)
    assert out.returncode == 3
