from __future__ import annotations

from fastapi.testclient import TestClient

from sepko.main import app


def test_bulk_delete_url_is_not_parsed_as_invoice_id():
    """GET/POST /racuni/bulk-obrisi ne smije pasti na /racuni/{invoice_id} (422 int_parsing)."""
    client = TestClient(app, raise_server_exceptions=False)
    for path in ("/racuni/bulk-obrisi", "/racuni/obrisi-odabrane"):
        get_r = client.get(path, follow_redirects=False)
        assert get_r.status_code == 303, get_r.text
        assert "int_parsing" not in get_r.text
        post_r = client.post(path, data={"csrf_token": "x"}, follow_redirects=False)
        assert post_r.status_code == 303, post_r.text
        assert "int_parsing" not in post_r.text


def test_numeric_invoice_path_still_routes():
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/racuni/123", follow_redirects=False)
    assert r.status_code in (200, 303, 404)
    assert "int_parsing" not in r.text


def test_pwa_manifest_is_dynamic():
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    data = r.json()
    assert data["start_url"] == "/app"
    assert data["display"] == "standalone"
    assert data["launch_handler"]["client_mode"] == "focus-existing"
    assert data["related_applications"]


def test_pos_kasa_requires_auth():
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/kasa", follow_redirects=False)
    assert r.status_code == 303
    assert "/login" in r.headers.get("location", "")


def test_pos_pwa_requires_auth():
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/app/kasa", follow_redirects=False)
    assert r.status_code == 303
    assert "/login" in r.headers.get("location", "")


def test_pos_kasa_manifest():
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/manifest-kasa.webmanifest")
    assert r.status_code == 200
    data = r.json()
    assert data["start_url"] == "/app/kasa"
    assert data["short_name"] == "Kasa"


def test_dobavljaci_redirects_to_kupci():
    client = TestClient(app, raise_server_exceptions=False)
    for path in (
        "/dobavljaci",
        "/dobavljaci/1",
        "/dobavljaci/1/obrisi",
        "/dobavljaci/lookup.json",
    ):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303, path
        loc = r.headers.get("location", "")
        assert loc.startswith("/kupci"), (path, loc)
        assert "?edit=" not in loc


def test_company_logo_route_requires_auth():
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/podesavanja/logo", follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers.get("location", "")
    assert "/login" in loc or loc == "/"
