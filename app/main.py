"""Aprendio Scorecard - FastAPI app: TV display, edit grid, admin, API, scheduler."""
from __future__ import annotations

import json
import logging
import mimetypes
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime, time as dt_time, timezone
from functools import partial, wraps
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import (BackgroundTasks, Body, Depends, FastAPI, Form,
                     HTTPException, Request, Response)
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from migrate import add_admin_scope, slack_two_way
from migrate import passkeys as passkeys_migration

from . import (alerts, channels, db as dbm, demo, entry_ops, grid as gridm,
               passkeys, readiness, weeks as wk)
from .api import router as api_router
from .mcp import router as mcp_router
from .inbound import router as inbound_router
from .slack import router as slack_router
from .auth import (RESET_LINK_HOURS, ROTATE_GRACE_DAYS, ROTATE_GRACE_MAX,
                   SESSION_COOKIE, complete_password_reset, consume_magic_link,
                   create_magic_link, create_reset_link,
                   create_session, destroy_session, hash_password, new_api_token,
                   rotate_api_token,
                   require_admin, require_editor, require_self, require_viewer,
                   session_hash, user_from_request, verify_password)
from .db import db_dep

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("scorecard")

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.filters["qlabel"] = lambda w: wk.quarter_label(
    w if isinstance(w, date) else wk.parse_week(w))
# Cache-buster: changes whenever any hand-written static asset does, so a
# browser never serves a stale scorecard.css or passkey.js after a deploy.
templates.env.globals["static_v"] = str(int(max(
    (BASE / "static" / f).stat().st_mtime
    for f in ("scorecard.css", "passkey.js"))))

scheduler = BackgroundScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    with dbm.get_db() as con:
        dbm.init_db(con)
        try:  # migration for DBs created before the is_key column existed
            con.execute("ALTER TABLE metrics ADD COLUMN is_key INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        # migration for DBs created before api_tokens.scope allowed 'admin'
        if add_admin_scope.needs_migration(con):
            add_admin_scope.migrate(con)
            log.info("Migrated api_tokens.scope to allow 'admin'")
        # additive columns for DBs created before view-as-user / channels
        for ddl in ("ALTER TABLE sessions ADD COLUMN "
                    "impersonate_user_id INTEGER REFERENCES users(id)",
                    "ALTER TABLE users ADD COLUMN notify_channel TEXT",
                    "ALTER TABLE users ADD COLUMN notify_address TEXT",
                    # token rotation (grace window + lineage)
                    "ALTER TABLE api_tokens ADD COLUMN expires_at TEXT",
                    "ALTER TABLE api_tokens ADD COLUMN "
                    "rotated_from_id INTEGER REFERENCES api_tokens(id)"):
            try:
                con.execute(ddl)
            except sqlite3.OperationalError:
                pass
        # migrations for DBs created before two-way Slack existed
        if slack_two_way.needs_entries_migration(con):
            slack_two_way.migrate_entries(con)
            log.info("Migrated entries.source to allow 'slack'")
        if slack_two_way.needs_alerts_migration(con):
            slack_two_way.migrate_alerts(con)
            log.info("Migrated alerts_sent.alert_type to allow nudges")
        # magic_links.purpose + users.webauthn_handle for DBs that predate
        # password-reset links and passkeys. init_db above already made the two
        # new tables (they are IF NOT EXISTS); columns need this.
        for line in passkeys_migration.migrate(con):
            log.info("Migration: %s", line)
        if dbm.get_setting(con, "display_token") is None:
            dbm.set_setting(con, "display_token", secrets.token_urlsafe(24))
    scheduler.add_job(alerts.stale_sweep, CronTrigger(
        day_of_week="wed", hour=8, minute=0, timezone="America/Chicago"),
        id="stale_sweep", replace_existing=True)
    scheduler.add_job(alerts.red_sweep, CronTrigger(
        day_of_week="tue", hour=8, minute=0, timezone="America/Chicago"),
        id="red_sweep", replace_existing=True)
    # Check-in nudge DMs. Always registered; enable/preset are checked inside
    # the job (same pattern as alerts_enabled) so settings changes need no
    # rescheduling. Monday 16:00 = "due tonight"; Tuesday 09:00 = last call
    # before the Wednesday 08:00 stale sweep.
    scheduler.add_job(partial(alerts.nudge_sweep, "nudge1"), CronTrigger(
        day_of_week="mon", hour=16, minute=0, timezone="America/Chicago"),
        id="nudge1", replace_existing=True)
    scheduler.add_job(partial(alerts.nudge_sweep, "nudge2"), CronTrigger(
        day_of_week="tue", hour=9, minute=0, timezone="America/Chicago"),
        id="nudge2", replace_existing=True)

    def prune_sessions():
        with dbm.get_db() as con:
            now_iso = datetime.now(timezone.utc).isoformat()
            con.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso,))
            con.execute("DELETE FROM magic_links WHERE expires_at < ?", (now_iso,))
            # Sweep history is a log, not state: the status page only ever reads
            # the latest run per kind, so old rows would grow forever unwatched.
            readiness.prune_sweep_runs(con)

    scheduler.add_job(prune_sessions, CronTrigger(
        day_of_week="sun", hour=3, minute=0, timezone="America/Chicago"),
        id="prune_sessions", replace_existing=True)
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Aprendio Scorecard", lifespan=lifespan, docs_url=None, redoc_url=None)
# Python's mimetypes table has no .webmanifest entry, so StaticFiles would serve
# the manifest as text/plain and Firefox would refuse to parse it.
mimetypes.add_type("application/manifest+json", ".webmanifest")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
app.include_router(api_router)
app.include_router(mcp_router)
app.include_router(slack_router)
app.include_router(inbound_router)


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    """Browsers and link unfurlers ask for /favicon.ico at the root whatever the
    <link> tags say; the TV kiosk logs a 404 on every cold boot otherwise."""
    return FileResponse(BASE / "static" / "favicon.ico")


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


# ---------------------------------------------------------------- demo mode
def data_db_dep(con: sqlite3.Connection = Depends(db_dep)):
    """Connection the board surfaces (grid, TV, cells, 1-3-1s) read AND write.
    Normally the real DB; the throwaway demo DB while 'Display Demo Data' is
    on. Auth, admin pages, alerts, and the JSON API always use db_dep - the
    toggle itself lives in the real DB, so real data can never be touched
    through a demo surface.

    Built ON TOP of db_dep so FastAPI's per-request dependency cache hands out
    the SAME real connection the auth guard already used. A second connection
    here would deadlock: the auth guard's last_seen_at update holds the write
    lock until its dependency teardown, which only runs after the response."""
    if dbm.get_setting(con, "display_demo_data", "0") != "1":
        yield con
        return
    months = dbm.get_setting(con, "display_months", "2")
    with demo.demo_db(datetime.now(timezone.utc), months) as dcon:
        yield dcon


def _real_actor(request: Request, user: sqlite3.Row) -> sqlite3.Row:
    """The account actually driving the browser: the impersonating admin when
    view-as is active, else the session user. Audit rows always name them."""
    return getattr(request.state, "impersonator", None) or user


def _data_actor_id(con: sqlite3.Connection, user: sqlite3.Row) -> Optional[int]:
    """Audit attribution that also works while writes land in the demo DB,
    whose users table differs from the real one the session user lives in."""
    if con.execute("SELECT 1 FROM users WHERE id = ?", (user["id"],)).fetchone():
        return user["id"]
    row = con.execute(
        "SELECT id FROM users ORDER BY role = 'admin' DESC, id LIMIT 1").fetchone()
    return row["id"] if row else None


@app.exception_handler(HTTPException)
async def redirect_handler(request: Request, exc: HTTPException):
    if exc.status_code == 303 and exc.headers and "Location" in exc.headers:
        return RedirectResponse(exc.headers["Location"], status_code=303)
    from fastapi.exception_handlers import http_exception_handler
    return await http_exception_handler(request, exc)


# ---------------------------------------------------------------- auth pages
def _login_render(request: Request, con: sqlite3.Connection,
                  error: Optional[str]) -> HTMLResponse:
    # The passkey button is hidden until at least one exists anywhere: on a
    # fresh instance it could only ever pop an empty authenticator prompt.
    return render(request, "login.html", error=error,
                  passkeys_on=passkeys.any_registered(con))


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, con: sqlite3.Connection = Depends(db_dep)):
    return _login_render(request, con, None)


# Simple in-memory login throttle: 5 failures per identity per 15 minutes.
_login_failures: dict[str, list[float]] = {}
_LOCKOUT_N, _LOCKOUT_WINDOW = 5, 900.0


def _throttled(key: str) -> bool:
    import time
    now = time.monotonic()
    hits = [t for t in _login_failures.get(key, []) if now - t < _LOCKOUT_WINDOW]
    _login_failures[key] = hits
    return len(hits) >= _LOCKOUT_N


def _record_failure(key: str) -> None:
    import time
    _login_failures.setdefault(key, []).append(time.monotonic())


def _clear_throttle(email: str) -> None:
    """Drop every login-failure bucket for this identity, across all IPs.

    Both reset paths must call this, or the recovery flow silently undoes
    itself. The person who resets a password is BY DEFINITION the person who
    just burned through the attempt limit - that is what "I forgot my
    password" looks like from the server - and _throttled runs before the
    password is ever verified, so the correct new password gets rejected
    exactly like the forgotten one. The symptom is maddening: passkey sign-in
    works (no throttle on that path) while the password you just set does not.

    Safe to clear: reaching either caller means control of the account was
    already proven - a reset token delivered to a private channel, or an
    admin. Keyed by IP and email, so every bucket for the email goes: the
    reset is often finished on a different device than the failed attempts."""
    suffix = f":{email.strip().lower()}"
    for k in [k for k in _login_failures if k.endswith(suffix)]:
        _login_failures.pop(k, None)


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...),
          con: sqlite3.Connection = Depends(db_dep)):
    key = f"{(request.client.host if request.client else '?')}:{email.strip().lower()}"
    if _throttled(key):
        return _login_render(request, con,
                             "Too many attempts. Wait 15 minutes and try again.")
    row = con.execute("SELECT * FROM users WHERE email = ? AND is_active = 1",
                      (email.strip(),)).fetchone()
    if row is None or not verify_password(password, row["password_hash"]):
        _record_failure(key)
        return _login_render(request, con, "Wrong email or password.")
    _login_failures.pop(key, None)
    token = create_session(con, row["id"])
    dest = "/account" if row["must_change_password"] else "/"
    # DRIs with numbers still missing land straight on the check-in page.
    # Suppressed in demo mode: /checkin would show demo data, and steering
    # someone there to "fix" real numbers would be a lie.
    if (dest == "/" and row["role"] != "viewer"
            and dbm.get_setting(con, "display_demo_data", "0") != "1"
            and entry_ops.missing_due_metrics(con, row["id"],
                                              datetime.now(timezone.utc))):
        dest = "/checkin"
    resp = RedirectResponse(dest, status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                    max_age=30 * 86400, secure=request.url.scheme == "https")
    return resp


@app.post("/logout")
def logout(request: Request, con: sqlite3.Connection = Depends(db_dep)):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        destroy_session(con, token)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# ------------------------------------------------------- forgotten passwords
PASSWORD_MIN = 10

# What every /forgot POST says, whatever happened. Saying "no such account"
# would turn the form into a list of who works here, and saying "sent!" only on
# success would say the same thing more quietly.
FORGOT_SENT = ("If that address has an account with a message channel set up, "
               f"a reset link is on its way. It expires in {RESET_LINK_HOURS} hours.")


def _secret_base_url(con: sqlite3.Connection) -> str:
    """Base URL for a message that carries a credential - the configured
    setting ONLY, never request.base_url.

    Everywhere else, falling back to the request's own address is a
    convenience. Here it would be host-header injection: the app also binds
    the LAN directly, so anyone who can reach it can send a request with a
    Host of their choosing and have the reset link in someone's DM point at
    their server. An unset public base URL means no reset link goes out, which
    Admin > Setup & status reports rather than leaving to a log line."""
    return (dbm.get_setting(con, "public_base_url") or "").rstrip("/")


def _send_reset_link(con: sqlite3.Connection,
                     target: sqlite3.Row) -> tuple[bool, str]:
    """Mint a reset link and deliver it. Returns (ok, channel-or-reason).

    Delivery is Slack DM / Telegram / SMS, never Teams or Google Chat: those
    are shared-space webhooks, so a link posted there is a link the whole team
    can use (channels.deliver_secret). One helper for both callers - the
    self-serve /forgot page and the admin button - so there is one message and
    one set of preconditions rather than two that drift."""
    if not channels.deliver_secret(con, target):
        return False, ("has no private message channel set up, so a reset link "
                       "cannot be delivered")
    base = _secret_base_url(con)
    if not base:
        # No public base URL means no clickable link to send. Covered by the
        # reset_delivery readiness check; the caller says so out loud.
        return False, "cannot be sent until the public base URL is set in Settings"
    token = create_reset_link(con, target["id"])
    ch = channels.user_channel(target)
    url = f"{base}/reset?t={token}"
    text = (f"Password reset for the Company Scorecard. "
            f"{channels.link(ch, url, 'Set a new password')}\n"
            f"The link works for {RESET_LINK_HOURS} hours. "
            f"If you did not ask for this, ignore it - "
            f"your current password still works.")
    if not alerts.send_direct(con, target, text):
        return False, f"{channels.LABELS[ch]} would not accept the message"
    return True, ch


def _deliver_reset(user_id: int) -> None:
    """Background half of /forgot. Opens its own connection: the request's is
    closed by dependency teardown before background tasks run (same reason
    slack.handle_dm does)."""
    with dbm.get_db() as con:
        target = con.execute("SELECT * FROM users WHERE id = ? AND is_active = 1",
                             (user_id,)).fetchone()
        if target is None:
            return
        ok, why = _send_reset_link(con, target)
        if not ok:
            log.warning("password reset for user %s not sent: %s", user_id, why)


@app.get("/forgot", response_class=HTMLResponse)
def forgot_page(request: Request):
    return render(request, "forgot.html", error=None, sent=None)


@app.post("/forgot", response_class=HTMLResponse)
def forgot_send(request: Request, background: BackgroundTasks,
                email: str = Form(...),
                con: sqlite3.Connection = Depends(db_dep)):
    """Ask for a reset link. Answers the same way whoever you ask about.

    The send is deferred to a background task and nothing about it is awaited,
    because delivery is an HTTP call to Slack/Telegram/Twilio: doing it inline
    made a known address answer hundreds of milliseconds slower than an unknown
    one, so the identical wording said nothing and the latency said everything.
    What is left in the request is one indexed SELECT, taken on every path.

    A user without a private channel gets no message and the same reply - the
    admin temp-password path on Admin > Users is still their way back in."""
    key = f"forgot:{(request.client.host if request.client else '?')}:{email.strip().lower()}"
    if _throttled(key):
        return render(request, "forgot.html", sent=None,
                      error="Too many requests. Wait 15 minutes and try again.")
    _record_failure(key)  # every attempt counts, not just the ones that miss

    row = con.execute("SELECT id FROM users WHERE email = ? AND is_active = 1",
                      (email.strip(),)).fetchone()
    if row is not None:
        background.add_task(_deliver_reset, row["id"])
    return render(request, "forgot.html", error=None, sent=FORGOT_SENT)


# The reset token, moved out of the URL on arrival. Same exchange the /checkin
# magic link does: the token has to travel in a link, but it does not have to
# stay in the address bar, the browser history, or every proxy access-log line
# for the page. Path-scoped so it is not attached to any other request.
RESET_COOKIE = "scorecard_reset"


def _reset_cookie(resp: Response, request: Request, token: str) -> None:
    resp.set_cookie(RESET_COOKIE, token, httponly=True, samesite="lax",
                    path="/reset", max_age=RESET_LINK_HOURS * 3600,
                    secure=request.url.scheme == "https")


@app.get("/reset", response_class=HTMLResponse)
def reset_page(request: Request, t: str = "",
               con: sqlite3.Connection = Depends(db_dep)):
    """Arrive with ?t=, continue without it.

    A valid token is stashed in a path-scoped cookie and the browser is sent to
    a clean /reset. The link stays multi-use until it expires, so Slack
    unfurling the URL before the human clicks it still cannot burn it."""
    if t:
        if consume_magic_link(con, t, purpose="reset") is None:
            return render(request, "reset.html", valid=False, error=None,
                          min_len=PASSWORD_MIN)
        resp = RedirectResponse("/reset", status_code=303)
        _reset_cookie(resp, request, t)
        return resp
    token = request.cookies.get(RESET_COOKIE, "")
    valid = bool(token) and consume_magic_link(con, token, purpose="reset") is not None
    return render(request, "reset.html", valid=valid, error=None,
                  min_len=PASSWORD_MIN)


@app.post("/reset", response_class=HTMLResponse)
def reset_submit(request: Request, new: str = Form(...),
                 con: sqlite3.Connection = Depends(db_dep)):
    uid = consume_magic_link(con, request.cookies.get(RESET_COOKIE, ""),
                             purpose="reset")
    if uid is None:
        return render(request, "reset.html", valid=False, min_len=PASSWORD_MIN,
                      error="That link has expired. Request a new one.")
    if len(new) < PASSWORD_MIN:
        return render(request, "reset.html", valid=True, min_len=PASSWORD_MIN,
                      error=f"Password must be {PASSWORD_MIN}+ characters.")
    complete_password_reset(con, uid, new)
    who = con.execute("SELECT email FROM users WHERE id = ?", (uid,)).fetchone()
    if who is not None:
        _clear_throttle(who["email"])
    # Signed in on the new password, after every other session was dropped.
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(RESET_COOKIE, path="/reset")
    resp.set_cookie(SESSION_COOKIE, create_session(con, uid), httponly=True,
                    samesite="lax", max_age=30 * 86400,
                    secure=request.url.scheme == "https")
    return resp


# ----------------------------------------------------------------- passkeys
def _passkey_json(fn):
    """Ceremony endpoints answer JSON, including on failure: the caller is
    fetch() inside passkey.js, which needs a message it can show rather than an
    HTML error page it would have to ignore. A handler that already built its
    own Response (to set the session cookie) is passed straight through.

    Sync, like every other route here, and the body arrives through Body()
    rather than `await request.json()`. That is not a style choice: db_dep
    hands out a plain sqlite3 connection, which may only be used on the thread
    that opened it. An async endpoint would run on the event loop while its
    dependency ran in the threadpool, and every query would raise."""
    @wraps(fn)   # keeps the signature FastAPI reads to build the dependencies
    def wrapped(*a, **kw):
        try:
            out = fn(*a, **kw)
        except passkeys.PasskeyError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return out if isinstance(out, Response) else JSONResponse(out)

    return wrapped


@app.post("/login/passkey/begin")
@_passkey_json
def passkey_login_begin(request: Request,
                        con: sqlite3.Connection = Depends(db_dep)):
    return json.loads(passkeys.begin_login(con, request))


@app.post("/login/passkey/finish")
@_passkey_json
def passkey_login_finish(request: Request, body: dict = Body(...),
                         con: sqlite3.Connection = Depends(db_dep)):
    uid = passkeys.finish_login(con, request, body)
    token = create_session(con, uid)
    row = con.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    resp = JSONResponse({"ok": True,
                         "next": "/account" if row["must_change_password"] else "/"})
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                    max_age=30 * 86400, secure=request.url.scheme == "https")
    return resp


@app.post("/account/passkeys/begin")
@_passkey_json
def passkey_register_begin(request: Request, user=Depends(require_self),
                           con: sqlite3.Connection = Depends(db_dep)):
    return json.loads(passkeys.begin_registration(con, request, user))


@app.post("/account/passkeys/finish")
@_passkey_json
def passkey_register_finish(request: Request, body: dict = Body(...),
                            user=Depends(require_self),
                            con: sqlite3.Connection = Depends(db_dep)):
    name = passkeys.finish_registration(con, request, user,
                                        body.get("credential") or {},
                                        body.get("name") or "")
    return {"ok": True, "name": name}


@app.post("/account/passkeys/{pk}/delete")
def passkey_delete(pk: int, user=Depends(require_self),
                   con: sqlite3.Connection = Depends(db_dep)):
    passkeys.delete_credential(con, user["id"], pk)
    return RedirectResponse("/account", status_code=303)


def _account_render(request: Request, con: sqlite3.Connection, user, **ctx):
    return render(request, "account.html", user=user, active="",
                  min_len=PASSWORD_MIN,
                  keys=passkeys.credentials_for(con, user["id"]), **ctx)


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request, user=Depends(require_viewer),
                 con: sqlite3.Connection = Depends(db_dep)):
    return _account_render(request, con, user)


@app.post("/account/password")
def change_password(request: Request, current: str = Form(...), new: str = Form(...),
                    user=Depends(require_viewer),
                    con: sqlite3.Connection = Depends(db_dep)):
    if not verify_password(current, user["password_hash"]):
        return _account_render(request, con, user,
                               flash="Current password is wrong.", flash_kind="err")
    if len(new) < PASSWORD_MIN:
        return _account_render(
            request, con, user, flash_kind="err",
            flash=f"New password must be {PASSWORD_MIN}+ characters.")
    con.execute("UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
                (hash_password(new), user["id"]))
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------- scorecard
@app.get("/", response_class=HTMLResponse)
def grid_page(request: Request, user=Depends(require_viewer),
              con: sqlite3.Connection = Depends(data_db_dep),
              real: sqlite3.Connection = Depends(db_dep)):
    vm = gridm.build_grid(con, datetime.now(timezone.utc))
    return render(request, "grid.html", user=user, vm=vm, active="grid",
                  can_edit=user["role"] in ("editor", "admin"),
                  demo_on=dbm.get_setting(real, "display_demo_data", "0") == "1",
                  display_token=dbm.get_setting(real, "display_token"))


def _metric_or_404(con: sqlite3.Connection, metric_id: int) -> sqlite3.Row:
    m = con.execute("SELECT * FROM metrics WHERE id = ?", (metric_id,)).fetchone()
    if m is None:
        raise HTTPException(404)
    return m


def _render_cell(request: Request, con: sqlite3.Connection, metric_id: int,
                 week: date) -> HTMLResponse:
    """Re-render a single cell after an edit (htmx swap)."""
    vm = gridm.build_grid(con, datetime.now(timezone.utc))
    for s in vm.sections:
        for r in s.rows:
            if r.metric_id == metric_id:
                for c in r.cells:
                    if c.week == week:
                        html = templates.env.from_string(
                            '{% from "_cell.html" import cell_td %}'
                            '{{ cell_td(row, cell, true, last_closed) }}'
                        ).render(row=r, cell=c, last_closed=vm.last_closed)
                        return HTMLResponse(html)
    raise HTTPException(404)


@app.get("/cell/{metric_id}/{week}/edit", response_class=HTMLResponse)
def cell_edit_form(metric_id: int, week: str, request: Request,
                   user=Depends(require_editor),
                   con: sqlite3.Connection = Depends(data_db_dep)):
    m = _metric_or_404(con, metric_id)
    w = wk.parse_week(week)
    e = con.execute("SELECT * FROM entries WHERE metric_id=? AND week_start=?",
                    (metric_id, week)).fetchone()
    current = None
    if e:
        current = e["value_status"] if m["metric_type"] == "status" else e["value_numeric"]
    return render(request, "_cell_form.html", metric=m, week=week, current=current)


@app.post("/cell/{metric_id}/{week}", response_class=HTMLResponse)
def cell_save(metric_id: int, week: str, request: Request, value: str = Form(...),
              user=Depends(require_editor),
              con: sqlite3.Connection = Depends(data_db_dep)):
    m = _metric_or_404(con, metric_id)
    w = wk.parse_week(week)
    if w > wk.current_week(datetime.now(timezone.utc)):
        raise HTTPException(422, "Future week")
    actor = _data_actor_id(con, _real_actor(request, user))
    try:
        entry_ops.save_value(con, m, w, value, source="manual", user_id=actor)
    except ValueError as e:
        raise HTTPException(422, str(e))
    con.commit()
    return _render_cell(request, con, metric_id, w)


# ---------------------------------------------------------------- my numbers
def _checkin_items(con: sqlite3.Connection, uid: Optional[int], now: datetime):
    """The (effective) user's owned metrics as check-in cards: due-week cell,
    current-week cell, plus the earlier weeks of the display window (newest
    first) for catching up on gaps or correcting numbers after the fact.
    Missing-first ordering; every save is audited like any other write."""
    vm = gridm.build_grid(con, now)
    items = []
    for s in vm.sections:
        for r in s.rows:
            if r.dri_user_id != uid:
                continue
            due = next((c for c in r.cells if c.week == vm.last_closed), None)
            cur = next((c for c in r.cells if c.week == vm.current_week), None)
            earlier = [c for c in r.cells
                       if c.week < vm.last_closed and c.editable]
            earlier.reverse()
            items.append({
                "row": r, "section": s.name, "due": due, "cur": cur,
                "due_missing": bool(due and due.raw is None and due.editable),
                "earlier": earlier,
                "earlier_missing": sum(1 for c in earlier if c.raw is None),
            })
    items.sort(key=lambda i: (not i["due_missing"], not i["earlier_missing"]))
    return vm, items


@app.get("/checkin", response_class=HTMLResponse)
def checkin_page(request: Request, t: str = "",
                 con: sqlite3.Connection = Depends(data_db_dep),
                 real: sqlite3.Connection = Depends(db_dep)):
    """One focused page: enter your own numbers. Reached from the nav, the
    post-login redirect, or a Slack magic link (?t=) that signs the DRI in."""
    user = user_from_request(request, real)
    if user is None:
        if t:
            uid = consume_magic_link(real, t)
            if uid is not None:
                token = create_session(real, uid)
                resp = RedirectResponse("/checkin", status_code=303)  # clean URL
                resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                                max_age=30 * 86400,
                                secure=request.url.scheme == "https")
                return resp
        return RedirectResponse("/login", status_code=303)
    if user["role"] == "viewer":
        raise HTTPException(403, "Viewers have no numbers to enter")
    now = datetime.now(timezone.utc)
    vm, items = _checkin_items(con, _data_actor_id(con, user), now)
    return render(request, "checkin.html", user=user, vm=vm, items=items,
                  active="checkin",
                  missing=sum(1 for i in items if i["due_missing"]),
                  demo_on=dbm.get_setting(real, "display_demo_data", "0") == "1")


@app.post("/checkin/{metric_id}/{week}", response_class=HTMLResponse)
def checkin_save(metric_id: int, week: str, request: Request, value: str = Form(""),
                 user=Depends(require_editor),
                 con: sqlite3.Connection = Depends(data_db_dep)):
    m = _metric_or_404(con, metric_id)
    w = wk.parse_week(week)
    now = datetime.now(timezone.utc)
    if w > wk.current_week(now):
        raise HTTPException(422, "Future week")
    actor = _data_actor_id(con, _real_actor(request, user))
    try:
        entry_ops.save_value(con, m, w, value, source="manual", user_id=actor)
    except ValueError as e:
        raise HTTPException(422, str(e))
    con.commit()
    vm, items = _checkin_items(con, _data_actor_id(con, user), now)
    item = next((i for i in items if i["row"].metric_id == metric_id), None)
    if item is None:
        raise HTTPException(404)
    # Keep the earlier-weeks section open when that's where they just saved,
    # so multi-week catch-up doesn't collapse the section between edits.
    html = templates.env.from_string(
        '{% from "_checkin_row.html" import checkin_card %}'
        '{{ checkin_card(item, vm, open_earlier) }}').render(
        item=item, vm=vm, open_earlier=w < vm.last_closed)
    return HTMLResponse(html)


# ---------------------------------------------------------------- 1-3-1
@app.get("/131/{metric_id}/{week}", response_class=HTMLResponse)
def one_three_one_page(metric_id: int, week: str, request: Request,
                       user=Depends(require_editor),
                       con: sqlite3.Connection = Depends(data_db_dep)):
    m = _metric_or_404(con, metric_id)
    w = wk.parse_week(week)
    dri = con.execute(
        "SELECT display_name FROM users WHERE id = ?", (m["dri_user_id"],)
    ).fetchone() if m["dri_user_id"] else None
    existing = con.execute(
        """SELECT o.*, u.display_name AS author FROM one_three_ones o
           JOIN users u ON u.id = o.created_by
           WHERE o.metric_id = ? AND o.week_start = ?""",
        (metric_id, week)).fetchone()
    return render(request, "onethreeone.html", user=user, active="grid",
                  metric=m, dri_name=dri["display_name"] if dri else None,
                  week=week, week_date=w, existing=existing,
                  existing_options=json.loads(existing["options_json"]) if existing else [])


@app.post("/131/{metric_id}/{week}")
def one_three_one_save(metric_id: int, week: str, request: Request,
                       problem: str = Form(...), option1: str = Form(...),
                       option2: str = Form(...), option3: str = Form(...),
                       recommendation: str = Form(...),
                       user=Depends(require_editor),
                       con: sqlite3.Connection = Depends(data_db_dep)):
    _metric_or_404(con, metric_id)
    wk.parse_week(week)
    con.execute(
        """INSERT OR IGNORE INTO one_three_ones
           (metric_id, week_start, problem, options_json, recommendation, created_by)
           VALUES (?,?,?,?,?,?)""",
        (metric_id, week, problem, json.dumps([option1, option2, option3]),
         recommendation, _data_actor_id(con, _real_actor(request, user))))
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------- TV display
def _check_display_token(con: sqlite3.Connection, token: str) -> None:
    if not token or token != dbm.get_setting(con, "display_token"):
        raise HTTPException(403, "Bad display token")


@app.get("/tv")
def tv_shortcut(con: sqlite3.Connection = Depends(db_dep)):
    """Typeable shortcut for a TV/kiosk browser: looks up the current display
    token server-side and 302-redirects to /display. 302 (not 301/308) so the
    redirect is never cached and keeps working after the token is rotated."""
    token = dbm.get_setting(con, "display_token") or ""
    return RedirectResponse(f"/display?token={token}", status_code=302)


def _tv_context(con: sqlite3.Connection):
    now = datetime.now(timezone.utc)
    return (gridm.build_tv(con, now),
            now.astimezone(wk.BUSINESS_TZ).strftime("%-I:%M %p"))


def _screensaver_active(con: sqlite3.Connection, now: datetime) -> bool:
    """Settings live in the REAL db (`con` must not be the demo copy)."""
    if dbm.get_setting(con, "screensaver_enabled", "0") != "1":
        return False
    return wk.in_nightly_window(
        now,
        dbm.get_setting(con, "screensaver_start", "21:00") or "",
        dbm.get_setting(con, "screensaver_end", "06:00") or "")


# ---- switchable TV views
# Rebuilt server-side after the five client-switched views were removed in
# 71944be. The board stays the default and the others are built from the SAME
# TvVM that gridm.build_tv already returns - a view is a different arrangement
# of the one board, never a second query path.
TV_VIEWS = ("board", "act", "key")
TV_VIEW_LABELS = {"board": "Full board",
                  "act": "Act on this",
                  "key": "Key metrics"}
TV_ROTATE_DEFAULT = 45
# The poll is the only thing that advances a rotation, so a period shorter than
# it would just be the poll interval with extra steps.
TV_ROTATE_MIN = 10


def _enabled_views(con: sqlite3.Connection) -> list[str]:
    raw = dbm.get_setting(con, "display_views", "board") or ""
    views = [v.strip() for v in raw.split(",") if v.strip() in TV_VIEWS]
    return views or ["board"]


def _view_has_content(view: str, tv) -> bool:
    """Whether a view has anything to say right now.

    Rotation skips the ones that do not. An unattended TV showing an empty
    panel for 45 seconds reads as broken, and 'Act on this' is empty exactly
    when the company is doing well - the best case must not look like a
    fault."""
    if view == "act":
        return bool(tv.actions)
    if view == "key":
        return any(r.is_key for col in tv.columns for sec in col for r in sec.rows)
    return True


def _tv_view(con: sqlite3.Connection, tv, now: datetime, override: str = "") -> str:
    """The view this render shows: an explicit ?view= if it is enabled and has
    content, otherwise the clock-driven rotation over the enabled views."""
    enabled = _enabled_views(con)
    if override in enabled and _view_has_content(override, tv):
        return override
    live = [v for v in enabled if _view_has_content(v, tv)]
    seconds = _rotate_seconds(con)
    return wk.rotation_pick(now, live or ["board"], seconds) or "board"


def _rotate_seconds(con: sqlite3.Connection) -> int:
    try:
        n = int(dbm.get_setting(con, "display_rotate_seconds",
                                str(TV_ROTATE_DEFAULT)) or 0)
    except ValueError:
        return TV_ROTATE_DEFAULT
    return 0 if n <= 0 else max(TV_ROTATE_MIN, n)


def _sleep_context(now: datetime) -> dict:
    local = now.astimezone(wk.BUSINESS_TZ)
    mins = local.hour * 60 + local.minute
    # A new spot every minute (the TV polls more often than that; the position
    # is a function of the minute, so it moves once a minute regardless), on two
    # different prime cycles so the pair doesn't retrace itself within a night.
    # 0-100 is safe at any font size: the clock offsets itself by its own
    # width/height, so it never hangs off an edge - see .tv-sleep-clock.
    return {"rendered_at": local.strftime("%-I:%M %p"),
            "top": (mins * 37) % 101, "left": (mins * 53) % 97}


@app.get("/display", response_class=HTMLResponse)
def display_page(request: Request, token: str = "", view: str = "",
                 real: sqlite3.Connection = Depends(db_dep),
                 con: sqlite3.Connection = Depends(data_db_dep)):
    _check_display_token(real, token)
    now = datetime.now(timezone.utc)
    if _screensaver_active(real, now):
        return render(request, "display.html", tv=None, token=token,
                      pinned="", sleep=_sleep_context(now))
    tv, rendered_at = _tv_context(con)
    # `pinned` rides into the poll URL so a human who chose a view keeps it;
    # the TV passes nothing and therefore rotates.
    return render(request, "display.html", tv=tv, token=token,
                  view=_tv_view(real, tv, now, view), pinned=view,
                  rendered_at=rendered_at)


@app.get("/display/body", response_class=HTMLResponse)
def display_body(request: Request, token: str = "", view: str = "",
                 real: sqlite3.Connection = Depends(db_dep),
                 con: sqlite3.Connection = Depends(data_db_dep)):
    _check_display_token(real, token)
    now = datetime.now(timezone.utc)
    if _screensaver_active(real, now):
        html = templates.env.get_template("_display_sleep.html").render(
            sleep=_sleep_context(now))
    else:
        tv, rendered_at = _tv_context(con)
        html = templates.env.get_template("_display_body.html").render(
            tv=tv, rendered_at=rendered_at,
            view=_tv_view(real, tv, now, view))
    return HTMLResponse(f'<div class="board" id="tvroot">{html}</div>')


# ---------------------------------------------------------------- admin
@app.get("/admin", response_class=HTMLResponse)
def admin_root(user=Depends(require_admin)):
    return RedirectResponse("/admin/metrics", status_code=303)


@app.get("/admin/metrics", response_class=HTMLResponse)
def admin_metrics(request: Request, user=Depends(require_admin),
                  con: sqlite3.Connection = Depends(db_dep)):
    sections = []
    for s in con.execute("SELECT * FROM sections ORDER BY sort_order, id"):
        metrics = con.execute(
            """SELECT m.*, u.display_name AS dri_name FROM metrics m
               LEFT JOIN users u ON u.id = m.dri_user_id
               WHERE m.section_id = ? ORDER BY m.sort_order, m.id""", (s["id"],)).fetchall()
        sections.append({**dict(s), "metrics": metrics})
    users = con.execute("SELECT * FROM users WHERE is_active = 1 ORDER BY display_name").fetchall()
    return render(request, "admin_metrics.html", user=user, active="metrics",
                  sections=sections, users=users)


@app.post("/admin/sections")
def add_section(name: str = Form(...), icon: str = Form("chart"),
                user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    mx = con.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM sections").fetchone()["n"]
    con.execute("INSERT INTO sections (name, icon, sort_order) VALUES (?,?,?)", (name, icon, mx))
    return RedirectResponse("/admin/metrics", status_code=303)


@app.post("/admin/sections/{section_id}/toggle")
def toggle_section(section_id: int, user=Depends(require_admin),
                   con: sqlite3.Connection = Depends(db_dep)):
    con.execute("UPDATE sections SET is_enabled = 1 - is_enabled WHERE id = ?", (section_id,))
    return RedirectResponse("/admin/metrics", status_code=303)


@app.post("/admin/metrics")
def add_metric(section_id: int = Form(...), name: str = Form(...),
               metric_type: str = Form(...), rollup: str = Form("sum"),
               unit: str = Form(""), direction: str = Form("up"),
               dri_user_id: str = Form(""),
               user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    start = wk.current_week(datetime.now(timezone.utc))
    mx = con.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM metrics WHERE section_id = ?",
                     (section_id,)).fetchone()["n"]
    con.execute(
        """INSERT INTO metrics (section_id, name, metric_type, rollup, direction, unit,
                                dri_user_id, start_week, sort_order)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (section_id, name, metric_type,
         rollup if metric_type == "numeric" else None,
         direction, unit or None,
         int(dri_user_id) if dri_user_id else None, start.isoformat(), mx))
    return RedirectResponse("/admin/metrics", status_code=303)


@app.get("/admin/metrics/{metric_id}", response_class=HTMLResponse)
def edit_metric_page(metric_id: int, request: Request, user=Depends(require_admin),
                     con: sqlite3.Connection = Depends(db_dep)):
    m = _metric_or_404(con, metric_id)
    section = con.execute("SELECT * FROM sections WHERE id = ?", (m["section_id"],)).fetchone()
    sections = con.execute("SELECT * FROM sections ORDER BY sort_order").fetchall()
    users = con.execute("SELECT * FROM users WHERE is_active = 1 ORDER BY display_name").fetchall()
    return render(request, "admin_metric_edit.html", user=user, active="metrics",
                  m=m, section=section, sections=sections, users=users)


@app.post("/admin/metrics/{metric_id}")
def edit_metric(metric_id: int, section_id: int = Form(...), name: str = Form(...),
                metric_type: str = Form(...), rollup: str = Form("sum"),
                unit: str = Form(""), direction: str = Form("up"),
                dri_user_id: str = Form(""), start_week: str = Form(...),
                sort_order: int = Form(0), is_key: str = Form(""),
                user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    _metric_or_404(con, metric_id)
    start = wk.monday_of(date.fromisoformat(start_week)).isoformat()
    con.execute(
        """UPDATE metrics SET section_id=?, name=?, metric_type=?, rollup=?, direction=?,
                              unit=?, dri_user_id=?, start_week=?, sort_order=?, is_key=?
           WHERE id=?""",
        (section_id, name, metric_type,
         rollup if metric_type == "numeric" else None,
         direction, unit or None,
         int(dri_user_id) if dri_user_id else None, start, sort_order,
         1 if is_key else 0, metric_id))
    return RedirectResponse("/admin/metrics", status_code=303)


@app.post("/admin/metrics/{metric_id}/archive")
def archive_metric(metric_id: int, user=Depends(require_admin),
                   con: sqlite3.Connection = Depends(db_dep)):
    con.execute("UPDATE metrics SET archived_at = datetime('now') WHERE id = ?", (metric_id,))
    return RedirectResponse("/admin/metrics", status_code=303)


@app.post("/admin/metrics/{metric_id}/unarchive")
def unarchive_metric(metric_id: int, user=Depends(require_admin),
                     con: sqlite3.Connection = Depends(db_dep)):
    con.execute("UPDATE metrics SET archived_at = NULL WHERE id = ?", (metric_id,))
    return RedirectResponse("/admin/metrics", status_code=303)


# ---------------- targets
@app.get("/admin/targets", response_class=HTMLResponse)
def admin_targets(request: Request, year: Optional[int] = None, quarter: Optional[int] = None,
                  user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    now_w = wk.current_week(datetime.now(timezone.utc))
    y, q = wk.quarter_of(now_w)
    year = year or y
    quarter = quarter or q
    metrics = con.execute(
        """SELECT m.*, s.name AS section_name, u.display_name AS dri_name
           FROM metrics m
           JOIN sections s ON s.id = m.section_id
           LEFT JOIN users u ON u.id = m.dri_user_id
           WHERE m.archived_at IS NULL AND m.metric_type = 'numeric'
           ORDER BY s.sort_order, m.sort_order""").fetchall()
    rows = []
    missing = 0
    for m in metrics:
        t = con.execute("SELECT * FROM targets WHERE metric_id=? AND year=? AND quarter=?",
                        (m["id"], year, quarter)).fetchone()
        if t is None:
            missing += 1
        rows.append(type("R", (), {"m": m, "t": t})())
    return render(request, "admin_targets.html", user=user, active="targets",
                  rows=rows, year=year, quarter=quarter,
                  missing=missing if missing else None)


@app.post("/admin/targets/{metric_id}")
def save_target(metric_id: int, year: int, quarter: int,
                baseline: float = Form(...), stretch: float = Form(...),
                user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    con.execute(
        """INSERT INTO targets (metric_id, year, quarter, baseline_value, stretch_value)
           VALUES (?,?,?,?,?)
           ON CONFLICT(metric_id, year, quarter) DO UPDATE SET
             baseline_value = excluded.baseline_value,
             stretch_value = excluded.stretch_value""",
        (metric_id, year, quarter, baseline, stretch))
    return RedirectResponse(f"/admin/targets?year={year}&quarter={quarter}", status_code=303)


# ---------------- users
def _users_page(request: Request, con: sqlite3.Connection, user, **ctx):
    users = con.execute("SELECT * FROM users ORDER BY display_name").fetchall()
    # Which rows may be offered "Send reset link": a private channel, configured.
    # Computed here rather than in the template so the button cannot appear for
    # a Teams/Google Chat user, whose "DM" is the whole team's channel.
    for k in ("temp_password", "temp_user", "flash"):
        ctx.setdefault(k, None)
    return render(request, "admin_users.html", user=user, active="users",
                  users=users, reachable={u["id"] for u in users
                                          if channels.deliver_secret(con, u)},
                  **ctx)


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, user=Depends(require_admin),
                con: sqlite3.Connection = Depends(db_dep)):
    return _users_page(request, con, user)


def _temp_password() -> str:
    return secrets.token_urlsafe(9)


@app.post("/admin/users", response_class=HTMLResponse)
def add_user(request: Request, display_name: str = Form(...), email: str = Form(...),
             role: str = Form("editor"),
             user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    pw = _temp_password()
    try:
        con.execute(
            """INSERT INTO users (email, password_hash, display_name, role, must_change_password)
               VALUES (?,?,?,?,1)""",
            (email.strip(), hash_password(pw), display_name.strip(), role))
    except sqlite3.IntegrityError:
        return _users_page(request, con, user,
                           flash=f"{email} already exists.", flash_kind="err")
    return _users_page(request, con, user,
                       temp_password=pw, temp_user=display_name)


@app.post("/admin/users/{uid}/role")
def set_role(uid: int, role: str = Form(...), user=Depends(require_admin),
             con: sqlite3.Connection = Depends(db_dep)):
    if uid == user["id"]:
        raise HTTPException(400, "Cannot change your own role")
    con.execute("UPDATE users SET role = ? WHERE id = ?", (role, uid))
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{uid}/notify")
def set_notify(uid: int, notify_channel: str = Form("slack"), address: str = Form(""),
               user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    """One control per user: which channel nudges use and its address.
    Slack keeps its dedicated column; the rest share notify_address
    (Teams/Google Chat post to a shared webhook, so no address needed)."""
    if notify_channel not in channels.CHANNELS:
        raise HTTPException(422, "Unknown channel")
    addr = address.strip() or None
    if notify_channel == "slack":
        con.execute("UPDATE users SET notify_channel = 'slack', slack_member_id = ? "
                    "WHERE id = ?", (addr, uid))
    else:
        con.execute("UPDATE users SET notify_channel = ?, notify_address = ? "
                    "WHERE id = ?", (notify_channel, addr, uid))
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{uid}/toggle")
def toggle_user(uid: int, user=Depends(require_admin),
                con: sqlite3.Connection = Depends(db_dep)):
    if uid == user["id"]:
        raise HTTPException(400, "Cannot deactivate yourself")
    con.execute("UPDATE users SET is_active = 1 - is_active WHERE id = ?", (uid,))
    con.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{uid}/impersonate")
def impersonate_start(uid: int, request: Request, user=Depends(require_admin),
                      con: sqlite3.Connection = Depends(db_dep)):
    """View as user: this session renders as the target until exited. The
    session row keeps the admin as user_id; audit stays on the real admin."""
    target = con.execute(
        "SELECT id FROM users WHERE id = ? AND is_active = 1", (uid,)).fetchone()
    if target is None:
        raise HTTPException(404)
    con.execute("UPDATE sessions SET impersonate_user_id = ? WHERE token_hash = ?",
                (uid, session_hash(request)))
    return RedirectResponse("/", status_code=303)


@app.post("/impersonate/stop")
def impersonate_stop(request: Request, con: sqlite3.Connection = Depends(db_dep)):
    # No role guard: the effective user may be a viewer; the real admin must
    # always be able to exit. Clearing on a non-impersonating session is a no-op.
    con.execute("UPDATE sessions SET impersonate_user_id = NULL WHERE token_hash = ?",
                (session_hash(request),))
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{uid}/reset", response_class=HTMLResponse)
def reset_password(uid: int, request: Request, user=Depends(require_admin),
                   con: sqlite3.Connection = Depends(db_dep)):
    """Temp password read out loud. The fallback, kept because it is the only
    path that works for someone with no private channel - or when Slack is the
    thing that is broken."""
    target = con.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if target is None:
        raise HTTPException(404)
    pw = _temp_password()
    con.execute("UPDATE users SET password_hash = ?, must_change_password = 1 WHERE id = ?",
                (hash_password(pw), uid))
    con.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
    _clear_throttle(target["email"])   # or the temp password is refused too
    return _users_page(request, con, user,
                       temp_password=pw, temp_user=target["display_name"])


@app.post("/admin/users/{uid}/reset-link", response_class=HTMLResponse)
def send_reset_link(uid: int, request: Request, user=Depends(require_admin),
                    con: sqlite3.Connection = Depends(db_dep)):
    """DM the user a reset link instead of minting a password to read out.

    Unlike the temp-password path this does NOT change the password or drop
    sessions: nothing has happened yet, and invalidating a working password on
    the strength of a message that may not arrive would be a lockout, not a
    reset. The user's own click is what changes anything."""
    target = con.execute("SELECT * FROM users WHERE id = ? AND is_active = 1",
                         (uid,)).fetchone()
    if target is None:
        raise HTTPException(404)
    # Synchronous, unlike /forgot: this route is behind require_admin, so it is
    # nobody's enumeration oracle, and the admin needs to be told whether it
    # actually went out.
    ok, detail = _send_reset_link(con, target)
    if not ok:
        return _users_page(
            request, con, user, flash_kind="err",
            flash=f"{target['display_name']} {detail}. Use Reset password "
                  "instead, or check Admin > Setup & status.")
    return _users_page(
        request, con, user, flash_kind="ok",
        flash=f"Reset link sent to {target['display_name']} over "
              f"{channels.LABELS[detail]}. It expires in {RESET_LINK_HOURS} hours.")


# ---------------- API tokens
def _utc(ts: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc) if ts else None


def _token_view(t: sqlite3.Row, now: datetime,
                successor: Optional[sqlite3.Row] = None) -> dict:
    """Row plus the two things an admin actually needs while rotating: how long
    the old secret has left, and whether anything is still calling with it."""
    expires, used = _utc(t["expires_at"]), _utc(t["last_used_at"])
    if t["revoked_at"]:
        chip, status = "off", "revoked"
    elif expires is None:
        chip, status = "on", "active"
    elif expires <= now:
        chip, status = "off", "expired"
    else:
        left = expires - now
        chip, status = "pending", (f"expires in {left.days}d" if left.days
                                   else f"expires in {left.seconds // 3600}h")
    live = not t["revoked_at"] and (expires is None or expires > now)
    # Used since its replacement was minted => the integration still holds the
    # old secret. That is the signal that says "not safe to revoke yet", so the
    # comparison is inclusive: timestamps are second-resolution, and a tie in
    # the rotation second should read as "still in use" rather than "all clear".
    still_in_use = bool(live and successor is not None and used is not None
                        and used >= _utc(successor["created_at"]))
    return {"t": t, "chip": chip, "status": status, "live": live,
            "still_in_use": still_in_use}


def _token_lineages(rows, now: datetime) -> list[dict]:
    """Fold rotation chains together: one row per current token, with the
    secret it just replaced nested underneath. Generations further back are
    long dead, so they collapse to a count rather than growing the table."""
    by_id = {r["id"]: r for r in rows}
    superseded = {r["rotated_from_id"] for r in rows if r["rotated_from_id"]}
    groups = []
    for r in rows:  # rows arrive newest-first
        if r["id"] in superseded:
            continue
        prior = ([_token_view(by_id[r["rotated_from_id"]], now, successor=r)]
                 if r["rotated_from_id"] in by_id else [])
        retired, parent = 0, (by_id[r["rotated_from_id"]]["rotated_from_id"]
                              if prior else None)
        while parent in by_id:
            retired += 1
            parent = by_id[parent]["rotated_from_id"]
        groups.append({"head": _token_view(r, now), "prior": prior,
                       "retired": retired})
    return groups


def _tokens_page(request: Request, user, con: sqlite3.Connection,
                 new_token=None, new_name=None, new_label=None, error=None):
    rows = con.execute("SELECT * FROM api_tokens ORDER BY created_at DESC").fetchall()
    # The MCP connector URL carries the token in the path, and tokens are
    # hashed - so the only moment we can hand over a ready-to-paste URL is the
    # one time the raw value exists, right after create or rotate.
    base = (dbm.get_setting(con, "public_base_url")
            or str(request.base_url).rstrip("/")).rstrip("/")
    return render(request, "admin_tokens.html", user=user, active="tokens",
                  lineages=_token_lineages(rows, datetime.now(timezone.utc)),
                  grace_default=ROTATE_GRACE_DAYS, grace_max=ROTATE_GRACE_MAX,
                  mcp_base=base,
                  mcp_url=f"{base}/mcp/t/{new_token}" if new_token else None,
                  new_token=new_token, new_name=new_name,
                  new_label=new_label, error=error)


@app.get("/admin/tokens", response_class=HTMLResponse)
def admin_tokens(request: Request, user=Depends(require_admin),
                 con: sqlite3.Connection = Depends(db_dep)):
    return _tokens_page(request, user, con)


@app.post("/admin/tokens", response_class=HTMLResponse)
def create_token(request: Request, name: str = Form(...), scope: str = Form("read_write"),
                 user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    raw = new_api_token(con, name.strip(), scope, user["id"])
    return _tokens_page(request, user, con, new_token=raw, new_name=name.strip(),
                        new_label="created")


@app.post("/admin/tokens/{tid}/rotate", response_class=HTMLResponse)
def rotate_token(request: Request, tid: int, grace_days: int = Form(ROTATE_GRACE_DAYS),
                 user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    row = con.execute("SELECT name FROM api_tokens WHERE id = ?", (tid,)).fetchone()
    try:
        raw = rotate_api_token(con, tid, grace_days, user["id"])
    except ValueError as e:
        return _tokens_page(request, user, con, error=str(e))
    return _tokens_page(request, user, con, new_token=raw, new_name=row["name"],
                        new_label="rotated")


@app.post("/admin/tokens/{tid}/revoke")
def revoke_token(tid: int, user=Depends(require_admin),
                 con: sqlite3.Connection = Depends(db_dep)):
    con.execute("UPDATE api_tokens SET revoked_at = datetime('now') WHERE id = ?", (tid,))
    return RedirectResponse("/admin/tokens", status_code=303)


# ---------------- activity (audit trail)
def _audit_value(mtype: Optional[str], unit: Optional[str],
                 numeric, status) -> str:
    if status is not None:
        return status
    if numeric is None:
        return ""
    return gridm.fmt_value(mtype or "numeric", unit, numeric)


@app.get("/admin/activity", response_class=HTMLResponse)
def admin_activity(request: Request, user=Depends(require_admin),
                   con: sqlite3.Connection = Depends(db_dep)):
    """Every write, old value -> new value, who did it and when. Writes made
    after the week's Wednesday-8am staleness deadline carry a LATE chip, so
    quietly back-filling or 'correcting' history is always visible here."""
    rows = con.execute(
        """SELECT a.*, m.name AS metric_name, m.metric_type, m.unit,
                  u.display_name AS actor_name, t.name AS token_name
           FROM entry_audit a
           LEFT JOIN metrics m ON m.id = a.metric_id
           LEFT JOIN users u ON u.id = a.actor_user_id
           LEFT JOIN api_tokens t ON t.id = a.actor_token_id
           ORDER BY a.id DESC LIMIT 200""").fetchall()
    items = []
    for a in rows:
        week = date.fromisoformat(a["week_start"])
        changed = datetime.fromisoformat(a["changed_at"]).replace(tzinfo=timezone.utc)
        items.append({
            "when": changed.astimezone(wk.BUSINESS_TZ).strftime("%b %-d, %-I:%M %p"),
            "metric": a["metric_name"] or f"(deleted #{a['metric_id']})",
            "week": week,
            "old": _audit_value(a["metric_type"], a["unit"],
                                a["old_numeric"], a["old_status"]),
            "new": _audit_value(a["metric_type"], a["unit"],
                                a["new_numeric"], a["new_status"]),
            "by": a["actor_name"] or a["token_name"] or "-",
            "source": a["source"],
            "late": changed >= wk.stale_at(week),
        })
    return render(request, "admin_activity.html", user=user, active="activity",
                  items=items)


# ---------------- setup & status
def _status_page(request: Request, user, con: sqlite3.Connection,
                 matched: str = "", proposal=None, proposal_error=None):
    now = datetime.now(timezone.utc)
    return render(request, "admin_status.html", user=user, active="status",
                  checks=readiness.local_checks(con, now),
                  network=readiness.network_checks(con, now),
                  sweeps=readiness.sweep_runs(con, now),
                  proposal=proposal, proposal_error=proposal_error,
                  matched=matched)


@app.get("/admin/status", response_class=HTMLResponse)
def admin_status(request: Request, matched: str = "", user=Depends(require_admin),
                 con: sqlite3.Connection = Depends(db_dep)):
    """Does this instance actually work? Local checks only on the way in -
    the Slack calls are behind the Re-check button, because an admin page that
    goes down when Slack does is a worse page."""
    return _status_page(request, user, con, matched=matched)


@app.post("/admin/status/recheck")
def admin_status_recheck(user=Depends(require_admin),
                         con: sqlite3.Connection = Depends(db_dep)):
    readiness.run_network_checks(con, datetime.now(timezone.utc))
    return RedirectResponse("/admin/status#verify", status_code=303)


@app.post("/admin/status/match-ids", response_class=HTMLResponse)
def admin_status_match_ids(request: Request, user=Depends(require_admin),
                           con: sqlite3.Connection = Depends(db_dep)):
    """Propose Slack member IDs by joining on email. Shows the diff; writes
    nothing until it is confirmed - the whole failure being fixed here is a
    plausible ID nobody ever checked."""
    rows, err = readiness.propose_member_ids(con)
    return _status_page(request, user, con, proposal=rows, proposal_error=err)


@app.post("/admin/status/match-ids/apply")
def admin_status_apply_ids(pair: list[str] = Form([]), user=Depends(require_admin),
                           con: sqlite3.Connection = Depends(db_dep)):
    n = readiness.apply_member_ids(con, pair)
    return RedirectResponse(f"/admin/status?matched={n}", status_code=303)


# ---------------- settings
@app.get("/admin/settings", response_class=HTMLResponse)
def admin_settings(request: Request, saved: str = "", user=Depends(require_admin),
                   con: sqlite3.Connection = Depends(db_dep)):
    goal_metrics = con.execute(
        """SELECT m.id, m.name, s.name AS section FROM metrics m
           JOIN sections s ON s.id = m.section_id
           WHERE m.archived_at IS NULL AND m.metric_type = 'numeric'
           ORDER BY s.sort_order, m.sort_order""").fetchall()
    return render(request, "admin_settings.html", user=user, active="settings",
                  display_token=dbm.get_setting(con, "display_token"),
                  slack_webhook_url=dbm.get_setting(con, "slack_webhook_url") or "",
                  slack_bot_token=dbm.get_setting(con, "slack_bot_token") or "",
                  slack_channel_id=dbm.get_setting(con, "slack_channel_id") or "",
                  alerts_enabled=dbm.get_setting(con, "alerts_enabled", "0") == "1",
                  screensaver_enabled=dbm.get_setting(con, "screensaver_enabled", "0") == "1",
                  screensaver_start=dbm.get_setting(con, "screensaver_start", "21:00"),
                  screensaver_end=dbm.get_setting(con, "screensaver_end", "06:00"),
                  view_labels=[(v, TV_VIEW_LABELS[v]) for v in TV_VIEWS],
                  enabled_views=_enabled_views(con),
                  rotate_seconds=_rotate_seconds(con),
                  rotate_min=TV_ROTATE_MIN,
                  demo_enabled=dbm.get_setting(con, "display_demo_data", "0") == "1",
                  display_months=int(dbm.get_setting(con, "display_months", "2")),
                  slack_signing_secret=dbm.get_setting(con, "slack_signing_secret") or "",
                  nudges_enabled=dbm.get_setting(con, "nudges_enabled", "0") == "1",
                  nudge_preset=dbm.get_setting(con, "nudge_preset", "mon_tue"),
                  public_base_url=dbm.get_setting(con, "public_base_url") or "",
                  channel_settings={k: dbm.get_setting(con, k) or ""
                                    for k in _CHANNEL_SETTING_KEYS},
                  goal_metrics=goal_metrics,
                  hud_mrr_metric_id=dbm.get_setting(con, "hud_mrr_metric_id") or "",
                  mrr_goal=dbm.get_setting(con, "mrr_goal") or "",
                  mrr_milestones=dbm.get_setting(con, "mrr_milestones") or "",
                  saved=saved,
                  slack_verify=_slack_verify(con),
                  base_url=str(request.base_url).rstrip("/"))


def _slack_verify(con: sqlite3.Connection) -> readiness.Check:
    """The cached token verification, rendered the same way the status page
    renders it, so "Saved." can say what was actually saved."""
    now = datetime.now(timezone.utc)
    return next(c for c in readiness.network_checks(con, now)
                if c.key == "slack_token")


def _settings_saved(section: str) -> RedirectResponse:
    """Back to the panel you were working in, with a confirmation on it.

    A bare redirect to /admin/settings scrolls to the top and looks identical
    whether or not anything was written - which is how a public base URL got
    filled in, discarded by a sibling toggle's reload, and reported as saved."""
    return RedirectResponse(f"/admin/settings?saved={section}#{section}",
                            status_code=303)


@app.post("/admin/settings/goal-band")
def save_goal_band(hud_mrr_metric_id: str = Form(""), mrr_goal: str = Form(""),
                   mrr_milestones: str = Form(""), user=Depends(require_admin),
                   con: sqlite3.Connection = Depends(db_dep)):
    dbm.set_setting(con, "hud_mrr_metric_id", hud_mrr_metric_id.strip())
    dbm.set_setting(con, "mrr_goal", mrr_goal.strip())
    dbm.set_setting(con, "mrr_milestones", mrr_milestones.strip())
    return _settings_saved("goal-band")


@app.post("/admin/settings/display-months")
def save_display_months(display_months: int = Form(...), user=Depends(require_admin),
                        con: sqlite3.Connection = Depends(db_dep)):
    dbm.set_setting(con, "display_months", str(max(1, min(4, display_months))))
    return _settings_saved("display-window")


@app.post("/admin/settings/tv-views")
def save_tv_views(views: list[str] = Form([]), rotate_seconds: int = Form(TV_ROTATE_DEFAULT),
                  user=Depends(require_admin),
                  con: sqlite3.Connection = Depends(db_dep)):
    """Which views the TV cycles through, and how long each is up.

    Unticking everything falls back to the full board rather than saving an
    empty rotation: the TV has no other screen to fall back to, and a blank
    one cannot be fixed from the TV itself."""
    chosen = [v for v in TV_VIEWS if v in views] or ["board"]
    dbm.set_setting(con, "display_views", ",".join(chosen))
    n = max(0, min(3600, rotate_seconds))
    dbm.set_setting(con, "display_rotate_seconds",
                    str(0 if n == 0 else max(TV_ROTATE_MIN, n)))
    return _settings_saved("tv-views")


@app.post("/admin/settings/rotate-display-token")
def rotate_display_token(user=Depends(require_admin),
                         con: sqlite3.Connection = Depends(db_dep)):
    dbm.set_setting(con, "display_token", secrets.token_urlsafe(24))
    return _settings_saved("tv-display")


@app.post("/admin/settings/demo-toggle")
def demo_toggle(user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    turning_on = dbm.get_setting(con, "display_demo_data", "0") != "1"
    dbm.set_setting(con, "display_demo_data", "1" if turning_on else "0")
    if turning_on:
        demo.reset()  # fresh fictional data every time it is switched on
    return _settings_saved("demo")


@app.post("/admin/settings/alerts-toggle")
def alerts_toggle(user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    cur = dbm.get_setting(con, "alerts_enabled", "0")
    dbm.set_setting(con, "alerts_enabled", "0" if cur == "1" else "1")
    return _settings_saved("slack")


@app.post("/admin/settings/screensaver-toggle")
def screensaver_toggle(user=Depends(require_admin),
                       con: sqlite3.Connection = Depends(db_dep)):
    cur = dbm.get_setting(con, "screensaver_enabled", "0")
    dbm.set_setting(con, "screensaver_enabled", "0" if cur == "1" else "1")
    return _settings_saved("screensaver")


@app.post("/admin/settings/screensaver")
def save_screensaver(screensaver_start: str = Form("21:00"),
                     screensaver_end: str = Form("06:00"),
                     user=Depends(require_admin),
                     con: sqlite3.Connection = Depends(db_dep)):
    # <input type=time> submits "HH:MM"; anything else falls back to defaults.
    for key, val, default in (("screensaver_start", screensaver_start, "21:00"),
                              ("screensaver_end", screensaver_end, "06:00")):
        try:
            dt_time.fromisoformat(val.strip())
            dbm.set_setting(con, key, val.strip())
        except ValueError:
            dbm.set_setting(con, key, default)
    return _settings_saved("screensaver")


def _save_slack_settings(con, webhook: str, bot: str, channel: str,
                         signing: str) -> None:
    dbm.set_setting(con, "slack_webhook_url", webhook.strip())
    dbm.set_setting(con, "slack_bot_token", bot.strip())
    dbm.set_setting(con, "slack_channel_id", channel.strip())
    dbm.set_setting(con, "slack_signing_secret", signing.strip())
    # Verify on save, and say which workspace the token is for. A bot token is
    # a long opaque string that looks identical whether it belongs to your
    # workspace or someone else's - which is exactly how one for an entirely
    # different workspace sat here looking fine. One call answers it, at the
    # only moment anyone is looking. No token: the same call, no network.
    con.commit()
    readiness.verify_token(con, datetime.now(timezone.utc))


@app.post("/admin/settings/slack")
def save_slack(slack_webhook_url: str = Form(""), slack_bot_token: str = Form(""),
               slack_channel_id: str = Form(""), slack_signing_secret: str = Form(""),
               user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    _save_slack_settings(con, slack_webhook_url, slack_bot_token, slack_channel_id,
                         slack_signing_secret)
    return _settings_saved("slack")


@app.post("/admin/settings/nudges")
def save_nudges(public_base_url: str = Form(""), nudge_preset: str = Form("mon_tue"),
                user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    dbm.set_setting(con, "public_base_url", public_base_url.strip().rstrip("/"))
    if nudge_preset in ("mon_tue", "mon", "tue"):
        dbm.set_setting(con, "nudge_preset", nudge_preset)
    return _settings_saved("nudges")


_CHANNEL_SETTING_KEYS = ("teams_webhook_url", "gchat_webhook_url",
                         "twilio_account_sid", "twilio_auth_token",
                         "twilio_from", "telegram_bot_token")


def _save_channel_settings(con: sqlite3.Connection, form: dict[str, str]) -> None:
    for key in _CHANNEL_SETTING_KEYS:
        dbm.set_setting(con, key, (form.get(key) or "").strip())


def _channel_form(teams_webhook_url: str = Form(""), gchat_webhook_url: str = Form(""),
                  twilio_account_sid: str = Form(""), twilio_auth_token: str = Form(""),
                  twilio_from: str = Form(""),
                  telegram_bot_token: str = Form("")) -> dict[str, str]:
    """Declared fields rather than `await request.form()`: an async endpoint
    runs on the event loop while Depends(db_dep) opened its SQLite connection
    in a worker thread, and sqlite3 refuses to be used across the two. Both
    handlers below 500'd on every save because of it."""
    return {"teams_webhook_url": teams_webhook_url,
            "gchat_webhook_url": gchat_webhook_url,
            "twilio_account_sid": twilio_account_sid,
            "twilio_auth_token": twilio_auth_token,
            "twilio_from": twilio_from,
            "telegram_bot_token": telegram_bot_token}


@app.post("/admin/settings/channels")
def save_channels(form: dict = Depends(_channel_form), user=Depends(require_admin),
                  con: sqlite3.Connection = Depends(db_dep)):
    _save_channel_settings(con, form)
    return _settings_saved("channels")


@app.post("/admin/settings/telegram-register")
def telegram_register(request: Request, form: dict = Depends(_channel_form),
                      user=Depends(require_admin),
                      con: sqlite3.Connection = Depends(db_dep)):
    """Save the channel settings, then point the Telegram bot's webhook at
    this server (with a generated secret token) so typed replies work."""
    _save_channel_settings(con, form)
    token = (form.get("telegram_bot_token") or "").strip()
    base = (dbm.get_setting(con, "public_base_url")
            or str(request.base_url).rstrip("/"))
    secret = dbm.get_setting(con, "telegram_webhook_secret")
    if not secret:
        secret = secrets.token_urlsafe(24)
        dbm.set_setting(con, "telegram_webhook_secret", secret)
    con.commit()
    if token:
        try:
            import httpx
            r = httpx.post(f"https://api.telegram.org/bot{token}/setWebhook",
                           json={"url": f"{base}/telegram/webhook",
                                 "secret_token": secret,
                                 "allowed_updates": ["message"]}, timeout=10)
            log.info("telegram setWebhook: %s %s", r.status_code, r.text[:200])
        except Exception as e:  # network failure must not 500 the settings page
            log.warning("telegram setWebhook failed: %s", e)
    return _settings_saved("channels")


@app.post("/admin/settings/nudges-toggle")
def nudges_toggle(user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    cur = dbm.get_setting(con, "nudges_enabled", "0")
    dbm.set_setting(con, "nudges_enabled", "0" if cur == "1" else "1")
    return _settings_saved("nudges")


@app.post("/admin/settings/nudge-test")
def nudge_test(request: Request, user=Depends(require_admin),
               con: sqlite3.Connection = Depends(db_dep)):
    """Message the current admin their own nudge, over their chosen channel,
    exactly as DRIs will get it. No alerts_sent rows, so it can be re-sent
    any number of times."""
    base = (dbm.get_setting(con, "public_base_url")
            or str(request.base_url).rstrip("/"))
    real = _real_actor(request, user)
    if channels.ready(con, real):
        now = datetime.now(timezone.utc)
        if not alerts.compose_and_send_nudge(con, real, base, now):
            alerts.send_direct(con, real,
                               "Test nudge: all your numbers are entered - "
                               "nothing due right now.")
    return _settings_saved("nudges")


@app.post("/admin/settings/slack-test")
def slack_test(slack_webhook_url: str = Form(""), slack_bot_token: str = Form(""),
               slack_channel_id: str = Form(""), slack_signing_secret: str = Form(""),
               user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    _save_slack_settings(con, slack_webhook_url, slack_bot_token, slack_channel_id,
                         slack_signing_secret)
    con.commit()
    msg = "Aprendio Scorecard: test message. Alerts are wired up."
    if slack_webhook_url.strip():
        alerts.post_channel(slack_webhook_url.strip(), msg)
    elif slack_bot_token.strip() and slack_channel_id.strip():
        alerts.post_channel_bot(slack_bot_token.strip(), slack_channel_id.strip(), msg,
                                icon=alerts.bot_icon_url(con))
    return _settings_saved("slack")
