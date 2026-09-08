"""Zoho CRM Email Log Search — Streamlit entry point.

Search a manually-exported Zoho CRM "Sent Email Status" report (one or more
XLSX/CSV files) by exact email address or by domain, filter by date range,
and export the combined, deduplicated result as a styled XLSX or PDF report.

Note: this app originally also supported a live Zoho API search (COQL match
against Contacts/Leads/Accounts + Get Emails of a Record per match). That
path is removed here because this org's mailbox-synced email history (Zoho's
`user_emails` type) fails with a Zoho-side `CANNOT_PROCESS` error, and the
uploaded-report path below is the one that actually works — see
search.py/zoho_auth.py/zoho_client.py/constants.py in this repo if that live
path is ever wanted back (e.g. once Zoho's mailbox sync is fixed for this
org), and README.md's git history for the original UI.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import streamlit as st

from auth_gate import check_password
from constants import MAX_DISPLAY_ROWS
from email_log import filter_by_date_range
from export import build_pdf_report, build_summary_df, build_xlsx_report
from report_import import filter_by_address, parse_report_files

st.set_page_config(page_title="Zoho CRM Email Log Search", page_icon="✉️", layout="wide")


def _normalize_search_value(raw: str, exact: bool) -> str:
    value = raw.strip()
    if not exact:
        value = value.lstrip("@")
        value = "@" + value
    return value


def _reset_search_state():
    st.session_state.pop("report_result", None)


def main():
    if not check_password():
        st.stop()

    st.title("Zoho CRM Email Log Search")
    st.caption(
        "Search a Zoho CRM 'Sent Email Status' report export by exact email address "
        "or by domain, filter by date range, and export a combined report."
    )
    st.info(
        "In Zoho CRM, go to **Reports → All Reports → Sent Email Status**, run it, "
        "and export the result (XLSX or CSV) — Zoho paginates large reports into "
        "several files (e.g. rows 0–1000, 1000–2000, ...); upload as many as you "
        "have and they'll be merged and deduplicated automatically. Everything below "
        "runs against that uploaded data — no Zoho API calls involved."
    )

    with st.form("report_search_form"):
        uploaded_files = st.file_uploader(
            "Sent Email Status export(s)",
            type=["xlsx", "csv"],
            accept_multiple_files=True,
        )

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

        submitted = st.form_submit_button("Search uploaded report(s)")

    if submitted:
        _reset_search_state()

        if not uploaded_files:
            st.warning("Upload at least one Sent Email Status report file (.xlsx or .csv).")
            st.stop()
        if not search_input.strip():
            st.warning("Enter an email address or domain to search for.")
            st.stop()

        exact = mode == "Exact email"
        value = _normalize_search_value(search_input, exact)

        files = [(f.name, f.getvalue()) for f in uploaded_files]
        with st.spinner("Parsing uploaded report file(s)..."):
            try:
                merged_df, infos = parse_report_files(files)
            except Exception as exc:  # noqa: BLE001 - surface any parse failure, don't crash the app
                st.error(f"Could not parse the uploaded file(s): {exc}")
                st.stop()

        filtered_df = filter_by_address(merged_df, value, exact)
        filtered_df = filter_by_date_range(filtered_df, start_date, end_date)

        st.session_state["report_result"] = {
            "df": filtered_df,
            "total_parsed": len(merged_df),
            "infos": infos,
            "params": {
                "value": value,
                "exact": exact,
                "start_date": start_date,
                "end_date": end_date,
            },
        }

    if "report_result" in st.session_state:
        _render_report_results(st.session_state["report_result"])


def _render_report_results(report_result: dict):
    st.divider()

    infos = report_result["infos"]
    errors = [i for i in infos if "error" in i]
    parsed = [i for i in infos if "error" not in i]

    if errors:
        with st.expander(f"⚠️ {len(errors)} file(s) could not be parsed — click to view", expanded=True):
            st.dataframe(errors, use_container_width=True)

    if parsed:
        total_data_rows = sum(i["data_rows"] for i in parsed)
        st.caption(
            f"Parsed {len(parsed)} file(s), {total_data_rows} report row(s) in total, "
            f"{report_result['total_parsed']} deduplicated email event(s) across all "
            f"uploaded files (before the search/date filter below)."
        )

    params = report_result["params"]
    detail_df = report_result["df"]

    if detail_df.empty:
        st.info(
            f"No emails found for **{params['value']}** "
            f"({'exact' if params['exact'] else 'domain'}) in the uploaded report(s) "
            f"within the selected date range."
        )
        return

    bounced_n = int(detail_df["bounced"].sum())
    opened_n = int(detail_df["opened"].sum())

    m1, m2, m3 = st.columns(3)
    m1.metric("Email rows (deduped)", len(detail_df))
    m2.metric("Bounced", bounced_n)
    m3.metric("Opened", opened_n)

    st.subheader("Breakdown by recipient address")
    summary_df = build_summary_df(detail_df)
    st.dataframe(summary_df, use_container_width=True, hide_index=True)

    st.subheader(f"Email log detail (showing up to {MAX_DISPLAY_ROWS} of {len(detail_df)} rows)")
    display_df = detail_df.head(MAX_DISPLAY_ROWS)[
        ["sent_on", "subject", "sent_to", "direction", "source_module", "opened", "bounced"]
    ]
    st.dataframe(display_df, use_container_width=True, hide_index=True)

    st.subheader("Export")
    meta = {
        "search_value": params["value"],
        "search_mode": "Exact" if params["exact"] else "Domain",
        "date_from": params["start_date"].isoformat(),
        "date_to": params["end_date"].isoformat(),
        "matched_record_count": len(detail_df),
        "record_count_label": "Matching email row(s) found in uploaded report(s):",
        "exported_on": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "failed_record_count": 0,
        "source_label": (
            "Source: Zoho CRM 'Sent Email Status' report export(s), via the "
            "zoho-email-search tool."
        ),
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
            key="report_xlsx_dl",
        )
    with col2:
        pdf_buf = build_pdf_report(detail_df, summary_df, meta)
        st.download_button(
            "Download PDF report",
            data=pdf_buf,
            file_name=f"{filename_stem}.pdf",
            mime="application/pdf",
            use_container_width=True,
            key="report_pdf_dl",
        )


if __name__ == "__main__":
    main()
