"""Two-stage search orchestration:

Stage 1 — find every Contact/Lead/Account whose Email field matches the
          user's search value (exact address or domain wildcard), via COQL.
Stage 2 — pull the email log for every matched record, via Get Emails of a
          Record, merging/deduping/date-filtering the combined result.

Both stages report progress through simple callbacks so app.py can drive a
Streamlit progress bar without this module depending on Streamlit itself
(keeps this module testable/reusable outside the UI).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

import pandas as pd

from constants import (
    COQL_FULL_LIMIT,
    COQL_PREFLIGHT_LIMIT,
    MAX_MATCHED_RECORDS,
    MAX_WORKERS,
    MODULES,
)
from email_log import filter_by_date_range, merge_and_dedupe, normalize_email_record
from zoho_auth import ZohoTokenManager
from zoho_client import CoqlMatch, ZohoAPIError, get_record_emails, run_coql


# --- Progress callback shapes -------------------------------------------------
# on_stage1_progress(module: str, count_so_far: int) -> None
# on_stage2_progress(done: int, total: int, current_record_name: str) -> None


@dataclass
class SearchResult:
    matches: list[CoqlMatch]
    detail_df: pd.DataFrame
    summary_counts: dict  # {address: count}
    failed_records: list[dict] = field(default_factory=list)
    capped: bool = False  # True if MAX_MATCHED_RECORDS was hit


def _build_where_clause(email_field: str, search_value: str, exact: bool) -> str:
    value = search_value.replace("'", "\\'")
    if exact:
        return f"{email_field} = '{value}'"
    # Domain mode: search_value is expected to already be normalized to
    # '@domain.com' by the caller (see app.py's input normalization).
    return f"{email_field} like '%{value}'"


def preflight_match_count(
    token_manager: ZohoTokenManager, search_value: str, exact: bool
) -> tuple[int, bool]:
    """Run just the first (cheapest) page per module to give the user a
    rough sense of scale before committing to the full pull. Returns
    (count_found_in_first_pages, any_module_has_more) — the second value
    signals "this is a broad search, there may be many more than shown"."""

    total = 0
    has_more = False
    for module, cfg in MODULES.items():
        where = _build_where_clause(cfg["email_field"], search_value, exact)
        count = 0
        gen = run_coql(
            token_manager,
            module,
            cfg["select_fields"],
            where,
            limit=COQL_PREFLIGHT_LIMIT,
        )
        for _ in gen:
            count += 1
        total += count
        if count >= COQL_PREFLIGHT_LIMIT:
            has_more = True
    return total, has_more


def match_records(
    token_manager: ZohoTokenManager,
    search_value: str,
    exact: bool,
    *,
    on_progress: Callable[[str, int], None] | None = None,
) -> tuple[list[CoqlMatch], bool]:
    """Stage 1: find every matching Contact/Lead/Account. Returns
    (matches, capped) where capped is True if MAX_MATCHED_RECORDS was hit
    and the search was stopped early."""

    matches: list[CoqlMatch] = []
    capped = False

    for module, cfg in MODULES.items():
        if capped:
            break
        where = _build_where_clause(cfg["email_field"], search_value, exact)
        module_count = 0
        for record in run_coql(
            token_manager, module, cfg["select_fields"], where, limit=COQL_FULL_LIMIT
        ):
            email = record.get(cfg["email_field"]) or ""
            name = record.get(cfg["name_field"]) or record.get("id", "")
            matches.append(
                CoqlMatch(module=module, record_id=str(record["id"]), email=email, name=str(name))
            )
            module_count += 1
            if on_progress:
                on_progress(module, len(matches))
            if len(matches) >= MAX_MATCHED_RECORDS:
                capped = True
                break

    return matches, capped


def pull_email_logs(
    token_manager: ZohoTokenManager,
    matches: list[CoqlMatch],
    email_types: list[str],
    *,
    on_progress: Callable[[int, int, str], None] | None = None,
    max_workers: int = MAX_WORKERS,
) -> tuple[pd.DataFrame, list[dict]]:
    """Stage 2: fetch and merge the email log for every matched record.

    Uses a small thread pool since these are independent, read-only,
    idempotent calls — cuts wall-clock time on broad searches with many
    matched records. Falls back gracefully (per-record) on failure rather
    than aborting the whole pull."""

    all_rows = []
    failed: list[dict] = []
    total = len(matches)
    done = 0

    def fetch_one(match: CoqlMatch):
        rows = []
        for event in get_record_emails(
            token_manager, match.module, match.record_id, match.name, email_types=email_types
        ):
            rows.extend(normalize_email_record(event))
        return match, rows

    if total == 0:
        return merge_and_dedupe([]), failed

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch_one, m): m for m in matches}
        for future in as_completed(futures):
            match = futures[future]
            done += 1
            try:
                _, rows = future.result()
                all_rows.extend(rows)
            except ZohoAPIError as exc:
                failed.append(
                    {
                        "module": match.module,
                        "record_id": match.record_id,
                        "name": match.name,
                        "error": str(exc),
                    }
                )
            if on_progress:
                on_progress(done, total, match.name)

    detail_df = merge_and_dedupe(all_rows)
    return detail_df, failed


def run_search(
    token_manager: ZohoTokenManager,
    search_value: str,
    exact: bool,
    start_date: date | None,
    end_date: date | None,
    email_types: list[str],
    *,
    on_stage1_progress: Callable[[str, int], None] | None = None,
    on_stage2_progress: Callable[[int, int, str], None] | None = None,
) -> SearchResult:
    """Run the full two-stage search and return a SearchResult ready for
    display and export. Does not itself enforce the pre-flight
    continue/narrow prompt — that's a UI decision made in app.py using
    preflight_match_count() before calling this."""

    matches, capped = match_records(
        token_manager, search_value, exact, on_progress=on_stage1_progress
    )

    detail_df, failed = pull_email_logs(
        token_manager, matches, email_types, on_progress=on_stage2_progress
    )

    detail_df = filter_by_date_range(detail_df, start_date, end_date)

    summary_counts: dict[str, int] = {}
    if not detail_df.empty:
        summary_counts = detail_df["sent_to"].value_counts().to_dict()

    return SearchResult(
        matches=matches,
        detail_df=detail_df,
        summary_counts=summary_counts,
        failed_records=failed,
        capped=capped,
    )
