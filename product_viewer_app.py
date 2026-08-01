"""Viewer-only entry point used by the hosted read-only deployment."""

from __future__ import annotations

import streamlit as st

st.set_page_config(
    page_title="Beauty catalogue",
    page_icon=":material/shopping_bag:",
    layout="wide",
)

page = st.navigation(
    [
        st.Page(
            "app_pages/product_view.py",
            title="Product view",
            icon=":material/shopping_bag:",
        )
    ],
    position="hidden",
)
page.run()
