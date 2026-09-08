"""Normalizes raw Zoho email events into a canonical flat schema, and
merges/dedupes/date-filters the combined pull from Stage 2.

The canonical schema (one row per recipient per email event) is the seam
between the live API and the report builders in export.py — export.py never
sees raw Zoho JSON, only this normalized shape, which is also what lets
export.py be shared with the earlier one-off CSV-based script if desired.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Iterable

import pandas as pd

from zoho_client import EmailEvent

# Canonical column order, used consistently across the results table and
# both export builders.
CANONICAL_COLUMNS = [
    "email_id",
    "sent_to",
    "sent_to_name",
    "subject",
    "sent_on",
    "direction",
    "status",
    "bounced",
    "opened",
    "source_module",
    "source_record_id",
    "source_record_name",
]


@dataclass
class NormalizedRow:
    email_id: str | None
    sent_to: str
    sent_to_name: str
    subject: str
    sent_on: datetime | None
    direction: str
    status: str
    bounced: bool
    opened: bool
    source_module: str
    source_record_id: str
    source_record_name: str


def _parse_datetime(value) -> datetime | None:
    """Zoho typically returns ISO-8601 timestamps with a timezone offset,
    e.g. '2026-09-08T09:39:00+01:00'. Falls back to None for anything we
    can't parse rather than raising, so one malformed record doesn't take
    down the whole merge."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        # Python's fromisoformat handles the +HH:MM style offset Zoho uses.
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_recipients(raw: dict) -> list[dict]:
    """Zoho's 'to' field on an email event is expected to be a list of
    {email, name?} objects (or similar) rather than the
    'Name<email>,Name<email>' string format used by the old CSV export. Be
    defensive about the exact shape here since it wasn't independently
    confirmed against a live response this session (see plan's Open
    Questions) — handle a few plausible shapes rather than assuming one."""

    to_field = raw.get("to") or raw.get("To") or raw.get("to_address")

    if to_field is None:
        return []

    # Case 1: already a list of dicts with an email-like key.
    if isinstance(to_field, list):
        out = []
        for item in to_field:
            if isinstance(item, dict):
                email = item.get("email") or item.get("Email") or item.get("address")
                name = item.get("name") or item.get("Name") or ""
                if email:
                    out.append({"email": email, "name": name})
            elif isinstance(item, str):
                out.append({"email": item, "name": ""})
        return out

    # Case 2: a single string, possibly comma-separated, possibly with
    # 'Name<email>' formatting (fallback compatibility with the report-export
    # style seen in the manual CSV investigation).
    if isinstance(to_field, str):
        import re

        parts = [p.strip() for p in to_field.split(",") if p.strip()]
        out = []
        for part in parts:
            m = re.match(r"^(.*?)<([^<>]+)>$", part)
            if m:
                out.append({"email": m.group(2).strip(), "name": m.group(1).strip()})
            else:
                out.append({"email": part, "name": ""})
        return out

    return []


def normalize_email_record(event: EmailEvent) -> list[NormalizedRow]:
    """Map one raw EmailEvent into zero or more canonical rows (one per
    recipient — an email sent to 3 addresses becomes 3 rows, matching the
    per-recipient counting approach used in today's Servicevend report)."""

    raw = event.raw
    recipients = _extract_recipients(raw)
    if not recipients:
        return []

    email_id = raw.get("id") or raw.get("mail_id") or raw.get("message_id")
    subject = raw.get("subject") or raw.get("Subject") or ""
    sent_on = _parse_datetime(
        raw.get("sent_time") or raw.get("Sent_On") or raw.get("Modified_Time")
    )
    direction = raw.get("type") or raw.get("status") or ""
    status = raw.get("status") or raw.get("delivery_status") or ""

    status_lower = str(status).lower()
    bounced = "bounce" in status_lower or bool(raw.get("bounced_time") or raw.get("Bounced_Time"))
    opened = bool(
        raw.get("opened_time")
        or raw.get("Last_Opened")
        or "open" in status_lower
        or raw.get("is_read")
    )

    rows = []
    for r in recipients:
        rows.append(
            NormalizedRow(
                email_id=str(email_id) if email_id else None,
                sent_to=r["email"],
                sent_to_name=r["name"],
                subject=subject,
                sent_on=sent_on,
                direction=str(direction),
                status=str(status),
                bounced=bounced,
                opened=opened,
                source_module=event.source_module,
                source_record_id=event.source_record_id,
                source_record_name=event.source_record_name,
            )
        )
    return rows


def merge_and_dedupe(rows: Iterable[NormalizedRow]) -> pd.DataFrame:
    """Combine normalized rows from every matched record's email log into
    one deduped DataFrame, sorted most-recent-first.

    Dedupe strategy: prefer the stable `email_id` Zoho assigns to the event
    if present (the same email can legitimately appear in more than one
    matched record's log, e.g. a Contact and its parent Account). Falls back
    to a (sent_to, subject, sent_on, module) tuple key for rows with no
    email_id, matching the approach used in the earlier CSV-based script.
    """

    seen_ids: set[str] = set()
    seen_tuples: set[tuple] = set()
    deduped: list[NormalizedRow] = []

    for row in rows:
        if row.email_id:
            key = ("id", row.email_id, row.sent_to)
            if key in seen_ids:
                continue
            seen_ids.add(key)
        else:
            key = (row.sent_to, row.subject, row.sent_on, row.source_module)
            if key in seen_tuples:
                continue
            seen_tuples.add(key)
        deduped.append(row)

    df = pd.DataFrame([asdict(r) for r in deduped], columns=CANONICAL_COLUMNS)
    if not df.empty:
        # asdict() produces a column of plain Python datetime objects (or
        # None), which pandas leaves as dtype 'object' rather than a proper
        # datetime64 dtype -- that silently breaks the .dt accessor used
        # later (filter_by_date_range, export.py's .strftime() calls still
        # work either way, but .dt.date does not on an object column).
        # Normalize explicitly here, once, right after construction.
        df["sent_on"] = pd.to_datetime(df["sent_on"], utc=True, errors="coerce")
        df = df.sort_values("sent_on", ascending=False, na_position="last").reset_index(drop=True)
    return df


def filter_by_date_range(df: pd.DataFrame, start_date, end_date) -> pd.DataFrame:
    """Post-filter the merged DataFrame to a date range (inclusive). Applied
    after merge/dedupe since Get-Emails-of-Record has no server-side date
    filter — see plan §3/§4.

    Compares full UTC Timestamps rather than using the .dt.date accessor:
    .dt.date on a tz-aware column returned a datetime-like array (not a
    plain object array of date()s) in the pandas/numpy build Streamlit
    Cloud runs, which made the >=/<= comparison against a plain
    datetime.date raise "Invalid comparison between dtype=... and
    datetime.date" — a version-specific accessor quirk this sidesteps by
    only ever comparing like-typed Timestamps."""

    if df.empty:
        return df

    mask = df["sent_on"].notna()
    if start_date is not None:
        start_ts = pd.Timestamp(start_date, tz="UTC")
        mask &= df["sent_on"] >= start_ts
    if end_date is not None:
        # Exclusive upper bound at the start of the *next* day, so the
        # chosen end_date is fully included regardless of time-of-day.
        end_ts = pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1)
        mask &= df["sent_on"] < end_ts
    return df[mask].reset_index(drop=True)
