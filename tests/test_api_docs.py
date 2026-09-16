"""The published API documentation stays true to the code.

docs/openapi.json is what Postman, n8n and client generators import, and
docs/API.md is what a person reads; both rot silently the moment an endpoint
changes. These fail instead, and say how to fix it."""
import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app import api_docs
from app.api import router as api_router
from app.main import app

ROOT = Path(__file__).resolve().parents[1]
REGEN = "uv run python -m app.api_docs > docs/openapi.json"


def test_committed_openapi_matches_the_code():
    committed = json.loads((ROOT / "docs" / "openapi.json").read_text())
    assert committed == json.loads(json.dumps(api_docs.schema())), \
        f"docs/openapi.json is out of date - run: {REGEN}"


def test_schema_describes_the_api_and_nothing_else():
    paths = api_docs.schema()["paths"]
    assert paths and all(p.startswith("/api/v1/") for p in paths)
    assert "/api/v1/openapi.json" not in paths
    routes = {r.path for r in api_router.routes}
    assert set(paths) == routes


def test_every_operation_is_documented_and_secured():
    for path, ops in api_docs.schema()["paths"].items():
        for method, op in ops.items():
            where = f"{method.upper()} {path}"
            assert op.get("summary") and op.get("description"), where
            assert op.get("tags"), where
            assert op.get("security") == [{"HTTPBearer": []}], where
            assert "200" in op["responses"] and "401" in op["responses"], where
            ok = op["responses"]["200"]["content"]["application/json"]["schema"]
            assert ok, f"{where} has no documented response body"


def test_the_written_reference_covers_every_endpoint():
    md = (ROOT / "docs" / "API.md").read_text()
    for path, ops in api_docs.schema()["paths"].items():
        short = path.removeprefix("/api/v1")
        for method in ops:
            assert re.search(rf"^### {method.upper()} {re.escape(short)}$", md, re.M), \
                f"docs/API.md has no section for {method.upper()} {short}"
    assert api_docs.VERSION.rsplit(".", 1)[0] in md.splitlines()[2]


def test_docs_are_served_and_the_app_wide_schema_is_not():
    c = TestClient(app)
    assert c.get("/openapi.json").status_code == 404
    spec = c.get("/api/v1/openapi.json")
    assert spec.status_code == 200 and spec.json()["info"]["title"] == "Scorecard API"
    page = c.get("/api/docs")
    assert page.status_code == 200 and "/api/v1/openapi.json" in page.text
