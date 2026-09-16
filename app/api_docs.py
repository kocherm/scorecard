"""The API's published documentation: an OpenAPI schema and a reference page.

The schema is built from the /api/v1 router ONLY. FastAPI's default
/openapi.json described every route in the app - login, admin forms, webhooks -
to anyone who asked, which is a map of the admin surface, not API docs; main.py
turns it off (openapi_url=None) and this serves the API part instead.

docs/openapi.json is the same schema committed to the repo, for tools that
import a file (Postman, n8n, client generators). tests/test_api_docs.py fails
when it drifts from the code; regenerate it with

    uv run python -m app.api_docs > docs/openapi.json
"""
from __future__ import annotations

import json

from fastapi import APIRouter
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse

from .api import router as api_router

VERSION = "1.1.0"

DESCRIPTION = """\
Read the company scorecard and write weekly numbers into it.

**Authentication.** Send an API token as `Authorization: Bearer sc_...`.
Tokens are created on Admin > API tokens and shown once. Scopes: `read`
(GET only), `write` and `read_write` (GET and writes), `admin` (everything,
including archiving).

**Weeks.** A week is identified by its Monday, `YYYY-MM-DD`, in the business
timezone. Writes default to the last closed week, the one that is due. Future
weeks and weeks before a metric started are refused.

**Writes replace.** Writing a week sets its value, replacing any previous
value, including one typed by hand. Re-sending is safe. Every write is
recorded in the audit trail against the token.

Full reference with examples: `docs/API.md` in the repository.
"""

router = APIRouter(include_in_schema=False)
_cache: dict = {}


def schema() -> dict:
    if "schema" not in _cache:
        spec = get_openapi(title="Scorecard API", version=VERSION,
                           description=DESCRIPTION, routes=api_router.routes)
        spec["tags"] = [
            {"name": "Scorecard", "description": "The scored board, as data."},
            {"name": "Metrics", "description": "List metrics and write weekly values."},
            {"name": "CEO metrics", "description": "The seven CEO tiles and their "
                                                   "category breakdowns."},
            {"name": "Admin", "description": "Structural changes. Admin tokens only."},
        ]
        _cache["schema"] = spec
    return _cache["schema"]


@router.get("/api/v1/openapi.json")
def openapi_json():
    return JSONResponse(schema())


@router.get("/api/docs", response_class=HTMLResponse)
def api_docs_page():
    return get_swagger_ui_html(openapi_url="/api/v1/openapi.json",
                               title="Scorecard API",
                               swagger_favicon_url="/static/favicon.ico")


if __name__ == "__main__":
    print(json.dumps(schema(), indent=2, sort_keys=True))
