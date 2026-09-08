"""Zoho CRM Email Log Search — Streamlit entry point.

Search Contacts, Leads, and Accounts by exact email address or by domain,
pull every matching email Zoho has logged against them, and export the
combined, deduplicated result as a styled XLSX or PDF report.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import streamlit as st

from auth_gate import check_password
from constants import ALL_EMAIL_TYPES, DEFAULT_EMAIL_TYPES, MAX_MATCHED_RECORDS, MAX_DISPLAY_ROWS
from email_log import CANONICAL_COLUMNS
from export import build_pdf_report, build_summary_df, build_xlsx_report
from search import SearchResult, preflight_match_count, run_search
from zoho_auth import ZohoAuthError, get_token_manager
from zoho_client import ZohoAPIError, get_record_emails

st.set_page_config(page_title="Zoho CRM Email Log Search", page_icon="✉️", layout="wide")

EMAIL_TYPE_LABELS = {
    "sent_from_crm": "Sent from CRM",
    "scheduled_in_crm": "Scheduled in CRM",
    "drafts": "Drafts",
    "user_emails": "User emails (mailbox sync)",
}


def _normalize_search_value(raw: str, exact: bool) -> str:
    value = raw.strip()
    if not exact:
        value = value.lstrip("@")
        value = "@" + value
    return value


def _reset_search_state():
    for key in ("preflight_done", "preflight_count", "preflight_broad", "result"):
        st.session_state.pop(key, None)


def main():
    if not check_password():
        st.stop()

    try:
        token_manager = get_token_manager()
    except ZohoAuthError as exc:
        st.error(f"Zoho credentials are not configured correctly: {exc}")
        st.stop()

    st.title("Zoho CRM Email Log Search")
    st.caption(
        "Search every email Zoho CRM has logged against a Contact, Lead, or Account, "
        "by exact address or by domain, and export a combined report."
    )

    with st.form("search_form"):
        col1, col2 = st.columns([1, 2])
        with col1:
            mode = st.radio("Search mode", ["Exact email", "Domain"], horizontal=False)
        with col2:
            placeholder = "name@example.com" if mode == "Exact email" else "example.com or @example.com"
            search_input = st.text_input("Search value", placeholder=placeholder)

        col3, col4 = st.columns(2)
        with col3:
            start_date = st.date_input("From", value=date.today() - timedelta(days=90))
        with col4:
            end_date = st.date_input("To", value=date.today())

        email_types = st.multiselect(
            "Email types to include",
            options=ALL_EMAIL_TYPES,
            default=DEFAULT_EMAIL_TYPES,
            format_func=lambda t: EMAIL_TYPE_LABELS.get(t, t),
        )

        submitted = st.form_submit_button("Search")

    if submitted:
        _reset_search_state()
        if not search_input.strip():
            st.warning("Enter an email address or domain to search for.")
            st.stop()
        if not email_types:
            st.warning("Select at least one email type to include.")
            st.stop()

        exact = mode == "Exact email"
        value = _normalize_search_value(search_input, exact)

        st.session_state["search_params"] = {
            "value": value,
            "exact": exact,
            "start_date": start_date,
            "end_date": end_date,
            "email_types": email_types,
        }

        with st.spinner("Checking search scope..."):
            try:
                count, has_more, skipped = preflight_match_count(token_manager, value, exact)
            except ZohoAPIError as exc:
                st.error(f"Zoho API error while checking search scope: {exc}")
                st.stop()

        st.session_state["preflight_done"] = True
        st.session_state["preflight_count"] = count
        st.session_state["preflight_broad"] = has_more or count > 50
        st.session_state["preflight_skipped"] = skipped

    # --- Pre-flight safeguard for broad searches ---
    if st.session_state.get("preflight_done") and "result" not in st.session_state:
        params = st.session_state["search_params"]
        count = st.session_state["preflight_count"]
        broad = st.session_state["preflight_broad"]
        preflight_skipped = st.session_state.get("preflight_skipped") or []

        if preflight_skipped:
            skipped_names = ", ".join(s["module"] for s in preflight_skipped)
            st.warning(
                f"Could not search {skipped_names} — that module doesn't have a searchable "
                f"'Email' column in this Zoho org (or another query error occurred). "
                f"Results below only cover the remaining module(s). See details below."
            )
            with st.expander("Skipped module details"):
                st.dataframe(preflight_skipped, use_container_width=True)

        if broad:
            st.warning(
                f"This search matches at least {count} record(s) in Contacts/Leads/Accounts "
                f"(possibly more — this is just the first page). Pulling email history for "
                f"all of them may take a while and use a fair number of API calls. "
                f"A hard cap of {MAX_MATCHED_RECORDS} matched records applies regardless."
            )
            col_a, col_b = st.columns([1, 1])
            with col_a:
                proceed = st.button("Continue with full search", type="primary")
            with col_b:
                narrow = st.button("Let me narrow it instead")
            if narrow:
                _reset_search_state()
                st.rerun()
            if not proceed:
                st.stop()
        else:
            st.info(f"Found {count} matching record(s). Pulling email history...")

        # Run the full two-stage search.
        stage1_status = st.empty()
        stage1_bar = st.progress(0.0)
        stage2_status = st.empty()
        stage2_bar = st.progress(0.0)

        def on_stage1(module: str, found_so_far: int):
            stage1_status.text(f"Stage 1 — matching records: {module}, {found_so_far} found so far...")
            stage1_bar.progress(min(found_so_far / MAX_MATCHED_RECORDS, 1.0))

        def on_stage2(done: int, total: int, current_name: str):
            stage2_status.text(f"Stage 2 — pulling email logs: {done}/{total} ({current_name})")
            stage2_bar.progress(done / total if total else 1.0)

        try:
            result = run_search(
                token_manager,
                params["value"],
                params["exact"],
                params["start_date"],
                params["end_date"],
                params["email_types"],
                on_stage1_progress=on_stage1,
                on_stage2_progress=on_stage2,
            )
        except ZohoAPIError as exc:
            st.error(f"Zoho API error during search: {exc}")
            st.stop()

        stage1_status.empty()
        stage1_bar.empty()
        stage2_status.empty()
        stage2_bar.empty()

        st.session_state["result"] = result

    # --- Results ---
    if "result" in st.session_state:
        result: SearchResult = st.session_state["result"]
        params = st.session_state["search_params"]
        _render_results(result, params, token_manager)


def _render_results(result: SearchResult, params: dict, token_manager=None):
    st.divider()

    if result.skipped_modules:
        skipped_names = ", ".join(s["module"] for s in result.skipped_modules)
        st.warning(
            f"Could not search {skipped_names} — see details below. "
            f"Results only cover the remaining module(s)."
        )
        with st.expander("Skipped module details"):
            st.dataframe(result.skipped_modules, use_container_width=True)

    if not result.matches:
        st.info(
            f"No Contacts, Leads, or Accounts found matching "
            f"**{params['value']}** ({'exact' if params['exact'] else 'domain'})."
        )
        return

    if result.capped:
        st.warning(
            f"Stopped after reaching the {MAX_MATCHED_RECORDS}-record match cap. "
            f"Results below reflect only the first {MAX_MATCHED_RECORDS} matched records — "
            f"narrow the search (e.g. a full email address, or a shorter date range) for a complete picture."
        )

    if result.failed_records:
        with st.expander(f"⚠️ {len(result.failed_records)} record(s) failed to fetch — click to view"):
            st.dataframe(result.failed_records, use_container_width=True)

    detail_df = result.detail_df

    if detail_df.empty:
        st.info(
            f"Matched {len(result.matches)} record(s), but no logged emails were found "
            f"in the selected date range / email types."
        )
        _render_debug_raw_fetch(result, token_manager)
        return

    bounced_n = int(detail_df["bounced"].sum())
    opened_n = int(detail_df["opened"].sum())

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Matched records", len(result.matches))
    m2.metric("Email rows (deduped)", len(detail_df))
    m3.metric("Bounced", bounced_n)
    m4.metric("Opened", opened_n)

    st.subheader("Breakdown by recipient address")
    summary_df = build_summary_df(detail_df)
    st.dataframe(summary_df, use_container_width=True, hide_index=True)

    st.subheader(f"Email log detail (showing up to {MAX_DISPLAY_ROWS} of {len(detail_df)} rows)")
    display_df = detail_df.head(MAX_DISPLAY_ROWS)[
        ["sent_on", "subject", "sent_to", "direction", "source_module", "source_record_name", "opened", "bounced"]
    ]
    st.dataframe(display_df, use_container_width=True, hide_index=True)

    st.subheader("Export")
    meta = {
        "search_value": params["value"],
        "search_mode": "Exact" if params["exact"] else "Domain",
        "date_from": params["start_date"].isoformat(),
        "date_to": params["end_date"].isoformat(),
        "matched_record_count": len(result.matches),
        "exported_on": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "failed_record_count": len(result.failed_records),
    }

    safe_value = params["value"].lstrip("@").replace("/", "-")
    filename_stem = f"zoho-email-log_{safe_value}_{params['start_date']}_to_{params['end_date']}"

    col1, col2 = st.columns(2)
    with col1:
        xlsx_buf = build_xlsx_report(detail_df, summary_df, meta)
        st.download_button(
            "Download XLSX report",
            data=xlsx_buf,
            file_name=f"{filename_stem}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )
    with col2:
        pdf_buf = build_pdf_report(detail_df, summary_df, meta)
        st.download_button(
            "Download PDF report",
            data=pdf_buf,
            file_name=f"{filename_stem}.pdf",
            mime="application/pdf",
            use_container_width=True,
        )


def _render_debug_raw_fetch(result: SearchResult, token_manager):
    """Temporary diagnostic: matched records but zero normalized rows is
    suspicious given known email history exists in this org. Fetch the raw
    (pre-normalization) response for the first matched record across every
    email type, so we can see exactly what Zoho is sending back rather than
    guess at why normalize_email_record() produced nothing."""

    if token_manager is None or not result.matches:
        return

    with st.expander("🔧 Debug: raw API response for the first matched record"):
        match = result.matches[0]
        st.write(f"Fetching raw emails for: **{match.module} / {match.name}** (id `{match.record_id}`)")
        try:
            events = list(
                get_record_emails(
                    token_manager,
                    match.module,
                    match.record_id,
                    match.name,
                    email_types=ALL_EMAIL_TYPES,
                )
            )
        except ZohoAPIError as exc:
            st.error(f"Raw fetch failed: {exc}")
            return

        st.write(f"Raw event count across all types: **{len(events)}**")
        if events:
            st.json(events[0].raw)
            if len(events) > 1:
                st.caption(f"(+{len(events) - 1} more not shown)")
        else:
            st.write("Zoho returned zero email events for this record across all four types.")


if __name__ == "__main__":
    main()
