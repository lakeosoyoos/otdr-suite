"""
.trc on the App's own screens.

The engines, the Viewer and the hub intake read .trc on main; these are the
App-only places that filter by file type: a project shoot's date (read from
the files when the project records none) and the SharePoint loader's walk
(test_sharepoint_link.test_walk_takes_trc_shots).
"""
import os
import shutil

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
DECL = os.path.join(HERE, 'fixtures', 'trc', 'TRCDECL0001_155016251310.trc')


@pytest.fixture
def hub():
    import app
    return app


@pytest.mark.skipif(not os.path.exists(DECL), reason='needs main\'s .trc fixtures (sync)')
def test_a_trc_shoot_is_dated_from_its_files(hub, tmp_path):
    """The shot time a .trc stores (2026-04-17 09:59 UTC), in the same local
    calendar day the .sor path would print for it."""
    import datetime
    for i in (1, 2):
        shutil.copy(DECL, tmp_path / f'TRCDECL{i:04d}_155016251310.trc')
    want = datetime.datetime.fromtimestamp(
        datetime.datetime(2026, 4, 17, 9, 59, 11, tzinfo=datetime.timezone.utc)
        .timestamp()).strftime('%Y-%m-%d')
    assert hub.sor_shot_date(str(tmp_path)) == want
