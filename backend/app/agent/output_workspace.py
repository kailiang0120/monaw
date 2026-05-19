from __future__ import annotations

from pathlib import Path

from app.agent.runtime_paths import MONAW_HOME_DIR


def default_output_root() -> Path:
    return MONAW_HOME_DIR / "browser"


def default_downloads_dir() -> Path:
    return default_output_root() / "downloads"


def default_screenshots_dir() -> Path:
    return default_output_root() / "screenshots"


def configured_screenshots_dir() -> Path:
    configured = ""
    try:
        from app.agent.settings_store import load_agent_settings
        from app.config import settings

        configured = load_agent_settings(settings).browser.screenshots_dir
    except Exception:
        configured = ""

    path = Path(configured).expanduser() if configured else default_screenshots_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path
