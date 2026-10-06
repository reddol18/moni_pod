"""Paths and secrets. The API key is read from the environment or a .env file and never printed."""

from __future__ import annotations

import os
from pathlib import Path

KEY_NAME = "RUNPOD_API_KEY"


def home_dir() -> Path:
    """Directory holding the ledger. `MONI_POD_HOME` overrides (tests)."""
    override = os.environ.get("MONI_POD_HOME")
    return Path(override) if override else Path.home() / ".moni_pod"


def _read_dotenv(path: Path) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):]
        if line.startswith(KEY_NAME + "="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            return value or None
    return None


# src/moni_pod/config.py -> project root (the plugin directory when installed or run with --plugin-dir)
PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def dotenv_candidates() -> list[Path]:
    """Where a `.env` holding RUNPOD_API_KEY is looked for, in order. Independent of the caller's cwd
    (task 0002: run from another project folder, the cwd-only lookup missed moni_pod's own `.env`).
    The working-directory and Claude-project `.env` come last, so a different project's key never wins."""
    dirs: list[Path] = []
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        dirs.append(Path(plugin_root))
    dirs += [PACKAGE_ROOT, home_dir(), Path.cwd()]
    project = os.environ.get("CLAUDE_PROJECT_DIR")
    if project:
        dirs.append(Path(project))
    seen, out = set(), []
    for d in dirs:
        key = str(d.resolve()) if d.exists() else str(d)
        if key not in seen:
            seen.add(key)
            out.append(d / ".env")
    return out


def key_not_found_message() -> str:
    places = ", ".join(str(p) for p in dotenv_candidates())
    return (f"{KEY_NAME} not found. Set the environment variable, or put {KEY_NAME}=... in one of: {places} "
            f"(recommended for an installed plugin: {home_dir() / '.env'})")


def load_api_key() -> str | None:
    """Environment variable first, then `.env` in the plugin root, moni_pod's own folder, ~/.moni_pod,
    the working directory, the Claude project dir."""
    value = os.environ.get(KEY_NAME)
    if value:
        return value.strip()
    for path in dotenv_candidates():
        value = _read_dotenv(path)
        if value:
            return value
    return None
