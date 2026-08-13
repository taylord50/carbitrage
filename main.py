"""Carbitrage: luxury-car market tracker for arbitrage hunting.

Run this file. On Replit it just works; locally: python main.py

SECURITY NOTE: on Replit this web app gets a PUBLIC URL with no login. The data
(car listings) is not sensitive and the API key stays in Replit Secrets, never
in the page. If you want a lock anyway, set an APP_PASSWORD secret and the app
will require it once per browser session.
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

# Load .env file if present (fallback for when Replit Secrets aren't working)
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

DB_PATH = os.environ.get("CARBITRAGE_DB", "carbitrage.db")
BUDGET = int(os.environ.get("API_BUDGET", DEFAULT_BUDGET))
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")


def get_db() -> Database:
    return Database(DB_PATH)


def protected(view):
    """Optional password gate, active only when APP_PASSWORD is set."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if APP_PASSWORD and not session.get("authed"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    if not APP_PASSWORD:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        if request.form.get("password") == APP_PASSWORD:
            session["authed"] = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        return render_template("login.html", error="Wrong password")
    return render_template("login.html", error="")


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
@protected
def dashboard():
    db = get_db()
    view = request.args.get("view", "active")

    if view == "starred":
        vehicles = db.vehicles(active_only=False, starred_only=True)
    elif view == "sold":
        vehicles = db.sold_vehicles()
    else:
        vehicles = db.vehicles(active_only=True)

    gauge = db.usage_gauge(BUDGET)
    watches = db.watches()
    manuals = db.manual_urls()
    key_set = bool(os.environ.get("AUTODEV_API_KEY", "").strip())

    db.close()
    return render_template(
        "dashboard.html", vehicles=vehicles, gauge=gauge, watches=watches,
        manuals=manuals, view=view, key_set=key_set,
    )


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

@app.route("/watch/add", methods=["POST"])
@protected
def add_watch():
    db = get_db()
    form = request.form

    def num(name):
        value = form.get(name, "").replace(",", "").replace("$", "").strip()
        return int(value) if value.isdigit() else None

    make = form.get("make", "").strip()
    if make:
        db.add_watch(
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
@protected
def test_watch():
    """Probe the API with one call to validate a watch definition.

    Costs 1 API call, returns JSON with count and sample listings.
    This is the 'Test this watch' button behavior.
    """
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
@protected
def toggle_watch(watch_id):
    db = get_db()
    current = [w for w in db.watches() if w["watch_id"] == watch_id]
    if current:
        db.set_watch_enabled(watch_id, not current[0]["enabled"])
    db.close()
    return redirect(url_for("dashboard"))


@app.route("/watch/<int:watch_id>/delete", methods=["POST"])
@protected
def delete_watch(watch_id):
    db = get_db()
    db.delete_watch(watch_id)
    db.close()
    return redirect(url_for("dashboard"))


@app.route("/manual/add", methods=["POST"])
@protected
def add_manual():
    db = get_db()
    url = request.form.get("url", "").strip()
    if url.startswith("http"):
        db.add_manual_url(url, label=request.form.get("label", ""),
                          vin=request.form.get("vin", ""))
    db.close()
    return redirect(url_for("dashboard"))


@app.route("/manual/<int:manual_id>/delete", methods=["POST"])
@protected
def delete_manual(manual_id):
    db = get_db()
    db.delete_manual_url(manual_id)
    db.close()
    return redirect(url_for("dashboard"))


@app.route("/vehicle/<int:vehicle_id>/star", methods=["POST"])
@protected
def star_vehicle(vehicle_id):
    db = get_db()
    starred = request.form.get("starred") == "1"
    db.set_starred(vehicle_id, starred)
    db.close()
    return jsonify({"ok": True, "starred": starred})


@app.route("/run", methods=["POST"])
@protected
def run_now():
    """Run the daily index on demand (also the endpoint a cron job hits)."""
    db = get_db()
    client = AutoDevClient(db, budget=BUDGET)
    summary = run_index(db, client)
    db.close()
    log.info("index run: %s", summary)
    return render_template("run_result.html", summary=summary)


@app.route("/seed", methods=["POST"])
@protected
def seed_watches():
    """Pre-populate the watchlist with the recommended luxury EV set."""
    db = get_db()
    existing_labels = {w["label"] for w in db.watches()}

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

    added = 0
    for seed in seeds:
        if seed["label"] in existing_labels:
            continue
        db.add_watch(
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
@protected
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
@protected
def export_csv():
    db = get_db()
    view = request.args.get("view", "active")
    if view == "starred":
        vehicles = db.vehicles(active_only=False, starred_only=True)
    elif view == "sold":
        vehicles = db.sold_vehicles()
    else:
        vehicles = db.vehicles(active_only=True)

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
        download_name=f"carbitrage_{view}.csv",
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    if not APP_PASSWORD:
        log.warning(
            "No APP_PASSWORD set: the web UI is open to anyone with the URL. "
            "Fine for car listings, but set the APP_PASSWORD secret in Replit "
            "if you want a lock.")
    app.run(host="0.0.0.0", port=port, debug=False)
