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
