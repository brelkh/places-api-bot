"""Thin client for the Google Places API (New) Text Search endpoint."""

from __future__ import annotations

import time
from typing import Any

import requests

from . import config


class PlacesAPIError(RuntimeError):
    """Raised when the Places API returns an unrecoverable error."""


class PlacesClient:
    """Calls the Text Search endpoint with retry/backoff on transient errors."""

    def __init__(
        self,
        api_key: str,
        field_mask: str = config.FIELD_MASK,
        region_code: str = config.DEFAULT_REGION_CODE,
        language_code: str = config.DEFAULT_LANGUAGE_CODE,
        max_retries: int = 4,
        timeout: int = 30,
        session: requests.Session | None = None,
    ) -> None:
        self.api_key = api_key
        self.field_mask = field_mask
        self.region_code = region_code
        self.language_code = language_code
        self.max_retries = max_retries
        self.timeout = timeout
        self.session = session or requests.Session()

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": self.field_mask,
        }

    def search_text(self, text_query: str) -> list[dict[str, Any]]:
        """Run a Text Search and return the list of matched places (may be empty).

        Retries on 429 (rate limit) and 5xx responses with exponential backoff.
        Raises PlacesAPIError on 4xx (other than 429) — those won't fix themselves.
        """
        body = {
            "textQuery": text_query,
            "languageCode": self.language_code,
            "regionCode": self.region_code,
        }

        last_error: str | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self.session.post(
                    config.SEARCH_TEXT_URL,
                    headers=self._headers(),
                    json=body,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = f"network error: {exc}"
                self._sleep_backoff(attempt)
                continue

            if resp.status_code == 200:
                return resp.json().get("places", [])

            # Rate limited or transient server error -> back off and retry.
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
                self._sleep_backoff(attempt)
                continue

            # Other 4xx errors are not retryable (bad key, bad request, etc.).
            raise PlacesAPIError(f"HTTP {resp.status_code}: {resp.text[:500]}")

        raise PlacesAPIError(
            f"Giving up after {self.max_retries} attempts. Last error: {last_error}"
        )

    @staticmethod
    def _sleep_backoff(attempt: int) -> None:
        # 2s, 4s, 8s, 16s ...
        time.sleep(2 ** (attempt + 1))
