"""Thin client for the Google Places API (New) — Text Search and Place Details."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import requests

from . import config


class PlacesAPIError(RuntimeError):
    """Raised when the Places API returns an unrecoverable error.

    `reason` is a coarse classification used to give users an actionable
    message: "quota", "auth", "invalid_request", "network", or "unknown".
    """

    def __init__(
        self, message: str, *, reason: str = "unknown", http_status: int | None = None
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.http_status = http_status


def classify_error(status_code: int | None, body: str) -> str:
    """Map an HTTP status + Google error body to a coarse reason."""
    status_text = ""
    try:
        status_text = (json.loads(body).get("error") or {}).get("status", "")
    except (ValueError, AttributeError):
        pass

    if status_code == 429 or status_text == "RESOURCE_EXHAUSTED":
        return "quota"
    if status_code in (401, 403) or status_text in (
        "PERMISSION_DENIED",
        "UNAUTHENTICATED",
    ):
        return "auth"
    if status_code == 400 or status_text == "INVALID_ARGUMENT":
        return "invalid_request"
    return "unknown"


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
    ) -> None:
        self.api_key = api_key
        self.field_mask = field_mask
        self.region_code = region_code
        self.language_code = language_code
        self.max_retries = max_retries
        self.timeout = timeout
        # One Session per thread. requests.Session isn't guaranteed thread-safe,
        # and the web app calls search_text concurrently, so we keep sessions
        # thread-local while still getting connection pooling within a thread.
        self._local = threading.local()

    @property
    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            self._local.session = session
        return session

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
        last_status: int | None = None
        for attempt in range(self.max_retries):
            is_last = attempt == self.max_retries - 1
            try:
                resp = self._session.post(
                    config.SEARCH_TEXT_URL,
                    headers=self._headers(),
                    json=body,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error, last_status = f"network error: {exc}", None
                if is_last:
                    break
                self._sleep_backoff(attempt)
                continue

            if resp.status_code == 200:
                return resp.json().get("places", [])

            last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
            last_status = resp.status_code

            # Rate limited or transient server error -> back off and retry.
            if resp.status_code == 429 or resp.status_code >= 500:
                if is_last:
                    break
                self._sleep_backoff(attempt)
                continue

            # Other 4xx errors are not retryable (bad key, bad request, etc.).
            raise PlacesAPIError(
                f"HTTP {resp.status_code}: {resp.text[:500]}",
                reason=classify_error(resp.status_code, resp.text),
                http_status=resp.status_code,
            )

        reason = (
            classify_error(last_status, "") if last_status is not None else "network"
        )
        raise PlacesAPIError(
            f"Giving up after {self.max_retries} attempt(s). Last error: {last_error}",
            reason=reason,
            http_status=last_status,
        )

    def get_place_details(self, place_id: str, detail_field_mask: str) -> dict[str, Any]:
        """Fetch Place Details for a known place ID and return the place object.

        Uses the Place Details endpoint (GET /v1/places/{id}) which is in the
        Pro pricing tier — cheaper per call than Text Search Pro.
        Retries on 429 and 5xx with the same exponential backoff as search_text.
        """
        url = f"{config.PLACE_DETAILS_URL}/{place_id}"
        headers = {
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": detail_field_mask,
        }

        last_error: str | None = None
        last_status: int | None = None
        for attempt in range(self.max_retries):
            is_last = attempt == self.max_retries - 1
            try:
                resp = self._session.get(url, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error, last_status = f"network error: {exc}", None
                if is_last:
                    break
                self._sleep_backoff(attempt)
                continue

            if resp.status_code == 200:
                return resp.json()

            last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
            last_status = resp.status_code

            if resp.status_code == 429 or resp.status_code >= 500:
                if is_last:
                    break
                self._sleep_backoff(attempt)
                continue

            raise PlacesAPIError(
                f"HTTP {resp.status_code}: {resp.text[:500]}",
                reason=classify_error(resp.status_code, resp.text),
                http_status=resp.status_code,
            )

        reason = (
            classify_error(last_status, "") if last_status is not None else "network"
        )
        raise PlacesAPIError(
            f"Giving up after {self.max_retries} attempt(s). Last error: {last_error}",
            reason=reason,
            http_status=last_status,
        )

    @staticmethod
    def _sleep_backoff(attempt: int) -> None:
        # 2s, 4s, 8s, 16s ...
        time.sleep(2 ** (attempt + 1))
