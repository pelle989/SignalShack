"""Multi-step setup wizard: resumability, ZIP-confirm path, done screen."""

from app.core import db
from tests.test_admin import client


def step1(c, password="hunter2hunter2"):
    return c.post("/admin/setup", data={"password": password,
                                        "password2": password},
                  follow_redirects=False)


def test_wizard_resumes_at_right_step(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as c:
        # fresh box: setup shows password step
        assert c.get("/admin/setup").status_code == 200
        r = step1(c)
        assert r.headers["location"] == "/admin/setup/location"
        # revisiting step 1 skips ahead — resumable
        r = c.get("/admin/setup", follow_redirects=False)
        assert r.headers["location"] == "/admin/setup/location"
        # done page refuses to render before location exists
        r = c.get("/admin/setup/done", follow_redirects=False)
        assert r.headers["location"] == "/admin/setup/location"


def test_zip_path_shows_confirm_then_creates(tmp_path, monkeypatch):
    import app.admin.routes as routes

    async def fake_geocode(zipcode):
        assert zipcode == "11758"
        return {"lat": 40.68, "lon": -73.47, "label": "Massapequa",
                "tz": "America/New_York"}

    monkeypatch.setattr(routes, "_geocode_zip", fake_geocode)
    with client(tmp_path, monkeypatch) as c:
        step1(c)
        # ZIP submit -> confirm screen with resolved MEANING, nothing created yet
        r = c.post("/admin/setup/location", data={"zipcode": "11758"})
        assert "Massapequa" in r.text and "Is this right?" in r.text
        conn = db.connect()
        assert not conn.execute("SELECT 1 FROM location").fetchone()
        # confirm -> location + board created, done screen renders with QR
        import re
        csrf = re.search(r'name="csrf" value="([^"]+)"', r.text).group(1)
        r = c.post("/admin/setup/confirm", data={
            "zipcode": "11758", "latitude": 40.68, "longitude": -73.47,
            "label": "Home", "timezone": "America/New_York", "csrf": csrf},
            follow_redirects=False)
        assert r.headers["location"] == "/admin/setup/done"
        assert conn.execute("SELECT 1 FROM location WHERE is_primary=1").fetchone()
        assert conn.execute("SELECT 1 FROM board WHERE is_default=1").fetchone()
        conn.close()
        r = c.get("/admin/setup/done")
        assert "<svg" in r.text                      # QR present
        assert "signalshack.local/display" in r.text
        assert "first announcement" in r.text.lower()


def test_geocode_failure_offers_manual_path(tmp_path, monkeypatch):
    import app.admin.routes as routes

    async def broken_geocode(zipcode):
        return None

    monkeypatch.setattr(routes, "_geocode_zip", broken_geocode)
    with client(tmp_path, monkeypatch) as c:
        step1(c)
        r = c.post("/admin/setup/location", data={"zipcode": "00000"})
        assert "ZIP lookup failed" in r.text
        assert 'name="latitude"' in r.text           # manual fields opened


def test_completed_setup_redirects_away(tmp_path, monkeypatch):
    from tests.test_admin import do_setup
    with client(tmp_path, monkeypatch) as c:
        do_setup(c)
        for path in ("/admin/setup", "/admin/setup/location"):
            r = c.get(path, follow_redirects=False)
            assert r.headers["location"] == "/admin"
