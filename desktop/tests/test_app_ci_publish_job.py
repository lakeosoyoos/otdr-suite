"""OTDR Suite App: CI's publish job takes the App's own build files.

main #473 split CI into tests / build / publish jobs; the publish job gets
the installer and portable zip from the build job as an artifact.  The App
builds OTDRSuiteApp-Setup.exe and uploads the artifact as
"OTDRSuiteApp-Windows", so its publish job must download that name and check
that file.  With main's names the download found nothing and every App build
went red after the tests and the build had passed.
"""
import re

from conftest import REPO_ROOT

CI = (REPO_ROOT / '.github' / 'workflows' / 'build-windows.yml').read_text(encoding='utf-8')


def _jobs():
    pub = CI.index('\n  publish:\n')
    return CI[:pub], CI[pub:]


def _artifact_names(text, action):
    return re.findall(r'uses: actions/' + action + r'@\S+[^\n]*\n\s+with:\n\s+name: (\S+)', text)


def test_publish_downloads_the_artifact_the_build_uploads():
    build, publish = _jobs()
    up = _artifact_names(build, 'upload-artifact')
    down = _artifact_names(publish, 'download-artifact')
    assert up == ['OTDRSuiteApp-Windows'], up
    assert down == up, (up, down)


def test_the_branch_rehearsal_checks_the_apps_installer():
    _, publish = _jobs()
    step = publish[publish.index('Rehearse the Release files'):]
    step = step[:step.index('\n      - name:')]
    assert 'dist\\OTDRSuiteApp-Setup.exe' in step
    assert 'OTDRSuite-Setup.exe' not in step


def test_the_app_release_is_published_from_the_downloaded_installer():
    _, publish = _jobs()
    assert '--installer desktop/dist/OTDRSuiteApp-Setup.exe' in publish
    assert publish.index('Download the build') < publish.index('publish_app_release.py')
