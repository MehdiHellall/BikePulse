from __future__ import annotations

import streamlit as st


def main() -> None:
    st.set_page_config(page_title="BikePulse", layout="wide")
    st.title("BikePulse")
    st.caption("M1: trustworthy Velib history collection")
    st.info(
        "Live predictions start after the M4 scoring milestone. Current work focuses on raw "
        "snapshots, Parquet history, and quality reports."
    )


if __name__ == "__main__":
    main()
