"""Small file helpers: a cross-platform lock file and atomic JSON writes."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

STALE_LOCK_SEC = 60


class LockTimeout(RuntimeError):
    pass


@contextmanager
def file_lock(lock_path: Path, timeout: float = 10.0) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            # A lock older than STALE_LOCK_SEC is from a crashed writer.
            try:
                if time.time() - lock_path.stat().st_mtime > STALE_LOCK_SEC:
                    lock_path.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise LockTimeout(str(lock_path))
            time.sleep(0.05)
    try:
        os.close(fd)
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def write_json_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))
