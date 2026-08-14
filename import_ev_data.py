"""One-time import of Taylor's EV deals from ev_deals.db into Turso."""
import sqlite3
import sys
sys.path.insert(0, ".")

from db import Database

TAYLOR_USER_ID = 2
SOURCE_DB = "../ev_deals.db"


def main():
    # Read from local ev_deals.db
    conn = sqlite3.connect(SOURCE_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT vin, year, make, model, trim, mileage,
               advertised_price as price, dealer_name,
               dealer_city as city, dealer_state as state,
               listing_url, first_seen, last_seen, is_active,
               source
        FROM vehicles
        WHERE vin IS NOT NULL AND vin != ''
    """).fetchall()
    conn.close()
    print(f"Read {len(rows)} vehicles from ev_deals.db")

    # Convert to record dicts
    records = []
    for row in rows:
        records.append({
            "vin": row["vin"],
            "year": row["year"],
            "make": row["make"],
            "model": row["model"],
            "trim": row["trim"],
            "mileage": row["mileage"],
            "price": row["price"],
            "accepting_offers": row["price"] is None,
            "dealer_name": row["dealer_name"] or "",
            "city": row["city"] or "",
            "state": row["state"] or "",
            "listing_url": row["listing_url"] or "",
        })

    # Batch insert into Turso
    db = Database()

    # Insert in chunks of 50 (pipeline has size limits)
    chunk_size = 50
    total = 0
    for i in range(0, len(records), chunk_size):
        chunk = records[i:i+chunk_size]
        db.upsert_vehicles_batch(chunk, user_id=TAYLOR_USER_ID, source="imported")
        total += len(chunk)
        print(f"  Imported {total}/{len(records)}...")

    print(f"Done! {total} vehicles imported for Taylor (user_id={TAYLOR_USER_ID})")


if __name__ == "__main__":
    main()
