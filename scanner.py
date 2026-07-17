#!/usr/bin/env python3
"""Fare Radar v4.

Scrapes current public deal-listing pages instead of broken RSS endpoints, writes
``deals.json`` for GitHub Pages, and emails only newly discovered matching posts.
It is deliberately conservative: a deal post is a discovery lead, not proof that
inventory is still bookable.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import smtplib
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

AIRPORTS = {
    "YYJ": ["yyj", "victoria"],
    "YVR": ["yvr", "vancouver"],
    "YXX": ["yxx", "abbotsford"],
    "BLI": ["bli", "bellingham"],
    "SEA": ["sea", "seattle"],
    "YYC": ["yyc", "calgary"],
    "YEG": ["yeg", "edmonton"],
    "YYZ": ["yyz", "toronto"],
}
REGIONS = {
    "Europe": ["europe", "paris", "london", "dublin", "amsterdam", "rome", "madrid", "barcelona", "lisbon", "porto", "faro", "frankfurt", "munich", "zurich", "vienna", "athens", "prague", "copenhagen", "stockholm", "oslo", "helsinki", "reykjavik", "brussels", "milan", "malaga", "edinburgh", "warsaw", "budapest", "zagreb", "istanbul", "france", "italy", "spain", "germany", "portugal", "greece", "iceland", "ireland", "netherlands", "switzerland", "croatia"],
    "India": ["india", "delhi", "mumbai", "bangalore", "bengaluru", "hyderabad", "chennai", "kolkata", "amritsar", "ahmedabad", "kochi", "goa", "colombo", "kathmandu", "dhaka", "lahore", "islamabad", "seychelles"],
    "Asia": ["japan", "tokyo", "osaka", "seoul", "korea", "china", "hong kong", "taipei", "taiwan", "bangkok", "thailand", "singapore", "vietnam", "hanoi", "manila", "philippines", "bali", "indonesia", "malaysia", "auckland", "new zealand"],
    "Mexico": ["mexico", "leon", "cancun", "puerto vallarta", "los cabos", "cabo", "caribbean", "jamaica", "dominican", "punta cana", "cuba", "aruba", "barbados", "costa rica", "belize"],
    "USA": ["united states", "usa", "new york", "los angeles", "san francisco", "las vegas", "hawaii", "honolulu", "orlando", "miami", "chicago", "boston", "washington", "seattle", "fort lauderdale", "oklahoma", "rome, italy"],
}

# Public listing pages. The previous /feed endpoints currently return malformed
# non-feed HTML and caused every source to fail.
SOURCES = [
    {"name": "YVR Deals", "url": "https://yvrdeals.com/", "airport": "YVR", "currency": "CAD"},
    {"name": "YYC Deals", "url": "https://www.yycdeals.com/", "airport": "YYC", "currency": "CAD"},
    {"name": "YYZ Deals", "url": "https://www.yyzdeals.com/", "airport": "YYZ", "currency": "CAD"},
    {"name": "Secret Flying Canada", "url": "https://www.secretflying.com/canada-flight-deals/", "airport": None, "currency": None},
    {"name": "Secret Flying Vancouver", "url": "https://www.secretflying.com/cheap-flights-from/Vancouver/", "airport": "YVR", "currency": "CAD"},
    {"name": "Secret Flying Seattle", "url": "https://www.secretflying.com/cheap-flights-from/Seattle/", "airport": "SEA", "currency": "USD"},
]

# Headline caps are deliberately stricter for expensive repositioning airports.
CAD_CAPS = {
    "YYJ": {"Europe": 850, "India": 1200, "Asia": 950, "Mexico": 600, "USA": 400},
    "YVR": {"Europe": 800, "India": 1100, "Asia": 900, "Mexico": 550, "USA": 400},
    "YXX": {"Europe": 720, "India": 1000, "Asia": 800, "Mexico": 500, "USA": 320},
    "YYC": {"Europe": 650, "India": 950, "Asia": 750, "Mexico": 450, "USA": 320},
    "YEG": {"Europe": 650, "India": 950, "Asia": 750, "Mexico": 450, "USA": 320},
    "YYZ": {"Europe": 525, "India": 850, "Asia": 700, "Mexico": 360, "USA": 260},
}
USD_CAPS = {"SEA": {"Europe": 500, "India": 750, "Asia": 650, "Mexico": 320, "USA": 220}, "BLI": {"Europe": 500, "India": 750, "Asia": 650, "Mexico": 320, "USA": 220}}
MAX_POST_AGE_DAYS = 14
MAX_CANDIDATES_PER_SOURCE = 18
MAX_PUBLIC_DEALS = 100
MAX_SEEN = 2000
SEEN_FILE = Path("seen.json")
DEALS_FILE = Path("deals.json")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/142 Safari/537.36 FareRadar/4.0",
    "Accept-Language": "en-CA,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def fetch_html(url: str) -> str:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = SESSION.get(url, timeout=35, allow_redirects=True)
            response.raise_for_status()
            if len(response.text) < 500:
                raise RuntimeError(f"unexpectedly short response ({len(response.text)} bytes)")
            return response.text
        except Exception as exc:
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(str(last_error or "request failed"))


def word_hit(text: str, keyword: str) -> bool:
    return bool(re.search(r"(?<![a-z0-9])" + re.escape(keyword.lower()) + r"(?![a-z0-9])", text.lower()))


def detect_airport(text: str, hint: str | None = None) -> str | None:
    low = text.lower()
    for code, words in AIRPORTS.items():
        if any(word_hit(low, word) for word in words):
            return code
    return hint


def detect_region(text: str) -> str:
    low = text.lower()
    for region, words in REGIONS.items():
        if any(word in low for word in words):
            return region
    return "Anywhere"


def detect_kind(text: str) -> str:
    low = text.lower()
    if any(x in low for x in ["aeroplan", "award space", "points", "miles", "membership rewards"]):
        return "points"
    if any(x in low for x in ["business class", "lie-flat", "lie flat", "first class", "saga premium"]):
        return "business"
    return "cash"


def is_error_fare(text: str) -> bool:
    low = text.lower()
    return any(x in low for x in ["error fare", "mistake fare", "pricing error", "fuel dump"])


def extract_price(text: str, source_currency: str | None) -> tuple[int | None, str | None, str]:
    # Preserve ranges in the display but use the lowest figure for filtering.
    cad_range = re.search(r"(?i)\$\s*([\d,]{2,6})\s*(?:to|[-–])\s*\$?\s*([\d,]{2,6})\s*CAD", text)
    if cad_range:
        lo, hi = (int(x.replace(",", "")) for x in cad_range.groups())
        return min(lo, hi), "CAD", f"${lo:,}–${hi:,} CAD"
    explicit = re.search(r"(?i)(?:CAD\s*)?\$\s*([\d,]{2,6})(?:\s*CAD)?", text)
    if explicit:
        amount = int(explicit.group(1).replace(",", ""))
        currency = "CAD" if "cad" in explicit.group(0).lower() or source_currency == "CAD" else source_currency
        return amount, currency, f"${amount:,}" + (f" {currency}" if currency else " (source currency)")
    euro = re.search(r"([€£])\s*([\d,]{2,6})", text)
    if euro:
        amount = int(euro.group(2).replace(",", ""))
        currency = "EUR" if euro.group(1) == "€" else "GBP"
        return amount, currency, f"{euro.group(1)}{amount:,}"
    iso = re.search(r"(?i)([\d,]{2,6})\s*(CAD|USD|EUR|GBP)\b", text)
    if iso:
        amount = int(iso.group(1).replace(",", ""))
        currency = iso.group(2).upper()
        return amount, currency, f"{amount:,} {currency}"
    return None, None, "SEE SOURCE"


def looks_like_deal_title(text: str) -> bool:
    low = text.lower()
    route = " to " in low or "→" in text or "open-jaw" in low
    price = bool(re.search(r"(?:C?\$|€|£)\s*[\d,]{2,6}", text))
    return 18 <= len(text) <= 240 and route and price and not any(x in low for x in ["hotel", "vacation package", "car rental"])


def listing_candidates(source: dict[str, Any]) -> list[tuple[str, str]]:
    soup = BeautifulSoup(fetch_html(source["url"]), "html.parser")
    candidates: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    source_host = urlparse(source["url"]).netloc.replace("www.", "")
    for anchor in soup.select("a[href]"):
        title = clean_text(anchor.get_text(" ", strip=True))
        if not looks_like_deal_title(title):
            continue
        url = urljoin(source["url"], anchor.get("href", ""))
        host = urlparse(url).netloc.replace("www.", "")
        if source_host not in host or url in seen_urls:
            continue
        seen_urls.add(url)
        candidates.append((title, url))
        if len(candidates) >= MAX_CANDIDATES_PER_SOURCE:
            break
    return candidates


def parse_posted(soup: BeautifulSoup) -> datetime | None:
    for attrs in [
        {"property": "article:published_time"},
        {"name": "article:published_time"},
        {"itemprop": "datePublished"},
    ]:
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            try:
                dt = dateparser.parse(tag["content"])
                return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except Exception:
                pass
    time_tag = soup.find("time", attrs={"datetime": True})
    if time_tag:
        try:
            dt = dateparser.parse(time_tag["datetime"])
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    # Both source families visibly print dates in Month D, YYYY form.
    text = clean_text(soup.get_text(" ", strip=True))
    for match in re.finditer(r"(?i)\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+([0-3]?\d)(?:st|nd|rd|th)?[,]?\s+(20\d{2})\b", text):
        try:
            dt = dateparser.parse(match.group(0), fuzzy=True).replace(tzinfo=timezone.utc)
            age = utcnow() - dt
            if timedelta(days=-1) <= age <= timedelta(days=MAX_POST_AGE_DAYS + 5):
                return dt
        except Exception:
            pass
    return None


def parse_detail(fallback_title: str, url: str) -> tuple[str, str, datetime | None]:
    soup = BeautifulSoup(fetch_html(url), "html.parser")
    heading = soup.find("h1")
    title = clean_text(heading.get_text(" ", strip=True)) if heading else fallback_title
    if not looks_like_deal_title(title):
        title = fallback_title
    posted = parse_posted(soup)
    page_text = clean_text(soup.get_text(" ", strip=True))
    # Keep a useful but short availability hint when present.
    note = "Published fare lead. Confirm dates, baggage and final price on the airline checkout."
    availability = re.search(r"(?i)Availability for travel\s+(.{0,230}?)(?:How to find|How to book|Google Flights|Sign up|$)", page_text)
    if availability:
        hint = clean_text(availability.group(1))[:220]
        if hint:
            note = f"Travel availability: {hint}. Confirm live inventory before paying."
    return title, note, posted


def should_include(airport: str, region: str, kind: str, error: bool, amount: int | None, currency: str | None) -> bool:
    if error or kind in {"points", "business"}:
        return True
    if amount is None or region == "Anywhere":
        return False
    if currency == "CAD":
        cap = CAD_CAPS.get(airport, {}).get(region)
        return cap is not None and amount <= cap
    if currency == "USD":
        cap = USD_CAPS.get(airport, {}).get(region)
        return cap is not None and amount <= cap
    return False


def deal_id(url: str, title: str) -> str:
    return hashlib.sha256((url or title).encode("utf-8")).hexdigest()[:20]


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def scan() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    seen = load_json(SEEN_FILE, [])
    seen_set = set(str(x) for x in seen) if isinstance(seen, list) else set()
    previous = load_json(DEALS_FILE, {"deals": []}).get("deals", [])
    existing = {d.get("id"): d for d in previous if d.get("id")}
    new_matches: list[dict[str, Any]] = []
    errors: list[str] = []

    for source in SOURCES:
        try:
            candidates = listing_candidates(source)
            if not candidates:
                raise RuntimeError("no deal links found on listing page")
            for fallback_title, url in candidates:
                # Reject clearly irrelevant or ordinary listings before fetching
                # every detail page. This keeps scheduled traffic modest.
                preview_airport = detect_airport(fallback_title, source.get("airport"))
                preview_region = detect_region(fallback_title)
                preview_kind = detect_kind(fallback_title)
                preview_error = is_error_fare(fallback_title)
                preview_amount, preview_currency, _ = extract_price(fallback_title, source.get("currency"))
                if not preview_airport or not should_include(preview_airport, preview_region, preview_kind, preview_error, preview_amount, preview_currency):
                    continue
                try:
                    title, note, posted = parse_detail(fallback_title, url)
                except Exception as exc:
                    # Listing data remains usable even if one detail page blocks us.
                    title, note, posted = fallback_title, "Current listing-page deal. Confirm live dates and price before booking.", utcnow()
                    print(f"DETAIL WARNING {source['name']}: {url}: {exc}")
                if posted is None:
                    # Only links currently present on the recent listing page reach here.
                    posted = utcnow()
                if utcnow() - posted > timedelta(days=MAX_POST_AGE_DAYS):
                    continue
                airport = detect_airport(title, source.get("airport"))
                if not airport:
                    continue
                region = detect_region(title)
                kind = detect_kind(title)
                error = is_error_fare(title)
                amount, currency, price_display = extract_price(title, source.get("currency"))
                if not should_include(airport, region, kind, error, amount, currency):
                    continue
                did = deal_id(url, title)
                deal = {
                    "id": did,
                    "airport": airport,
                    "region": region,
                    "kind": kind,
                    "error_fare": error,
                    "route": title[:180],
                    "title": title,
                    "price": amount,
                    "currency": currency,
                    "price_display": price_display,
                    "posted_at": posted.isoformat(),
                    "posted_display": posted.strftime("%b %d, %Y"),
                    "source": source["name"],
                    "url": url,
                    "note": note,
                }
                existing[did] = deal
                if did not in seen_set:
                    new_matches.append(deal)
                seen_set.add(did)
        except Exception as exc:
            errors.append(f"{source['name']}: {exc}")

    cutoff = utcnow() - timedelta(days=MAX_POST_AGE_DAYS)
    active: list[dict[str, Any]] = []
    for deal in existing.values():
        try:
            posted = datetime.fromisoformat(deal["posted_at"])
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            if posted >= cutoff:
                active.append(deal)
        except Exception:
            continue
    active.sort(key=lambda d: d.get("posted_at", ""), reverse=True)
    active = active[:MAX_PUBLIC_DEALS]
    save_json(SEEN_FILE, list(seen_set)[-MAX_SEEN:])
    return active, new_matches, errors


def email_new(deals: list[dict[str, Any]]) -> None:
    if not deals:
        print("No new matching deals; no email sent.")
        return
    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    recipient = os.environ.get("TO_EMAIL") or user
    if not user or not password or not recipient:
        print("Email secrets are not configured; skipping email.")
        return

    rows, text_rows = [], []
    for d in deals:
        rows.append(
            "<tr>"
            f"<td style='padding:10px;border-bottom:1px solid #ddd'><b>{html.escape(d['airport'])}</b></td>"
            f"<td style='padding:10px;border-bottom:1px solid #ddd'>{html.escape(d['title'])}<br><small>{html.escape(d['source'])} · {html.escape(d['posted_display'])}</small></td>"
            f"<td style='padding:10px;border-bottom:1px solid #ddd'><b>{html.escape(d['price_display'])}</b></td>"
            f"<td style='padding:10px;border-bottom:1px solid #ddd'><a href='{html.escape(d['url'])}'>Open</a></td>"
            "</tr>"
        )
        text_rows.append(f"[{d['airport']}] {d['title']} — {d['price_display']}\n{d['url']}")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Fare Radar: {len(deals)} new match{'es' if len(deals) != 1 else ''}"
    msg["From"] = user
    msg["To"] = recipient
    msg.attach(MIMEText("\n\n".join(text_rows) + "\n\nVerify before booking.", "plain", "utf-8"))
    msg.attach(MIMEText("<h2>Fare Radar</h2><p>New matching fare leads:</p><table style='border-collapse:collapse;width:100%'>" + "".join(rows) + "</table><p><small>Verify live price and dates before booking.</small></p>", "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(user, password)
        smtp.sendmail(user, [recipient], msg.as_string())
    print(f"Email sent with {len(deals)} new matches.")


def main() -> int:
    deals, new_matches, errors = scan()
    now = utcnow()
    output = {
        "generated_at": now.isoformat(),
        "generated_display": now.strftime("%b %d, %Y %H:%M UTC"),
        "deal_count": len(deals),
        "source_errors": errors,
        "deals": deals,
    }
    save_json(DEALS_FILE, output)
    email_new(new_matches)
    print(f"active={len(deals)} new={len(new_matches)} source_errors={len(errors)}")
    for error in errors:
        print("SOURCE ERROR:", error)
    # Do not fail the whole Pages deployment because one source changed its HTML.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())