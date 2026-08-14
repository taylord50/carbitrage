"""Carbitrage: multi-user luxury/budget EV market tracker.

Each user sees their own watches and vehicles. The API budget is shared.
"""

import csv
import io
import logging
import os
from functools import wraps
from pathlib import Path

from flask import (Flask, jsonify, redirect, render_template, request,
                   send_file, session, url_for)

from api_client import DEFAULT_BUDGET, AutoDevClient
from db import Database
from indexer import run_index

# Load .env file if present
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("carbitrage")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", os.urandom(24).hex())

BUDGET = int(os.environ.get("API_BUDGET", DEFAULT_BUDGET))


def get_db() -> Database:
    return Database()


def current_user_id() -> int:
    """Get the active user_id from session, default to darren (1)."""
    return session.get("user_id", 1)


def current_username() -> str:
    return session.get("username", "darren")


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
def dashboard():
    db = get_db()
    user_id = current_user_id()
    view = request.args.get("view", "active")

    if view == "starred":
        vehicles = db.vehicles(user_id=user_id, active_only=False, starred_only=True)
    elif view == "sold":
        vehicles = db.sold_vehicles(user_id=user_id)
    else:
        vehicles = db.vehicles(user_id=user_id, active_only=True)

    gauge = db.usage_gauge(BUDGET)
    watches = db.watches(user_id=user_id)
    manuals = db.manual_urls(user_id=user_id)
    users = db.get_users()
    key_set = bool(os.environ.get("AUTODEV_API_KEY", "").strip())

    db.close()
    return render_template(
        "dashboard.html", vehicles=vehicles, gauge=gauge, watches=watches,
        manuals=manuals, view=view, key_set=key_set, users=users,
        current_user=current_username(), current_user_id=user_id,
    )


@app.route("/switch/<username>")
def switch_user(username):
    db = get_db()
    user = db.get_user(username)
    db.close()
    if user:
        session["user_id"] = user["user_id"]
        session["username"] = user["username"]
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

@app.route("/watch/add", methods=["POST"])
def add_watch():
    db = get_db()
    form = request.form
    user_id = current_user_id()

    def num(name):
        value = form.get(name, "").replace(",", "").replace("$", "").strip()
        return int(value) if value.isdigit() else None

    make = form.get("make", "").strip()
    if make:
        db.add_watch(
            user_id=user_id,
            label=form.get("label", "").strip() or f"{make} {form.get('model','')}".strip(),
            make=make,
            model=form.get("model", "").strip(),
            trim_contains=form.get("trim_contains", "").strip(),
            year_min=num("year_min"), year_max=num("year_max"),
            price_min=num("price_min"), price_max=num("price_max"),
            mileage_max=num("mileage_max"),
            zip_code=form.get("zip_code", "").strip(),
            radius=num("radius") or 0,
        )
    db.close()
    return redirect(url_for("dashboard"))


@app.route("/watch/test", methods=["POST"])
def test_watch():
    db = get_db()
    form = request.form

    def num(name):
        value = form.get(name, "").replace(",", "").replace("$", "").strip()
        return int(value) if value.isdigit() else None

    watch = {
        "make": form.get("make", "").strip(),
        "model": form.get("model", "").strip(),
        "trim_contains": form.get("trim_contains", "").strip(),
        "year_min": num("year_min"),
        "year_max": num("year_max"),
        "price_min": num("price_min"),
        "price_max": num("price_max"),
        "mileage_max": num("mileage_max"),
        "zip_code": form.get("zip_code", "").strip(),
        "radius": num("radius") or 0,
    }

    client = AutoDevClient(db, budget=BUDGET)
    result = client.test_watch(watch)
    db.close()
    return jsonify(result)


@app.route("/watch/<int:watch_id>/toggle", methods=["POST"])
def toggle_watch(watch_id):
    db = get_db()
    current = [w for w in db.watches(user_id=current_user_id()) if w["watch_id"] == watch_id]
    if current:
        db.set_watch_enabled(watch_id, not current[0]["enabled"])
    db.close()
    return redirect(url_for("dashboard"))


@app.route("/watch/<int:watch_id>/delete", methods=["POST"])
def delete_watch(watch_id):
    db = get_db()
    db.delete_watch(watch_id)
    db.close()
    return redirect(url_for("dashboard"))


@app.route("/manual/add", methods=["POST"])
def add_manual():
    db = get_db()
    url = request.form.get("url", "").strip()
    if url.startswith("http"):
        db.add_manual_url(url, user_id=current_user_id(),
                          label=request.form.get("label", ""),
                          vin=request.form.get("vin", ""))
    db.close()
    return redirect(url_for("dashboard"))


@app.route("/manual/<int:manual_id>/delete", methods=["POST"])
def delete_manual(manual_id):
    db = get_db()
    db.delete_manual_url(manual_id)
    db.close()
    return redirect(url_for("dashboard"))


@app.route("/vehicle/<int:vehicle_id>/star", methods=["POST"])
def star_vehicle(vehicle_id):
    db = get_db()
    starred = request.form.get("starred") == "1"
    db.set_starred(vehicle_id, starred)
    db.close()
    return jsonify({"ok": True, "starred": starred})


@app.route("/run", methods=["POST"])
def run_now():
    """Run the index for the current user's watches."""
    db = get_db()
    user_id = current_user_id()
    client = AutoDevClient(db, budget=BUDGET)
    summary = run_index(db, client, user_id=user_id)
    db.close()
    log.info("index run (user %s): %s", current_username(), summary)
    return render_template("run_result.html", summary=summary)


@app.route("/seed", methods=["POST"])
def seed_watches():
    """Pre-populate watches for the current user."""
    db = get_db()
    user_id = current_user_id()
    username = current_username()
    existing_labels = {w["label"] for w in db.watches(user_id=user_id)}

    if username == "darren":
        seeds = [
            {"label": "Porsche Taycan", "make": "Porsche", "model": "Taycan"},
            {"label": "Porsche Macan Electric", "make": "Porsche", "model": "Macan Electric"},
            {"label": "Porsche Cayenne Electric", "make": "Porsche", "model": "Cayenne",
             "trim_contains": "Electric", "year_min": 2026},
            {"label": "Mercedes EQS", "make": "Mercedes-Benz", "model": "EQS"},
            {"label": "Mercedes EQE", "make": "Mercedes-Benz", "model": "EQE"},
            {"label": "Mercedes EQB", "make": "Mercedes-Benz", "model": "EQB"},
            {"label": "Lucid Air", "make": "Lucid", "model": "Air"},
            {"label": "BMW i7", "make": "BMW", "model": "i7"},
            {"label": "Audi e-tron GT", "make": "Audi", "model": "e-tron GT"},
        ]
    else:  # taylor — budget luxury EVs
        seeds = [
            {"label": "Kia EV9", "make": "Kia", "model": "EV9"},
            {"label": "Kia EV6", "make": "Kia", "model": "EV6"},
            {"label": "Genesis GV60", "make": "Genesis", "model": "GV60"},
            {"label": "Genesis GV70 Electric", "make": "Genesis", "model": "Electrified GV70"},
            {"label": "BMW iX", "make": "BMW", "model": "iX"},
            {"label": "BMW i4", "make": "BMW", "model": "i4"},
            {"label": "Audi Q8 e-tron", "make": "Audi", "model": "Q8 e-tron"},
            {"label": "Volvo EX90", "make": "Volvo", "model": "EX90"},
            {"label": "Cadillac LYRIQ", "make": "Cadillac", "model": "LYRIQ"},
            {"label": "Polestar 2", "make": "Polestar", "model": "2"},
        ]

    added = 0
    for seed in seeds:
        if seed["label"] in existing_labels:
            continue
        db.add_watch(
            user_id=user_id,
            label=seed["label"],
            make=seed["make"],
            model=seed.get("model", ""),
            trim_contains=seed.get("trim_contains", ""),
            year_min=seed.get("year_min"),
            year_max=seed.get("year_max"),
            price_min=seed.get("price_min"),
            price_max=seed.get("price_max"),
            mileage_max=seed.get("mileage_max"),
            zip_code=seed.get("zip_code", ""),
            radius=seed.get("radius") or 0,
        )
        added += 1

    db.close()
    return redirect(url_for("dashboard"))


@app.route("/vehicle/<int:vehicle_id>/history")
def vehicle_history(vehicle_id):
    db = get_db()
    series = [
        {"date": row["snapshot_date"], "price": row["price"],
         "mileage": row["mileage"]}
        for row in db.price_series(vehicle_id)
    ]
    db.close()
    return jsonify(series)


@app.route("/export.csv")
def export_csv():
    db = get_db()
    user_id = current_user_id()
    view = request.args.get("view", "active")
    if view == "starred":
        vehicles = db.vehicles(user_id=user_id, active_only=False, starred_only=True)
    elif view == "sold":
        vehicles = db.sold_vehicles(user_id=user_id)
    else:
        vehicles = db.vehicles(user_id=user_id, active_only=True)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    columns = ["vin", "year", "make", "model", "trim", "price",
               "accepting_offers", "mileage", "dealer_name", "city", "state",
               "listing_url", "starred", "first_seen", "last_seen",
               "days_tracked", "first_price", "lowest_price", "is_active",
               "sold_detected"]
    writer.writerow(columns)
    for vehicle in vehicles:
        keys = vehicle.keys()
        writer.writerow([vehicle[c] if c in keys else "" for c in columns])
    db.close()

    buffer.seek(0)
    return send_file(
        io.BytesIO(buffer.getvalue().encode("utf-8")),
        mimetype="text/csv", as_attachment=True,
        download_name=f"carbitrage_{current_username()}_{view}.csv",
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
