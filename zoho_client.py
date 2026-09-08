"""Thin wrappers around the two Zoho CRM API calls this app needs:

- run_coql(): Query API (COQL) — used to find Contacts/Leads/Accounts whose
  Email field matches a search value or domain (Stage 1).
- get_record_emails(): Get Emails of a Record — used to pull the email log
  for each matched record, 10 at a time (Stage 2).

Both go through a shared _request() helper that attaches the current access
token and handles the "token expired mid-request" edge case with one forced
refresh + retry, plus a generic retry/backoff policy for 429/5xx responses.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterator

import requests

from constants import (
    COQL_FULL_LIMIT,
    MAX_RETRIES,
    RETRY_BACKOFF_BASE,
    REQUEST_TIMEOUT,
    ZOHO_API_BASE,
    ZOHO_COQL_URL,
)
from zoho_auth import ZohoAuthError, ZohoTokenManager


class ZohoAPIError(RuntimeError):
    """Raised for a Zoho API error that isn't a plain auth failure — e.g. a
    malformed query, an invalid module name, or a non-retryable 4xx. Callers
    that are iterating over many records should catch this per-record rather
    than letting one bad record abort the whole search."""

    def __init__(self, message: str, status_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


@dataclass
class CoqlMatch:
    """One record matched at Stage 1."""

    module: str
    record_id: str
    email: str
    name: str


@dataclass
class EmailEvent:
    """One raw email event as returned by Get Emails of a Record, tagged
    with which record's log it came from. Kept close to the raw API shape —
    normalization into the canonical report schema happens in email_log.py."""

    source_module: str
    source_record_id: str
    source_record_name: str
    raw: dict = field(default_factory=dict)


def _is_retryable(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code < 600


def _request(
    token_manager: ZohoTokenManager,
    method: str,
    url: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
) -> dict:
    """Make one authenticated Zoho API call, handling token refresh and
    retry/backoff. Raises ZohoAPIError on a non-retryable or exhausted
    failure; raises ZohoAuthError if the token refresh itself fails."""

    last_error: Exception | None = None
    forced_refresh_done = False

    for attempt in range(MAX_RETRIES + 1):
        token = token_manager.get_access_token()
        headers = {"Authorization": f"Zoho-oauthtoken {token}"}

        try:
            resp = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
                continue
            raise ZohoAPIError(f"Network error calling Zoho API: {exc}") from exc

        if resp.status_code == 401 and not forced_refresh_done:
            # Token may have expired mid-flight (clock skew, early
            # server-side invalidation). Force one refresh and retry once,
            # without counting it against the normal retry budget.
            forced_refresh_done = True
            token_manager.force_refresh()
            continue

        if resp.status_code == 200 or resp.status_code == 204:
            if not resp.content:
                return {}
            return resp.json()

        if _is_retryable(resp.status_code) and attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
            continue

        # Non-retryable (or retries exhausted) — surface a clear error.
        try:
            payload = resp.json()
        except ValueError:
            payload = resp.text[:500]

        code = None
        if isinstance(payload, dict):
            code = payload.get("code")

        raise ZohoAPIError(
            f"Zoho API call failed (HTTP {resp.status_code}, code={code}): {payload}",
            status_code=resp.status_code,
            payload=payload,
        )

    # Should not be reachable, but keeps type-checkers happy.
    raise ZohoAPIError(f"Zoho API call failed after retries: {last_error}")


def run_coql(
    token_manager: ZohoTokenManager,
    module: str,
    select_fields: list[str],
    where_clause: str,
    *,
    limit: int = COQL_FULL_LIMIT,
    order_by: str | None = None,
) -> Iterator[dict]:
    """Yield matching records from one module via COQL, paging until
    exhausted or `limit` total records have been returned.

    COQL itself is single-module per query and pages via LIMIT/OFFSET-style
    cursoring driven by `info.more_records` in the response — this generator
    hides that behind a simple iterator so callers don't need to think about
    pagination.
    """

    fields = ", ".join(select_fields)
    offset = 0
    page_size = min(limit, 1000)  # Zoho's hard per-call cap is 2000; stay well under it.
    yielded = 0

    while True:
        remaining = limit - yielded
        if remaining <= 0:
            return
        this_page = min(page_size, remaining)

        query = f"SELECT {fields} FROM {module} WHERE {where_clause}"
        if order_by:
            query += f" ORDER BY {order_by}"
        query += f" LIMIT {offset}, {this_page}"

        body = {"select_query": query}
        result = _request(token_manager, "POST", ZOHO_COQL_URL, json_body=body)

        data = result.get("data", [])
        for record in data:
            yield record
            yielded += 1

        info = result.get("info", {})
        if not info.get("more_records") or not data:
            return
        offset += len(data)


def get_record_emails(
    token_manager: ZohoTokenManager,
    module: str,
    record_id: str,
    record_name: str,
    *,
    email_types: list[str],
) -> Iterator[EmailEvent]:
    """Yield every email event logged against one record, across the given
    `type` filters (e.g. ["sent_from_crm"]), paginating 10-at-a-time per
    Zoho's fixed page size for this endpoint."""

    url = f"{ZOHO_API_BASE}/crm/v8/{module}/{record_id}/Emails"

    for email_type in email_types:
        index = 1
        while True:
            params = {"type": email_type, "index": index}
            try:
                result = _request(token_manager, "GET", url, params=params)
            except ZohoAPIError as exc:
                # A record with no email history at all sometimes comes back
                # as a 204/empty payload (handled above) but some orgs
                # return a 400 "no content" style error instead — treat that
                # specific case as "no emails of this type", not a failure.
                if exc.status_code == 204:
                    break
                raise

            emails = result.get("Emails", [])
            if not emails:
                break

            for raw in emails:
                yield EmailEvent(
                    source_module=module,
                    source_record_id=record_id,
                    source_record_name=record_name,
                    raw=raw,
                )

            info = result.get("info", {})
            if not info.get("more_records"):
                break
            index = info.get("next_index", index + 10)
