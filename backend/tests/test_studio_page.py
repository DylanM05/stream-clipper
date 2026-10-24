"""The parts of the Studio automation that read the upload dialog, checked against a stand-in page.

Needs a browser; skipped where none is installed (the real Studio is only reachable signed in).
"""

import pytest

from clipper.studio import Studio, StudioError

DIALOG = """
<a href="https://www.youtube.com/watch?v=OLDVIDEO123">An older video in the list behind the dialog</a>
<ytcp-uploads-dialog>
  <div>Video link</div>
  <a class="renamed-by-youtube" href="https://youtube.com/shorts/NEWSHORT123">https://youtube.com/shorts/NEWSHORT123</a>
  <span id="footer">Uploading 45% ... 2 minutes left</span>
</ytcp-uploads-dialog>
"""


@pytest.fixture(scope="module")
def page():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = None
        for kwargs in ({}, {"channel": "msedge"}, {"channel": "chrome"}):
            try:
                browser = p.chromium.launch(**kwargs)
                break
            except Exception:
                continue
        if browser is None:
            pytest.skip("no browser available")
        yield browser.new_page()
        browser.close()


def test_finds_the_new_video_link_inside_the_dialog_only(page):
    page.set_content(DIALOG)
    assert Studio._video_link(page, wait_s=2) == "https://youtube.com/shorts/NEWSHORT123"


def test_waits_while_uploading_and_reports_progress(page):
    page.set_content(DIALOG)
    seen = []
    # Finish the upload a moment later, the way Studio's footer changes.
    page.evaluate("setTimeout(() => document.getElementById('footer').textContent = 'Checks complete. No issues found.', 1500)")
    Studio("unused", None)._wait_for_upload(page, seen.append)
    assert seen[0] == 0.45 and seen[-1] == 1.0


def _google_page(page, body: str):
    """A fresh tab where Google's account pages and Studio are stand-ins served without the network."""
    tab = page.context.browser.new_page()
    tab.route("https://accounts.google.com/**", lambda r: r.fulfill(content_type="text/html", body=body))
    tab.route("https://studio.youtube.com/**", lambda r: r.fulfill(content_type="text/html", body="<input type=file>"))
    tab.goto("https://accounts.google.com/ServiceLogin?passive=true", wait_until="domcontentloaded")
    return tab


def test_waits_for_google_to_send_the_page_on_to_studio(page):
    tab = _google_page(page, "<script>setTimeout(() => location.href = 'https://studio.youtube.com/channel/UCx/videos/upload', 1000)</script>")
    Studio._wait_for_studio(tab, wait_s=10)
    assert tab.url.startswith("https://studio.youtube.com/")
    tab.close()


def test_a_sign_in_page_that_stays_means_signing_in(page):
    tab = _google_page(page, "<form>Sign in</form>")
    with pytest.raises(StudioError, match="sign in"):
        Studio._wait_for_studio(tab, wait_s=2)
    tab.close()
