"""Deal-feed backend: track Zephyrus deals without a Best Buy API key.

Best Buy will not issue API keys to free email providers, which leaves the
official backend out of reach for a lot of people. This module reads public
RSS/Atom deal feeds instead -- Slickdeals search feeds by default, plus any
extra feed URLs you configure.

The trade-off is real and worth stating plainly:

* It sees deals *people post*, not Best Buy's inventory. Coverage is partial.
* There is no per-store data at all, so Boston pickup stock is not available.
* List prices are often missing, so a percent-off figure frequently is too.

What it does do well is catch open-box and heavily discounted Zephyrus
listings shortly after someone spots them, which is the bulk of the value.
"""

from __future__ import annotations

import gzip
import html
import io
import logging
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .models import NEW, Offer, Product

log = logging.getLogger(__name__)

SLICKDEALS_SEARCH = (
    "https://slickdeals.net/newsearch.php"
    "?q={query}&searcharea=deals&searchin=first&rss=1"
)

#: Condition keywords, checked in order -- first match wins.
CONDITION_PATTERNS = [
    ("openbox-listed", re.compile(r"\bopen[\s-]?box(?:es)?\b", re.I)),
    ("refurbished", re.compile(r"\brefurb(?:ished)?\b|\brenewed\b|\brecertified\b", re.I)),
    ("pre-owned", re.compile(r"\bpre[\s-]?owned\b|\bused\b", re.I)),
]

#: Retailers we can recognise from a link or body text.
RETAILER_PATTERNS = [
    ("Best Buy", re.compile(r"bestbuy\.com|\bbest\s?buy\b", re.I)),
    ("Amazon", re.compile(r"amazon\.com|\bamazon\b", re.I)),
    ("Newegg", re.compile(r"newegg\.com|\bnewegg\b", re.I)),
    ("Walmart", re.compile(r"walmart\.com|\bwalmart\b", re.I)),
    ("Costco", re.compile(r"costco\.com|\bcostco\b", re.I)),
    ("B&H", re.compile(r"bhphotovideo\.com|\bb&h\b", re.I)),
    ("Micro Center", re.compile(r"microcenter\.com|\bmicro\s?center\b", re.I)),
    ("eBay", re.compile(r"ebay\.com|\bebay\b", re.I)),
    ("ASUS", re.compile(r"asus\.com", re.I)),
]

PRICE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")

#: "was $2,499", "reg. $2499.99", "list price $2499" -- a regular price, if stated.
WAS_PRICE = re.compile(
    r"(?:was|reg(?:ular)?\.?|list(?:\s+price)?|retail|orig(?:inally)?\.?|msrp)"
    r"\D{0,12}\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)",
    re.I,
)

SLICKDEALS_THREAD = re.compile(r"slickdeals\.net/f/(\d+)")

#: Slickdeals states liveness on the thread page as isExpiredDeal":"Yes"/"No".
EXPIRED_FLAG = re.compile(r'isExpiredDeal"\s*:\s*("?[A-Za-z]+"?)')


@dataclass
class Deal:
    """One posting from a feed."""

    uid: str                       # stable id, used as the tracked "sku"
    title: str
    url: str
    price: float | None = None
    list_price: float | None = None
    condition: str = NEW
    retailer: str = ""
    image: str = ""
    posted_at: datetime | None = None
    source: str = ""
    #: True when the source says the deal is dead, False when it says it is
    #: live, None when we have not checked or cannot tell.
    expired: bool | None = None
    raw_title: str = field(default="", repr=False)

    def as_product(self) -> Product:
        name = self.title
        if self.retailer:
            name = f"{name} [{self.retailer}]"
        return Product(
            sku=self.uid,
            name=name,
            model_number=_model_of(self.raw_title or self.title),
            url=self.url,
            image=self.image,
            manufacturer="ASUS",
            posted_at=self.posted_at.date().isoformat() if self.posted_at else "",
            expired=bool(self.expired),
        )

    def as_offer(self) -> Offer:
        return Offer(
            sku=self.uid,
            condition=self.condition,
            price=self.price,
            regular_price=self.list_price,
            # Feed search returns long-dead postings, so presence in the feed
            # says nothing about whether the deal can still be bought.
            available=self.expired is not True,
            url=self.url,
        )


class FeedError(RuntimeError):
    pass


class FeedClient:
    """Fetches and parses public deal feeds. No key, no account."""

    USER_AGENT = (
        "Mozilla/5.0 (compatible; zephyrus-price-tracker/1.0; "
        "+personal price tracking; contact: see repository)"
    )

    def __init__(self, *, timeout: int = 25, max_retries: int = 3,
                 min_interval: float = 1.5) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_interval = min_interval
        self._next_at = 0.0
        self._ssl = ssl.create_default_context()
        self.request_count = 0

    # ------------------------------------------------------------------ HTTP

    def fetch(self, url: str) -> str:
        """GET a feed with polite pacing and backoff."""
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            wait = self._next_at - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._next_at = time.monotonic() + self.min_interval
            self.request_count += 1

            req = urllib.request.Request(url, headers={
                "User-Agent": self.USER_AGENT,
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
                "Accept-Encoding": "gzip",
            })
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl) as resp:
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding", "") == "gzip":
                        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                    return raw.decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                # 429 is the common one: public feeds rate-limit by IP.
                if exc.code in (429, 500, 502, 503, 504):
                    last = FeedError(f"HTTP {exc.code} from {_host(url)}")
                else:
                    raise FeedError(f"HTTP {exc.code} from {_host(url)}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = exc

            if attempt < self.max_retries:
                delay = min(2 ** attempt, 8) * 2
                log.warning("Feed %s failed (%s); retrying in %ss", _host(url), last, delay)
                time.sleep(delay)

        raise FeedError(f"Gave up on {_host(url)}: {last}")

    # ------------------------------------------------------------- discovery

    def slickdeals(self, query: str) -> list[Deal]:
        url = SLICKDEALS_SEARCH.format(query=urllib.parse.quote(query))
        return self.parse(self.fetch(url), source=f"slickdeals:{query}")

    def feed(self, url: str) -> list[Deal]:
        return self.parse(self.fetch(url), source=_host(url))

    def check_expired(self, url: str) -> bool | None:
        """Is this posting dead? True/False, or None when we cannot tell.

        Slickdeals states it outright on the thread page. Feed *search* happily
        returns postings that expired months ago, so without this every dead
        deal looks live -- which is the whole point of the check.
        """
        if not SLICKDEALS_THREAD.search(url or ""):
            return None
        try:
            page = self.fetch(url)
        except FeedError as exc:
            log.warning("Liveness check failed for %s: %s", _host(url), exc)
            return None
        match = EXPIRED_FLAG.search(page)
        if not match:
            return None
        return match.group(1).strip('"').lower() == "yes"

    # ----------------------------------------------------------------- parse

    @staticmethod
    def parse(xml_text: str, *, source: str = "") -> list[Deal]:
        """Parse an RSS 2.0 or Atom document into Deals."""
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise FeedError(f"Feed from {source or 'unknown'} is not valid XML: {exc}") from exc

        nodes = root.findall(".//item") or root.findall(
            ".//{http://www.w3.org/2005/Atom}entry")
        deals: list[Deal] = []
        for node in nodes:
            deal = _deal_from_node(node, source)
            if deal is not None:
                deals.append(deal)
        return deals


# --------------------------------------------------------------------- parsing


def _text(node: ET.Element, *names: str) -> str:
    for name in names:
        found = node.find(name)
        if found is not None:
            if found.text:
                return found.text.strip()
            href = found.get("href")
            if href:
                return href.strip()
    return ""


def _deal_from_node(node: ET.Element, source: str) -> Deal | None:
    atom = "{http://www.w3.org/2005/Atom}"
    title = html.unescape(_text(node, "title", f"{atom}title"))
    link = html.unescape(_text(node, "link", f"{atom}link"))
    if not title or not link:
        return None

    body_parts = [
        _text(node, "description", f"{atom}summary", f"{atom}content"),
        _text(node, "{http://purl.org/rss/1.0/modules/content/}encoded"),
    ]
    body = html.unescape(" ".join(p for p in body_parts if p))

    price = _last_price(title) or _last_price(body)
    list_price = _list_price(title) or _list_price(body)
    # A "was" price below the deal price is a mis-parse -- drop it rather than
    # report a negative discount.
    if price is not None and list_price is not None and list_price <= price:
        list_price = None

    return Deal(
        uid=_uid(link, title, source),
        title=_clean_title(title),
        url=_clean_url(link),
        price=price,
        list_price=list_price,
        condition=_condition(f"{title} {body}"),
        retailer=_retailer(f"{link} {body} {title}"),
        image=_image(body),
        posted_at=_posted(node, atom),
        source=source,
        raw_title=title,
    )


def _last_price(text: str) -> float | None:
    """The deal price. Feed titles conventionally end with it."""
    matches = PRICE.findall(text or "")
    if not matches:
        return None
    try:
        return float(matches[-1].replace(",", ""))
    except ValueError:
        return None


def _list_price(text: str) -> float | None:
    match = WAS_PRICE.search(text or "")
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _condition(text: str) -> str:
    for condition, pattern in CONDITION_PATTERNS:
        if pattern.search(text or ""):
            return condition
    return NEW


def _retailer(text: str) -> str:
    for name, pattern in RETAILER_PATTERNS:
        if pattern.search(text or ""):
            return name
    return ""


def _image(body: str) -> str:
    match = re.search(r'<img[^>]+src="([^"]+)"', body or "")
    return match.group(1) if match else ""


def _model_of(title: str) -> str:
    """Pull a model designator like G14 / G16 out of a title, if present."""
    match = re.search(r"\b(G1[468]|GA\d{3}[A-Z]{0,2}|GU\d{3}[A-Z]{0,2}|GX\d{3})\b", title or "", re.I)
    return match.group(1).upper() if match else ""


def _uid(link: str, title: str, source: str) -> str:
    """A stable id for the posting, so price edits track the same row."""
    thread = SLICKDEALS_THREAD.search(link)
    if thread:
        return f"sd-{thread.group(1)}"
    # Fall back to the URL path, which is stable for most feeds.
    path = urllib.parse.urlsplit(link).path.strip("/")
    if path:
        return f"{_host(link).split('.')[0]}-{abs(hash(path)) % (10 ** 10)}"
    return f"{source}-{abs(hash(title)) % (10 ** 10)}"


def _clean_title(title: str) -> str:
    """Drop the trailing price and tracking noise from a feed title."""
    cleaned = re.sub(r"\s*[-–—]?\s*\$\s?[\d,]+(?:\.\d{1,2})?\s*$", "", title).strip()
    # Posters also put the price up front: "$1575 FS BEST BUY ASUS - ROG ...".
    cleaned = re.sub(r"^\$\s?[\d,]+(?:\.\d{1,2})?\s*(?:FS\b)?\s*[:,-]?\s*", "",
                     cleaned, flags=re.I).strip()
    return (cleaned or title)[:180]


def _clean_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    keep = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query)
            if not k.startswith("utm_")]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(keep)))


def _posted(node: ET.Element, atom: str) -> datetime | None:
    raw = _text(node, "pubDate", f"{atom}updated", f"{atom}published")
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc or url
