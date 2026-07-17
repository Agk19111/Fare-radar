#!/usr/bin/env python3
"""Fare Radar v3.

Scans public flight-deal RSS feeds, applies conservative filters, writes deals.json
for the GitHub Pages dashboard, and emails only newly matched posts.

This is a deal-post scanner, not a live airline inventory engine.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import smtplib
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import feedparser

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
ACTIVE_AIRPORTS = list(AIRPORTS)

REGIONS = {
    "Europe": ["europe", "paris", "london", "dublin", "amsterdam", "rome", "madrid", "barcelona", "lisbon", "porto", "frankfurt", "munich", "zurich", "vienna", "athens", "prague", "copenhagen", "stockholm", "oslo", "helsinki", "reykjavik", "brussels", "milan", "venice", "warsaw", "budapest", "zagreb", "istanbul", "france", "italy", "spain", "germany", "portugal", "greece", "iceland", "ireland", "netherlands", "switzerland", "croatia"],
    "India": ["india", "delhi", "mumbai", "bangalore", "bengaluru", "hyderabad", "chennai", "kolkata", "amritsar", "ahmedabad", "kochi", "goa", "colombo", "kathmandu", "dhaka", "lahore", "islamabad"],
    "Asia": ["japan", "tokyo", "osaka", "seoul", "korea", "china", "hong kong", "taipei", "taiwan", "bangkok", "thailand", "singapore", "vietnam", "hanoi", "manila", "philippines", "bali", "indonesia", "malaysia"],
    "Mexico": ["mexico", "cancun", "puerto vallarta", "los cabos", "cabo", "caribbean", "jamaica", "dominican", "punta cana", "cuba", "aruba", "barbados"],
    "USA": ["united states", "new york", "los angeles", "san francisco", "las vegas", "hawaii", "honolulu", "orlando", "miami", "chicago", "boston", "washington dc"],
}

# Conservative headline price caps. They are only applied where the price is
# confidently interpreted as CAD. Error fares and points posts may have no cash price.
CAD_CAPS = {"Europe": 700, "India": 1150, "Asia": 850, "Mexico": 550, "USA": 400}
YYZ_MAX_CAD = 550
MAX_POST_AGE_HOURS = 96
MAX_PUBLIC_DEALS = 80
MAX_SEEN = 1500

FEEDS = [
    {"name": "YVR Deals", "url": "https://www.yvrdeals.com/feed", "currency": "CAD"},
    {"name": "YYC Deals", "url": "https://www.yycdeals.com/feed", "currency": "CAD"},
    {"name": "YYZ Deals", "url": "https://www.yyzdeals.com/feed", "currency": "CAD"},
    {"name": "Secret Flying", "url": "https://www.secretflying.com/feed/", "currency": None},
    {"name": "Secret Flying Canada", "url": "https://www.secretflying.com/canada-deals/feed/", "currency": None},
]

SEEN_FILE = Path("seen.json")
DEALS_FILE = Path("deals.json")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def word_hit(text: str, keyword: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(keyword.lower()) + r"(?![a-z0-9])", text.lower()) is not None


def detect_airport(text: str) -> str | None:
    for code in ACTIVE_AIRPORTS:
        if any(word_hit(text, k) for k in AIRPORTS[code]):
            return code
    return None


def detect_region(text: str) -> str:
    low = text.lower()
    for region, words in REGIONS.items():
        if any(w in low for w in words):
            return region
    return "Anywhere"


def detect_kind(text: str) -> str:
    low = text.lower()
    if any(x in low for x in ["aeroplan", "award space", "points", "miles", "membership rewards"]):
        return "points"
    if any(x in low for x in ["business class", "lie-flat", "lie flat", "first class"]):
        return "business"
    return "cash"


def is_error_fare(text: str) -> bool:
    low = text.lower()
    return any(x in low for x in ["error fare", "mistake fare", "pricing error", "fuel dump"])


def extract_price(title: str, summary: str, source_currency: str | None) -> tuple[int | None, str | None, str]:
    """Return numeric amount, currency code if known, and display string.

    Prefer a price in the title. Never silently label an ambiguous dollar sign as CAD.
    """
    patterns = [title, summary]
    for text in patterns:
        # Explicit ISO currency, e.g. CAD $605 / $605 CAD / 605 CAD
        m = re.search(r"(?i)(?:CAD\s*)?\$\s*([\d,]{2,6})(?:\s*CAD)?", text)
        if m:
            amount = int(m.group(1).replace(",", ""))
            explicit_cad = "cad" in m.group(0).lower() or source_currency == "CAD"
            currency = "CAD" if explicit_cad else source_currency
            display = f"${amount:,}" + (f" {currency}" if currency else " (source currency)")
            return amount, currency, display
        m = re.search(r"(?i)(€|£)\s*([\d,]{2,6})", text)
        if m:
            amount = int(m.group(2).replace(",", ""))
            currency = "EUR" if m.group(1) == "€" else "GBP"
            return amount, currency, f"{m.group(1)}{amount:,}"
        m = re.search(r"(?i)([\d,]{2,6})\s*(CAD|USD|EUR|GBP)\b", text)
        if m:
            amount = int(m.group(1).replace(",", ""))
            currency = m.group(2).upper()
            return amount, currency, f"{amount:,} {currency}"
    return None, None, "SEE SOURCE"


def entry_datetime(entry: Any) -> datetime | None:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return None
    try:
        return datetime(*t[:6], tzinfo=timezone.utc)
    except Exception:
        return None


def recent_enough(posted: datetime | None) -> bool:
    if posted is None:
        return False
    age = utcnow() - posted
    return timedelta(0) <= age <= timedelta(hours=MAX_POST_AGE_HOURS)


def should_include(airport: str, region: str, kind: str, error: bool, amount: int | None, currency: str | None) -> bool:
    if error or kind == "points":
        return True
    if airport == "YYZ" and (currency != "CAD" or amount is None or amount > YYZ_MAX_CAD):
        return False
    if kind == "business":
        return amount is None or currency != "CAD" or amount <= 2200
    cap = CAD_CAPS.get(region)
    if cap is None:
        return error
    # Unknown currencies are retained because the source may use USD/EUR; the page labels them honestly.
    return amount is None or currency != "CAD" or amount <= cap


def route_label(title: str) -> str:
    # Keep source title because guessing IATA destinations creates false information.
    return title[:150]


def deal_id(link: str, title: str) -> str:
    return hashlib.sha256((link or title).encode("utf-8")).hexdigest()[:20]


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def scan() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    seen = load_json(SEEN_FILE, [])
    if not isinstance(seen, list):
        seen = []
    seen_set = set(str(x) for x in seen)
    previous = load_json(DEALS_FILE, {"deals": []}).get("deals", [])
    existing = {d.get("id"): d for d in previous if d.get("id")}
    new_matches: list[dict[str, Any]] = []
    errors: list[str] = []

    for src in FEEDS:
        try:
            parsed = feedparser.parse(src["url"], request_headers={"User-Agent": "FareRadar/3.0 (+GitHub Actions)"})
            if getattr(parsed, "bozo", False) and not parsed.entries:
                raise RuntimeError(str(getattr(parsed, "bozo_exception", "invalid feed")))
            for entry in parsed.entries[:50]:
                title = clean_text(entry.get("title", ""))
                summary = clean_text(entry.get("summary", ""))[:1200]
                link = str(entry.get("link", "")).strip()
                if not title or not link:
                    continue
                did = deal_id(link, title)
                posted = entry_datetime(entry)
                if not recent_enough(posted):
                    seen_set.add(did)
                    continue
                text = f"{title} {summary}"
                airport = detect_airport(text)
                if not airport:
                    continue
                region = detect_region(text)
                kind = detect_kind(text)
                error = is_error_fare(text)
                amount, currency, price_display = extract_price(title, summary, src.get("currency"))
                if not should_include(airport, region, kind, error, amount, currency):
                    seen_set.add(did)
                    continue
                deal = {
                    "id": did,
                    "airport": airport,
                    "region": region,
                    "kind": kind,
                    "error_fare": error,
                    "route": route_label(title),
                    "title": title,
                    "price": amount,
                    "currency": currency,
                    "price_display": price_display,
                    "posted_at": posted.isoformat(),
                    "posted_display": posted.strftime("%b %d, %Y"),
                    "source": src["name"],
                    "url": link,
                    "note": "Published deal post. Confirm current travel dates, baggage and fare rules on the source and airline site.",
                }
                existing[did] = deal
                if did not in seen_set:
                    new_matches.append(deal)
                seen_set.add(did)
        except Exception as exc:
            errors.append(f"{src['name']}: {exc}")

    cutoff = utcnow() - timedelta(hours=MAX_POST_AGE_HOURS)
    active = []
    for d in existing.values():
        try:
            if datetime.fromisoformat(d["posted_at"]) >= cutoff:
                active.append(d)
        except Exception:
            pass
    active.sort(key=lambda d: d.get("posted_at", ""), reverse=True)
    active = active[:MAX_PUBLIC_DEALS]
    save_json(SEEN_FILE, list(seen_set)[-MAX_SEEN:])
    return active, new_matches, errors


def email_new(deals: list[dict[str, Any]]) -> None:
    if not deals:
        return
    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    recipient = os.environ.get("TO_EMAIL") or user
    if not user or not password or not recipient:
        print("Email secrets are not configured; skipping email.")
        return

    rows = []
    text_rows = []
    for d in deals:
        rows.append(f"<tr><td style='padding:10px;border-bottom:1px solid #ddd'><b>{html.escape(d['airport'])}</b></td><td style='padding:10px;border-bottom:1px solid #ddd'>{html.escape(d['title'])}<br><small>{html.escape(d['source'])} · {html.escape(d['posted_display'])}</small></td><td style='padding:10px;border-bottom:1px solid #ddd'><b>{html.escape(d['price_display'])}</b></td><td style='padding:10px;border-bottom:1px solid #ddd'><a href='{html.escape(d['url'])}'>Open</a></td></tr>")
        text_rows.append(f"[{d['airport']}] {d['title']} — {d['price_display']}\n{d['url']}")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Fare Radar: {len(deals)} new match{'es' if len(deals) != 1 else ''}"
    msg["From"] = user
    msg["To"] = recipient
    msg.attach(MIMEText("\n\n".join(text_rows) + "\n\nVerify before booking.", "plain", "utf-8"))
    msg.attach(MIMEText("<h2>Fare Radar</h2><p>New matching deal posts:</p><table style='border-collapse:collapse;width:100%'>" + "".join(rows) + "</table><p><small>Verify live price and dates before booking.</small></p>", "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(user, password)
        smtp.sendmail(user, [recipient], msg.as_string())


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
    for err in errors:
        print("SOURCE ERROR:", err)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
