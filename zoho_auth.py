"""OAuth token management for the Zoho self-client.

Zoho access tokens expire roughly hourly. This module exchanges the
long-lived refresh token (generated once, manually, via the self-client
"Generate Code" flow in api-console.zoho.eu) for short-lived access tokens,
and refreshes automatically a few minutes before expiry.

The refresh token itself is never expected to change at runtime — if it is
ever revoked, a human has to go back to api-console.zoho.eu and regenerate
it, then update the stored secret.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import requests
import streamlit as st

from constants import REQUEST_TIMEOUT, ZOHO_TOKEN_URL


class ZohoAuthError(RuntimeError):
    """Raised when the OAuth token refresh itself fails (bad credentials,
    revoked refresh token, wrong data center, etc.) — this is a setup/config
    problem, not a transient API error, so callers should surface it clearly
    rather than retrying silently."""


@dataclass
class _TokenState:
    access_token: str | None = None
    expires_at: float = 0.0  # epoch seconds


class ZohoTokenManager:
    """Holds the current access token and refreshes it as needed.

    Thread-safe: Stage 2 of a search may fetch multiple records' email logs
    concurrently (see search.py), and all of those workers share this same
    token manager instance.
    """

    # Refresh this many seconds before actual expiry, as a safety margin
    # against clock skew and in-flight requests.
    _REFRESH_MARGIN_SECONDS = 300

    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._state = _TokenState()
        self._lock = threading.Lock()

    def get_access_token(self) -> str:
        """Return a valid access token, refreshing first if necessary."""
        with self._lock:
            if (
                self._state.access_token is None
                or time.time() > self._state.expires_at - self._REFRESH_MARGIN_SECONDS
            ):
                self._refresh_locked()
            return self._state.access_token  # type: ignore[return-value]

    def force_refresh(self) -> str:
        """Force a refresh regardless of the cached token's apparent expiry.
        Used as a one-shot retry when a call unexpectedly gets a 401, in case
        of clock skew or an early server-side invalidation."""
        with self._lock:
            self._refresh_locked()
            return self._state.access_token  # type: ignore[return-value]

    def _refresh_locked(self) -> None:
        """Must be called with self._lock held."""
        try:
            resp = requests.post(
                ZOHO_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise ZohoAuthError(f"Could not reach Zoho accounts server: {exc}") from exc

        if resp.status_code != 200:
            raise ZohoAuthError(
                f"Zoho token refresh failed (HTTP {resp.status_code}): {resp.text[:500]}"
            )

        payload = resp.json()
        if "access_token" not in payload:
            # Zoho returns 200 with an error body in some misconfiguration
            # cases (e.g. wrong data center, revoked refresh token) rather
            # than a non-200 status.
            raise ZohoAuthError(
                f"Zoho token refresh did not return an access token: {payload}"
            )

        self._state.access_token = payload["access_token"]
        # Subtract a tiny buffer so expires_at is never optimistic.
        self._state.expires_at = time.time() + float(payload.get("expires_in", 3600))


@st.cache_resource(show_spinner=False)
def get_token_manager() -> ZohoTokenManager:
    """Singleton token manager for this Streamlit session/process.

    Cached via st.cache_resource (not st.cache_data) because this object is
    stateful — it holds a live access token and a lock, not a pure function
    result.
    """
    try:
        client_id = st.secrets["ZOHO_CLIENT_ID"]
        client_secret = st.secrets["ZOHO_CLIENT_SECRET"]
        refresh_token = st.secrets["ZOHO_REFRESH_TOKEN"]
    except KeyError as exc:
        raise ZohoAuthError(
            "Missing Zoho credentials in Streamlit secrets. Expected "
            "ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN — see "
            "README.md for how to generate these."
        ) from exc

    return ZohoTokenManager(client_id, client_secret, refresh_token)
