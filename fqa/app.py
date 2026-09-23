"""
FQA Builder — standalone Streamlit app
======================================

A thin wrapper so the tool can be run on its own, without the hub:

    streamlit run fqa/app.py --server.port 8514 --server.maxUploadSize 512

The interface itself lives in fqa/ui.py, which the OTDR Suite hub renders
as one of its pages.  One copy, so the standalone app and the hub page
cannot drift.

Robert, 2026-09-23.
"""
from __future__ import annotations

import os
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fqa.ui import render                                        # noqa: E402

PORT = 8514                     # see ~/Desktop/ports_registry.md

st.set_page_config(page_title='FQA Builder', layout='wide')
render()
