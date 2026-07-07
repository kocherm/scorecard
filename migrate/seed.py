"""Seed the scorecard from seed_data.local.json: sections, metrics, targets,
users, historical weekly data, display token, and an integration API token.

ALL company-specific data (people, clients, targets, history) comes from
seed_data.local.json, which is gitignored. This file stays generic.

Idempotent: refuses to run if any section already exists.
Usage: uv run python -m migrate.seed
Prints temp passwords and the API token ONCE - copy them.
"""
from __future__ import annotations

import json
import secrets
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import db as dbm  # noqa: E402
from app.auth import hash_password, new_api_token  # noqa: E402

LOCAL = Path(__file__).parent / "seed_data.local.json"


def main() -> None:
    data = json.loads(LOCAL.read_text())
    history_weeks = [date.fromisoformat(w) for w in data.get("history_weeks", [])]
    history_numeric = data.get("history_numeric", {})
    history_status = data.get("history_status", {})
    start_hist = min(history_weeks) if history_weeks else date(2026, 7, 6)
    # Metrics without historical values start at the current quarter week
    # instead, so they don't render months of fake stale cells.
    start_fresh = date(2026, 7, 6)

    with dbm.get_db() as con:
        dbm.init_db(con)
        if con.execute("SELECT 1 FROM sections LIMIT 1").fetchone():
            print("Already seeded; aborting. (Drop the DB to reseed.)")
            return

        # ---- users
        uid: dict[str, int] = {}
        creds: list[tuple[str, str, str]] = []
        for u in data["users"]:
            pw = secrets.token_urlsafe(9)
            cur = con.execute(
                """INSERT INTO users (email, password_hash, display_name, role,
                                      must_change_password)
                   VALUES (?,?,?,?,1)""",
                (u["email"], hash_password(pw), u["name"], u["role"]))
            uid[u["email"]] = cur.lastrowid
            creds.append((u["name"], u["email"], pw))
        dri = {k: uid[v] for k, v in data["dri"].items()}

        # ---- sections
        def section(name: str, icon: str, order: int, enabled: int = 1) -> int:
            return con.execute(
                "INSERT INTO sections (name, icon, sort_order, is_enabled) VALUES (?,?,?,?)",
                (name, icon, order, enabled)).lastrowid

        s_sales = section("Sales Activity", "trend", 1)
        s_rev = section("Revenue", "dollar", 2)
        s_health = section("Client Health", "heart", 3)
        s_content = section("Content & Pipeline", "megaphone", 4)
        s_fulfil = section("Fulfillment Health", "wrench", 5, enabled=0)

        # ---- metrics (generic methodology structure; DRIs come from the json)
        mid: dict[str, int] = {}

        def metric(key: str, sec: int, name: str, mtype: str, order: int,
                   rollup: str | None = "sum", direction: str = "up",
                   unit: str | None = None, dri_key: str | None = None) -> None:
            has_history = key in history_numeric or key in history_status
            start = start_hist if has_history else start_fresh
            mid[key] = con.execute(
                """INSERT INTO metrics (section_id, name, metric_type, rollup, direction,
                                        unit, dri_user_id, start_week, sort_order)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (sec, name, mtype, rollup if mtype == "numeric" else None,
                 direction, unit, dri.get(dri_key) if dri_key else None,
                 start.isoformat(), order)).lastrowid

        metric("conversations", s_sales, "New qualified conversations", "numeric", 1,
               dri_key="sales_activity")
        metric("followups", s_sales, "Follow-ups sent to pipeline", "numeric", 2,
               dri_key="sales_activity")
        metric("calls_booked", s_sales, "Discovery calls booked", "numeric", 3,
               dri_key="sales_activity")
        metric("calls_held", s_sales, "Discovery calls held", "numeric", 4,
               dri_key="calls_held")
        metric("proposals", s_sales, "Proposals sent", "numeric", 5,
               dri_key="calls_held")

        metric("mrr", s_rev, "Current MRR", "numeric", 1, rollup="average",
               unit="$", dri_key="revenue")
        metric("new_mrr", s_rev, "New MRR added", "numeric", 2, unit="$",
               dri_key="revenue")
        metric("churn_risk", s_rev, "Churn risk", "numeric", 3, rollup="average",
               direction="down", unit="$", dri_key="revenue")

        for i, client in enumerate(data["clients"], 1):
            metric(f"client_{client}", s_health, client, "status", i,
                   rollup=None, dri_key="client_health")

        metric("scripts", s_content, "Video scripts drafted", "numeric", 1,
               dri_key="content")
        metric("posts", s_content, "Posts / carousels published", "numeric", 2,
               dri_key="content")
        metric("content_convos", s_content, "New conversations from content", "numeric", 3,
               dri_key="content")
        metric("followers", s_content, "New followers from content", "numeric", 4,
               dri_key="followers")

        # The 7 numbers every founder must know. Ships hidden; enable the
        # section in admin when ready to track them.
        s_unit = section("Unit Economics", "chart", 6, enabled=0)
        metric("cash_collected", s_unit, "Cash collected", "numeric", 1,
               unit="$", dri_key="revenue")
        metric("expenses", s_unit, "Expenses", "numeric", 2, unit="$",
               direction="down", dri_key="revenue")
        metric("leads", s_unit, "New leads", "numeric", 3, dri_key="sales_activity")
        metric("conversions", s_unit, "New customers (conversions)", "numeric", 4,
               dri_key="revenue")
        metric("cac", s_unit, "Customer acquisition cost", "numeric", 5,
               rollup="average", unit="$", direction="down", dri_key="revenue")
        metric("retention", s_unit, "Client retention", "numeric", 6,
               rollup="average", unit="%", dri_key="revenue")
        metric("profit", s_unit, "Profit", "numeric", 7, unit="$", dri_key="revenue")

        metric("bugs", s_fulfil, "Open bug tickets (all clients)", "numeric", 1,
               rollup="average", direction="down", dri_key="revenue")
        metric("deliverables", s_fulfil, "Deliverables delivered on time", "numeric", 2,
               rollup="average", unit="%", dri_key="revenue")
        metric("intern_rate", s_fulfil, "Intern task completion rate", "numeric", 3,
               rollup="average", unit="%")

        # Dan Martell's leading trio: bolded and starred on the board.
        for key in ("conversations", "calls_held", "proposals"):
            if key in mid:
                con.execute("UPDATE metrics SET is_key = 1 WHERE id = ?", (mid[key],))

        # ---- targets (baseline weeks 1-6 / stretch weeks 7+)
        tq = data.get("target_quarter", {"year": 2026, "quarter": 3})
        for key, pair in data.get("targets", {}).items():
            if key in mid:
                con.execute(
                    """INSERT INTO targets (metric_id, year, quarter,
                                            baseline_value, stretch_value)
                       VALUES (?,?,?,?,?)""",
                    (mid[key], tq["year"], tq["quarter"], pair[0], pair[1]))

        # ---- historical entries (attributed to the first/admin user)
        admin_id = uid[data["users"][0]["email"]]
        for key, vals in history_numeric.items():
            if key not in mid:
                continue
            for w, v in zip(history_weeks, vals):
                if v is not None:
                    dbm.upsert_entry(con, mid[key], w, value_numeric=float(v),
                                     source="manual", user_id=admin_id)
        for client, vals in history_status.items():
            key = f"client_{client}"
            if key not in mid:
                continue
            for w, v in zip(history_weeks, vals):
                if v is not None:
                    dbm.upsert_entry(con, mid[key], w, value_status=v,
                                     source="manual", user_id=admin_id)

        # ---- display + integration tokens
        display = secrets.token_urlsafe(24)
        dbm.set_setting(con, "display_token", display)
        if "mrr" in mid:
            dbm.set_setting(con, "hud_mrr_metric_id", str(mid["mrr"]))
            dbm.set_setting(con, "mrr_goal", "100000")
            dbm.set_setting(con, "mrr_milestones",
                            "37000:$37k Q3;62000:$62k Q4;100000:$100k")
        api_tok = new_api_token(con, "hermes", "read_write", admin_id)

        print("\n=== SEED COMPLETE - copy these now, they are not shown again ===")
        for name, email, pw in creds:
            print(f"  {name:<16} {email:<28} temp password: {pw}")
        print(f"\n  TV display URL path: /display?token={display}")
        print(f"  Integration API token: {api_tok}")
        print("=================================================================\n")


if __name__ == "__main__":
    main()
