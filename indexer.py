"""The daily index run: watches -> API, manual URLs -> fetch, then sold detection.

Designed to be safe to run any time: everything is upsert-based, the API budget
guard makes over-running impossible, and a crash mid-run loses nothing since
each vehicle commits individually.
"""

import logging
from typing import Dict

from api_client import AutoDevClient, BudgetExhausted
from manual_tracker import check_url, title_to_fields

log = logging.getLogger(__name__)


def run_index(db, client: AutoDevClient, user_id: int = 1) -> Dict:
    """Run every enabled watch for a user and manual URLs. Returns a summary dict."""
    summary = {
        "watches_run": 0, "listings_seen": 0, "new_vehicles": 0,
        "manual_checked": 0, "manual_gone": 0,
        "marked_sold": 0, "budget_hit": False, "errors": [],
    }

    # --- API watches --------------------------------------------------
    for watch in db.watches(user_id=user_id, enabled_only=True):
        try:
            records = list(client.search_watch(watch))
            count = len(records)
            if records:
                db.upsert_vehicles_batch(records, user_id=user_id,
                                         watch_id=watch["watch_id"])
            summary["listings_seen"] += count
            summary["new_vehicles"] += count
            db.touch_watch(watch["watch_id"], count)
            summary["watches_run"] += 1
            log.info("watch %s (%s): %s listings", watch["watch_id"],
                     watch["label"], count)
        except BudgetExhausted as exc:
            summary["budget_hit"] = True
            summary["errors"].append(str(exc))
            log.warning("%s", exc)
            break
        except Exception as exc:
            summary["errors"].append(f"watch {watch['label']}: {exc}")
            log.exception("watch %s failed", watch["watch_id"])

    # --- manual URLs ----------------------------------------------------
    for manual in db.manual_urls(user_id=user_id, enabled_only=True):
        summary["manual_checked"] += 1
        result = check_url(manual["url"], known_vin=manual["vin"] or "")

        if result["status"] == "gone":
            summary["manual_gone"] += 1
            db.update_manual(manual["manual_id"], "gone")
            # If it maps to a vehicle, the sold sweep below will catch it via
            # last_seen aging out; mark it immediately for responsiveness.
            if manual["vehicle_id"]:
                db.conn.execute(
                    "UPDATE vehicles SET is_active = 0, "
                    "sold_detected = date('now') WHERE vehicle_id = ?",
                    (manual["vehicle_id"],))
                db.conn.commit()
            continue

        if result["status"] == "error":
            db.update_manual(manual["manual_id"], "error")
            continue

        fields = title_to_fields(result["title"])
        record = {
            "vin": result["vin"],
            "year": fields.get("year"),
            "make": "",
            "model": manual["label"] or result["title"][:60],
            "price": result["price"],
            "accepting_offers": result["price"] is None,
            "listing_url": manual["url"],
            "dealer_name": "(manual)",
        }
        vehicle_id, is_new = db.upsert_vehicle(record, user_id=user_id, source="manual")
        if is_new:
            summary["new_vehicles"] += 1
        db.update_manual(manual["manual_id"], "ok", vehicle_id)

    # --- sold detection -------------------------------------------------
    summary["marked_sold"] = db.mark_sold(user_id=user_id)

    return summary
