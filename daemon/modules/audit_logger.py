import json
import logging
import logging.handlers
import os
from datetime import datetime, timezone


class AuditLogger:
    """
    Structured JSON audit log, separate from the daemon's general
    application logger. Every event is logged with: timestamp, event_type,
    user_id, machine_id, action, result.

    Output: configurable path (default /var/log/labfiles/daemon.log).
    Rotation: daily, keep `rotation_days` days (default 30).
    """

    def __init__(self, log_path: str = "/var/log/labfiles/daemon.log", rotation_days: int = 30):
        self._logger = logging.getLogger("labfiles.audit")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False

        # Avoid adding duplicate handlers if instantiated more than once
        if not self._logger.handlers:
            log_dir = os.path.dirname(log_path)
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
            handler = logging.handlers.TimedRotatingFileHandler(
                log_path, when="midnight", backupCount=rotation_days, encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)

    def log(self, event_type: str, user_id: int | None, machine_id: str | None,
            action: str, result: str, **extra) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "user_id": user_id,
            "machine_id": machine_id,
            "action": action,
            "result": result,
        }
        record.update(extra)
        self._logger.info(json.dumps(record))
