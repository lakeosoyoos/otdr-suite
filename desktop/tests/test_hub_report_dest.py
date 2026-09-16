"""Every report page lets the tech choose where the report is saved, and the
default is the Downloads folder.

Field report (the boss): a Secret Sauce report and a Uni report were written
"next to the traces".  When the traces came in by drag-and-drop or a zip, that
folder is a temp staging copy, and the report was as good as lost.  His rule:
everything the suite saves defaults to Downloads, and every page gets a folder
picker so he can put it somewhere else.

The hub is a Streamlit page, so this pins the source: one shared row helper,
used on all three pages, each defaulting to `default_report_dir()` (Downloads).
"""
from __future__ import annotations

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SRC = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()


def test_one_shared_save_row_with_a_native_browse_button():
    assert 'def _report_dest_row(key, default_dir):' in SRC
    body = SRC.split('def _report_dest_row(key, default_dir):', 1)[1].split('\ndef ', 1)[0]
    assert "pick_folder('Choose where to save the reports')" in body
    assert "st.text_input('Save reports to', key=key, placeholder=default_dir" in body
    assert 'return default_dir' in body


def test_every_report_page_uses_it_with_downloads_as_the_default():
    assert "_report_dest_row(\n        'ss_report_dest', os.path.join(_fi_dest.default_report_dir(), 'SecretSauce_reports'))" in SRC
    assert "_report_dest_row('uni_report_dest', _fi_dest.default_report_dir())" in SRC
    assert "_report_dest_row('sr_report_dest', _fi.default_report_dir())" in SRC


def test_the_chosen_folder_is_what_the_engines_are_handed():
    assert "out_dir = _ss_dest" in SRC
    assert "out_xlsx = os.path.join(_uni_dest, 'unidirectional_events.xlsx')" in SRC
    assert re.search(r"out_xlsx = os\.path\.join\(_sr_dest,", SRC)
    # The old next-to-the-traces defaults are gone.
    assert "os.path.join(src_folder, 'SecretSauce_reports')" not in SRC
    assert "os.path.join(src_folder, 'unidirectional_events.xlsx')" not in SRC
