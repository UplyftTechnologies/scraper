"""Entry point combining the scraper and the product viewer in one app."""

from __future__ import annotations

import streamlit as st

from app_auth import require_password

st.set_page_config(
    page_title="Beauty pricing dashboard",
    page_icon=":material/monitoring:",
    layout="wide",
)

require_password()

page = st.navigation(
    [
        st.Page(
            "app_pages/scraper.py",
            title="Scraper",
            icon=":material/monitoring:",
            default=True,
        ),
        st.Page(
            "app_pages/product_view.py",
            title="Product view",
            icon=":material/shopping_bag:",
        ),
    ],
    position="top",
)
page.run()
