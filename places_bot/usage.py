"""Best-effort local tracker for API call usage per calendar month.

This is a *local estimate* to help you avoid blowing past the free tier. The
authoritative number is always in the Google Cloud console; this file only
counts calls made through this tool on this machine.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path


class UsageTracker:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    @staticmethod
    def _month_key(when: date | None = None) -> str:
        return (when or date.today()).strftime("%Y-%m")

    def _load(self) -> dict[str, int]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
            return {str(k): int(v) for k, v in data.items()}
        except (ValueError, OSError):
            # Corrupt or unreadable file: start fresh rather than crash.
            return {}

    def current_month_count(self) -> int:
        return self._load().get(self._month_key(), 0)

    def add(self, n: int) -> int:
        """Add `n` calls to the current month and return the new monthly total."""
        data = self._load()
        key = self._month_key()
        data[key] = data.get(key, 0) + n
        try:
            self.path.write_text(json.dumps(data, indent=2, sort_keys=True))
        except OSError:
            pass  # Tracking is best-effort; never fail the run over it.
        return data[key]
