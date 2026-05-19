"""Session debug logger (NDJSON). Repo root: parent of backend/."""

import json
import time

from app.agent.runtime_paths import runtime_path

_LOG_FILE = runtime_path("debug-f944f5.log")


def dbg_log(location: str, message: str, *, hypothesis_id: str = "", data: dict | None = None) -> None:
    # #region agent log
    try:
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "sessionId": "f944f5",
                        "hypothesisId": hypothesis_id,
                        "location": location,
                        "message": message,
                        "data": data or {},
                        "timestamp": int(time.time() * 1000),
                    }
                )
                + "\n"
            )
    except Exception:
        pass
    # #endregion
