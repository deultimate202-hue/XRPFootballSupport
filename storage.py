"""
storage.py - Minimal atomic JSON persistence.

Used for two things that MUST survive a restart/redeploy:
  * the match-event deduplication ledger (so goals are never re-posted)
  * the support-ticket routing table (so admins can answer after a restart)

Mount STATE_DIR on a persistent volume in production (see README).
"""
from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
from typing import Any

log = logging.getLogger("storage")


class JsonStore:
    def __init__(self, path: str, defaults: dict[str, Any]):
        self.path = path
        self.data: dict[str, Any] = copy.deepcopy(defaults)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            for key in defaults:
                if key in loaded:
                    self.data[key] = loaded[key]
            log.info("Loaded state from %s", path)
        except FileNotFoundError:
            log.info("No state file at %s yet - starting fresh", path)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read %s (%s) - starting fresh", path, exc)

    def save(self) -> None:
        """Write atomically so a crash mid-write never corrupts the file."""
        try:
            directory = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.error("Failed to persist state to %s: %s", self.path, exc)
