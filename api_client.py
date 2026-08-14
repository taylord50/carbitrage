"""Auto.dev API client with a hard budget guard.

Every call is counted against a monthly budget (default 950 of the free tier's
1,000). When the budget is exhausted the client refuses to make calls until the
month rolls over, so the account can never incur a charge. The guard lives here,
at the lowest level, so no code path can accidentally bypass it.

Exotics frequently list without a price ("accepting_offers"). Those records are
kept and tracked: days-on-market and disappearance still carry signal even when
the price is hidden.
"""

import logging
import os
from typing import Any, Dict, Iterable, List, Optional

import requests

log = logging.getLogger(__name__)

API_BASE = "https://auto.dev/api/listings"
ENV_KEY = "AUTODEV_API_KEY"

#: Stop at 950, not 1,000, leaving margin for retries and manual testing.
DEFAULT_BUDGET = 950


class BudgetExhausted(RuntimeError):
    """Raised when the monthly call budget is used up."""


class AutoDevClient:
    def __init__(self, db, api_key: Optional[str] = None,
                 budget: int = DEFAULT_BUDGET):
        self.db = db
        self.api_key = (api_key or os.environ.get(ENV_KEY, "")).strip()
        self.budget = budget

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _check_budget(self) -> None:
        used = self.db.calls_this_month()
        if used >= self.budget:
            raise BudgetExhausted(
                f"Monthly API budget reached ({used}/{self.budget}). "
                "No further calls until the month rolls over. "
                "This is the guard that keeps the account free.")

    def search(self, params: Dict[str, Any]) -> Optional[Dict]:
        """One budget-counted API call. Returns parsed JSON or None."""
        if not self.configured:
            log.warning("No API key set (%s). Skipping.", ENV_KEY)
            return None
        self._check_budget()

        try:
            response = requests.get(
                API_BASE, params=params,
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Accept": "application/json"},
                timeout=20,
            )
        except requests.RequestException as exc:
            log.warning("API request failed: %s", exc)
            self.db.record_call()   # a failed call still counts against quota
            return None

        self.db.record_call()
        if response.status_code != 200:
            log.warning("API returned %s", response.status_code)
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def _build_params(self, watch, page: int = 1) -> Dict[str, Any]:
        """Build API query params from a watch definition."""
        params: Dict[str, Any] = {
            "make": watch["make"],
            "page": page,
            "limit": 50,
        }
        if watch["model"]:
            params["model"] = watch["model"]
        if watch["year_min"]:
            params["year_min"] = watch["year_min"]
        if watch["year_max"]:
            params["year_max"] = watch["year_max"]
        if watch["price_max"]:
            params["price_max"] = watch["price_max"]
        if watch["price_min"]:
            params["price_min"] = watch["price_min"]
        if watch["mileage_max"]:
            params["mileage_max"] = watch["mileage_max"]
        if watch["zip_code"] and watch["radius"]:
            params["zip"] = watch["zip_code"]
            params["radius"] = watch["radius"]
        return params

    def _trim_matches(self, record: Dict, watch) -> bool:
        """Check if a record's trim contains the watch's trim filter."""
        # watch may be a sqlite3.Row (no .get), so use bracket access with default
        try:
            trim_filter = (watch["trim_contains"] or "").strip().lower()
        except (KeyError, IndexError):
            trim_filter = ""
        if not trim_filter:
            return True
        trim_value = (record.get("trim") or "").lower()
        return trim_filter in trim_value

    def search_watch(self, watch, max_pages: int = 3) -> Iterable[Dict]:
        """Run one saved watch, paginating within budget."""
        for page in range(1, max_pages + 1):
            params = self._build_params(watch, page)
            payload = self.search(params)
            if not payload:
                return

            records = payload.get("records") or []
            for raw in records:
                normalized = self._normalize(raw)
                if normalized and self._trim_matches(normalized, watch):
                    yield normalized

            if len(records) < 50:
                return

    def test_watch(self, watch) -> Dict[str, Any]:
        """One probe call to validate a watch. Returns count and sample.

        Costs exactly 1 API call. Used by the 'Test' button in the UI.
        """
        params = self._build_params(watch, page=1)
        params["limit"] = 5  # Only need a few to confirm it works

        payload = self.search(params)
        if payload is None:
            return {"ok": False, "error": "API call failed (no key or network error)",
                    "count": 0, "sample": []}

        records = payload.get("records") or []
        total = payload.get("totalCount") or payload.get("total") or len(records)

        # Apply trim filter to the sample
        trim_filter = (watch.get("trim_contains") or "").strip().lower()
        sample = []
        for raw in records:
            normalized = self._normalize(raw)
            if normalized:
                if trim_filter and trim_filter not in (normalized.get("trim") or "").lower():
                    continue
                sample.append(normalized)

        note = ""
        if trim_filter:
            note = (f"API returned ~{total} total; trim filter '{trim_filter}' "
                    f"applied client-side (matched {len(sample)}/{len(records)} in sample).")

        return {"ok": True, "error": "", "count": total,
                "sample": sample[:3], "note": note}

    @staticmethod
    def _normalize(raw: Dict) -> Optional[Dict]:
        vin = str(raw.get("vin") or "").strip().upper()
        if len(vin) != 17:
            return None

        # Price arrives as "$52,536", "accepting_offers", or a number.
        price_raw = raw.get("price")
        price: Optional[int] = None
        accepting = False
        if price_raw is not None:
            text = str(price_raw).replace("$", "").replace(",", "").strip()
            if "accepting" in text.lower() or not text:
                accepting = True
            else:
                try:
                    value = int(float(text))
                    if value > 500:
                        price = value
                except ValueError:
                    accepting = True

        mileage = raw.get("mileage") or raw.get("odometer")
        if isinstance(mileage, str):
            digits = "".join(c for c in mileage if c.isdigit())
            mileage = int(digits) if digits else None

        dealer = raw.get("dealer") or {}
        if not isinstance(dealer, dict):
            dealer = {}

        url = str(raw.get("vdpUrl") or raw.get("clickoffUrl")
                  or raw.get("url") or "")
        # API-relative links become absolute so they open from the dashboard.
        if url.startswith("/"):
            url = "https://auto.dev" + url

        return {
            "vin": vin,
            "year": raw.get("year"),
            "make": raw.get("make"),
            "model": raw.get("model"),
            "trim": raw.get("trim"),
            "mileage": mileage,
            "price": price,
            "accepting_offers": accepting,
            "dealer_name": (raw.get("dealerName")
                            or dealer.get("name") or ""),
            "city": raw.get("city") or dealer.get("city") or "",
            "state": raw.get("state") or dealer.get("state") or "",
            "listing_url": url,
        }
