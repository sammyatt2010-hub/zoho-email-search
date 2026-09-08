"""Simple password gate, mirroring the sycomms-quoting app's pattern:
no accounts, no admin panel — one shared password stored in Streamlit
secrets, checked once per session.
"""

from __future__ import annotations

import streamlit as st


def check_password() -> bool:
    """Returns True once the correct password has been entered this
    session. Renders its own login form and stops the script (via
    st.stop()) until the gate is passed, so callers can simply do:

        if not check_password():
            st.stop()

    at the very top of app.py, before any Zoho call.
    """

    if st.session_state.get("_authed", False):
        return True

    try:
        expected = st.secrets["APP_PASSWORD"]
    except KeyError:
        st.error(
            "No APP_PASSWORD is configured in Streamlit secrets. "
            "Add one to `.streamlit/secrets.toml` (see secrets.toml.example) "
            "before this app can be used."
        )
        return False

    st.title("Zoho CRM Email Log Search")
    st.caption("Enter the shared password to continue.")

    with st.form("password_form"):
        pwd = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Continue")

    if submitted:
        if pwd == expected:
            st.session_state["_authed"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")

    return False
