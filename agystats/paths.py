"""Path resolution helpers for agystats (Antigravity CLI & Desktop)."""

from __future__ import annotations

import os
from pathlib import Path


def _app_data_from_executable_data_dir(raw_dir: str | None) -> Path | None:
    if not raw_dir or not raw_dir.strip():
        return None
    p = Path(raw_dir.strip()).expanduser()
    for candidate in (p, *p.parents):
        if candidate.name == "sidecar_data" and candidate.parent != candidate:
            return candidate.parent
    return None


def get_app_data_dir() -> Path:
    """Resolve the Antigravity app data directory, preferring Antigravity CLI."""
    for env_key in (
        "AGYSTATS_APP_DATA_DIR",
        "AGY_METRICS_APP_DATA_DIR",
        "ANTIGRAVITY_CLI_APP_DATA_DIR",
        "ANTIGRAVITY_APP_DATA_DIR",
    ):
        explicit = os.environ.get(env_key)
        if explicit:
            return Path(explicit).expanduser()

    exec_app_data = _app_data_from_executable_data_dir(
        os.environ.get("ANTIGRAVITY_EXECUTABLE_DATA_DIR")
    )
    if exec_app_data is not None:
        return exec_app_data

    gemini_root = Path.home() / ".gemini"
    cli_dir = gemini_root / "antigravity-cli"
    desktop_dir = gemini_root / "antigravity"
    if cli_dir.is_dir():
        return cli_dir
    if desktop_dir.is_dir() and (desktop_dir / "brain").is_dir():
        return desktop_dir
    if gemini_root.is_dir():
        try:
            for cand in sorted(gemini_root.iterdir(), key=lambda c: c.name):
                if cand.name != "config" and cand.is_dir() and (cand / "brain").is_dir():
                    return cand
        except OSError:
            pass
    if desktop_dir.is_dir():
        return desktop_dir
    return cli_dir


def get_brain_dir(app_data_dir: Path | None = None) -> Path:
    """Resolve the brain directory containing conversation transcript logs."""
    if app_data_dir is None:
        for env_key in ("AGYSTATS_BRAIN_DIR", "AGY_METRICS_BRAIN_DIR"):
            explicit = os.environ.get(env_key)
            if explicit:
                return Path(explicit).expanduser()
    base = Path(app_data_dir).expanduser() if app_data_dir is not None else get_app_data_dir()
    return base / "brain"


def get_projects_dir(app_data_dir: Path | None = None) -> Path:
    """Resolve the Antigravity projects metadata directory."""
    for env_key in ("AGYSTATS_PROJECTS_DIR", "AGY_METRICS_PROJECTS_DIR"):
        explicit = os.environ.get(env_key)
        if explicit:
            return Path(explicit).expanduser()
    base = Path(app_data_dir).expanduser() if app_data_dir is not None else get_app_data_dir()
    cand = base.parent / "config" / "projects"
    if cand.is_dir():
        return cand
    if app_data_dir is None:
        return Path.home() / ".gemini" / "config" / "projects"
    return cand


def get_state_dir(app_data_dir: Path | None = None) -> Path:
    """Resolve the persistent state directory for rollup SQLite cache and pricing overrides."""
    if app_data_dir is None:
        for env_key in ("AGYSTATS_STATE_DIR", "AGY_METRICS_STATE_DIR", "PLUGIN_DATA"):
            explicit = os.environ.get(env_key)
            if explicit:
                return Path(explicit).expanduser()

    app_data = Path(app_data_dir).expanduser() if app_data_dir is not None else get_app_data_dir()
    candidates = (
        app_data / "agystats",
        app_data / "plugin_data" / "agystats",
        app_data / "plugin_data" / "agy-metrics",
        app_data / "agy_metrics",
    )
    for cand in candidates:
        if cand.is_dir():
            return cand
    return candidates[0]


def get_cli_conversations_dir(brain_dir: Path) -> Path:
    """Return the Antigravity CLI conversations SQLite directory beside brain/."""
    return brain_dir.parent / "conversations"


def get_cli_summaries_db_path(brain_dir: Path) -> Path:
    """Return the Antigravity CLI conversation_summaries.db path beside brain/."""
    return brain_dir.parent / "conversation_summaries.db"
