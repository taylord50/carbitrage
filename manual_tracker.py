"""Manual URL tracking for listings the API does not carry.

For exotic and private-sale listings (broker sites, Bring a Trailer, dealer
pages for cars that never hit the aggregators), the URL itself is the tracking
handle. Each daily run fetches the page, pulls out a price and VIN if present,
and marks the listing gone when the page 404s or reads as sold.

Plain HTTP only, one polite request per URL per run. If a site blocks
automated requests the status shows 'error' and the listing simply is not
updated; nothing retries aggressively.
"""

import logging
import re
from typing import Dict, Optional

import requests

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

PRICE_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d{4,7})(?!\d)")
VIN_RE = re.compile(r"\b([A-HJ-NPR-Z0-9]{17})\b")
SOLD_RE = re.compile(
    r"\b(sold|no longer available|listing (?:has )?ended|off market|"
    r"vehicle (?:has been )?removed|auction ended)\b", re.IGNORECASE)

#: Two-decade VIN year codes for a rough year guess from a found VIN.
VIN_YEAR = {"L": 2020, "M": 2021, "N": 2022, "P": 2023, "R": 2024,
            "S": 2025, "T": 2026}


def check_url(url: str, known_vin: str = "") -> Dict:
    """Fetch one tracked URL and report what the page says.

    Returns a dict with:
        status: ok | gone | error
        price: int or None
        vin: str or ''
        title: page title text
    """
    try:
        response = requests.get(
            url, timeout=20, allow_redirects=True,
            headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    except requests.RequestException as exc:
        return {"status": "error", "detail": type(exc).__name__,
                "price": None, "vin": known_vin, "title": ""}

    if response.status_code in (404, 410):
        return {"status": "gone", "detail": f"HTTP {response.status_code}",
                "price": None, "vin": known_vin, "title": ""}
    if response.status_code != 200:
        return {"status": "error", "detail": f"HTTP {response.status_code}",
                "price": None, "vin": known_vin, "title": ""}

    body = response.text
    text = re.sub(r"<script.*?</script>", " ", body, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)

    title_match = re.search(r"<title[^>]*>(.*?)</title>", body,
                            re.DOTALL | re.IGNORECASE)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""

    # Sold language near the top of the page is a strong signal; deep in the
    # page it is usually a related-listings module.
    if SOLD_RE.search(text[:5000]):
        return {"status": "gone", "detail": "page reads as sold",
                "price": None, "vin": known_vin, "title": title}

    # VIN: prefer the one we were given; otherwise first plausible one found.
    vin = known_vin
    if not vin:
        for match in VIN_RE.finditer(text):
            candidate = match.group(1)
            if any(c.isdigit() for c in candidate) and any(c.isalpha() for c in candidate):
                vin = candidate
                break

    # Price: the largest dollar figure on the page is usually the asking price
    # on a listing page (smaller ones are payments and fees).
    price = None
    amounts = []
    for match in PRICE_RE.finditer(text):
        try:
            amounts.append(int(match.group(1).replace(",", "")))
        except ValueError:
            continue
    plausible = [a for a in amounts if 2000 <= a <= 3000000]
    if plausible:
        price = max(plausible)

    return {"status": "ok", "detail": "", "price": price, "vin": vin,
            "title": title}


def title_to_fields(title: str) -> Dict:
    """Best-effort year/make/model from a page title like
    '2021 Rolls-Royce Cullinan for sale - Exotic Motors'."""
    out: Dict = {}
    match = re.search(r"\b(19[89]\d|20[0-3]\d)\b", title)
    if match:
        out["year"] = int(match.group(1))
    return out
