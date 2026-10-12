"""Every method a route answers passes the browser's preflight: a method left out
of the CORS list fails before the route is reached, as a bare 400 on OPTIONS."""

import pytest
from fastapi.testclient import TestClient

from config import get_settings
from main import app

ORIGIN = get_settings().cors_origins[0]
METHODS = sorted({method.upper() for path in app.openapi()["paths"].values() for method in path})


@pytest.mark.parametrize("method", METHODS)
def test_every_route_method_passes_the_preflight(method: str):
    r = TestClient(app).options(
        "/banking/recurring/any",
        headers={"Origin": ORIGIN, "Access-Control-Request-Method": method},
    )
    assert r.status_code == 200, r.text
