"""Password gate shared by the hosted Streamlit entry points.

The hosted Scraper tab can start runs that write to Supabase, so the public
Render URL must not be usable by whoever finds it.
"""

from __future__ import annotations

import hmac
import os

import streamlit as st

_AUTHENTICATED = "authenticated"
_TRUTHY = {"1", "true", "yes", "on"}


def hosted_deployment() -> bool:
    """Report whether this process is the hosted Render deployment."""
    return os.getenv("HOSTED_DASHBOARD", "").strip().casefold() in _TRUTHY


def require_password() -> None:
    """Stop the script until the visitor supplies the deployment password.

    Local runs stay unauthenticated: without APP_PASSWORD there is nothing to
    check. A hosted deployment missing the variable refuses to serve instead,
    so a forgotten secret cannot quietly publish the dashboard.
    """
    password = os.getenv("APP_PASSWORD", "")
    if not password:
        if hosted_deployment():
            st.error(
                "This deployment is missing APP_PASSWORD, so it will not "
                "serve the dashboard. Set it in the Render service "
                "environment and redeploy.",
                icon=":material/lock:",
            )
            st.stop()
        return

    if st.session_state.get(_AUTHENTICATED):
        return

    with st.form("password_gate"):
        st.subheader("Beauty pricing dashboard")
        entered = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in")
    if submitted:
        if hmac.compare_digest(
            entered.encode("utf-8"),
            password.encode("utf-8"),
        ):
            st.session_state[_AUTHENTICATED] = True
            st.rerun()
        st.error("Incorrect password.", icon=":material/lock:")
    st.stop()
