"""Scan orchestration: discover SKUs, price every condition, detect, notify."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .bestbuy import BestBuyClient, BestBuyError
from .feeds import FeedClient, FeedError
from .detect import Detector, filter_new_alerts
from .models import NEW, OPEN_BOX_PREFIX, PRE_OWNED, REFURBISHED, Alert, Offer, Product
from .notify import dispatch
from .storage import Storage

log = logging.getLogger(__name__)

#: Best Buy ``orderable`` values that mean "you can buy it right now".
ORDERABLE_OK = {"Available", "Preorder", "BackOrder"}

#: How stale the Boston store list may get before we re-resolve it.
STORE_REFRESH_HOURS = 24 * 7


@dataclass
class ScanResult:
    products: int = 0
    offers: list[Offer] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    suppressed: int = 0
    api_requests: int = 0
    duration_s: float = 0.0
    failed_channels: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: True when this scan only established a baseline and deliberately
    #: sent nothing -- the first scan has nothing to compare against.
    baseline: bool = False
    capped: int = 0


class AlertPipeline:
    """Shared tail of every scan: persist, detect, suppress, notify.

    Both backends -- the Best Buy API and the public deal feeds -- produce the
    same ``Product`` / ``Offer`` shapes, so everything downstream of discovery
    is identical and lives here.
    """

    def __init__(self, config, storage: Storage) -> None:
        self.cfg = config
        self.db = storage
        self.detector = Detector(config, storage)

    def process(self, products: dict[str, Product], offers: list[Offer],
                result: ScanResult, *, dry_run: bool, notifiers: list | None) -> None:
        cooldown = int(self.cfg.get("alerts.cooldown_hours", 24))
        max_alerts = int(self.cfg.get("alerts.max_per_scan", 12))

        # Nothing tracked yet means nothing to compare against, so the first
        # scan would otherwise fire an alert for every single listing.
        result.baseline = not self.db.get_products()

        seen_keys: set[tuple[str, str]] = set()
        candidates: list[Alert] = []

        with self.db.tx():
            for sku, product in products.items():
                is_new_product = self.db.upsert_product(product)
                for offer in [o for o in offers if o.sku == sku]:
                    seen_keys.add(offer.key)
                    prev = self.db.get_offer_state(offer.sku, offer.condition)
                    if not result.baseline:
                        candidates.extend(self.detector.evaluate(
                            offer, product.name, prev, product_is_new=is_new_product))
                    # Detection reads history, so persist only afterwards.
                    self.db.record_offer(offer, changed=_changed(prev, offer))
            self.db.mark_offers_gone(seen_keys)

        if result.baseline:
            log.info("First scan: recorded %d offers as a baseline, no alerts sent",
                     len(offers))
            return

        fresh = filter_new_alerts(candidates, self.db, cooldown)
        result.suppressed = len(candidates) - len(fresh)
        fresh.sort(key=lambda a: (-a.severity, a.product_name))

        # A flood usually means something upstream changed shape, not that 40
        # genuine deals landed at once. Cap it and say so.
        if max_alerts > 0 and len(fresh) > max_alerts:
            result.capped = len(fresh) - max_alerts
            fresh = fresh[:max_alerts]

        result.alerts = fresh
        if fresh and not dry_run:
            result.failed_channels = dispatch(notifiers or [], fresh)
            with self.db.tx():
                for alert in fresh:
                    self.db.record_alert(alert)


class Scanner(AlertPipeline):
    def __init__(self, config, storage: Storage, client: BestBuyClient) -> None:
        super().__init__(config, storage)
        self.client = client

    # ------------------------------------------------------------ store list

    def refresh_stores(self, *, force: bool = False) -> list[dict[str, Any]]:
        """Resolve Best Buy locations near the configured ZIP, cached for a week."""
        age = self.db.stores_age_hours()
        if not force and age is not None and age < STORE_REFRESH_HOURS and self.db.get_stores():
            return [dict(row) for row in self.db.get_stores()]

        postal = str(self.cfg.get("location.postal_code", "02108"))
        radius = int(self.cfg.get("location.radius_miles", 25))
        log.info("Resolving Best Buy stores within %d miles of %s", radius, postal)
        stores = self.client.stores_near(postal, radius)

        city_allow = {c.lower() for c in self.cfg.get("location.city_allowlist", []) or []}
        if city_allow:
            stores = [s for s in stores if str(s.get("city", "")).lower() in city_allow]

        self.db.save_stores(stores)
        return stores

    def tracked_store_ids(self) -> set[str]:
        """Store IDs whose stock we care about (explicit allowlist wins)."""
        allow = [str(s) for s in (self.cfg.get("location.store_ids") or [])]
        if allow:
            return set(allow)
        return {row["store_id"] for row in self.db.get_stores()}

    # -------------------------------------------------------------- discovery

    def discover_products(self) -> dict[str, tuple[Product, dict[str, Any]]]:
        """Every Zephyrus SKU the Products API knows about, keyed by SKU."""
        found: dict[str, tuple[Product, dict[str, Any]]] = {}
        manufacturer = str(self.cfg.get("search.manufacturer", "") or "").lower()

        for term in self.cfg.get("search.terms", ["zephyrus"]):
            expression = f"search={term}"
            log.info("Searching products: %s", expression)
            try:
                for raw in self.client.search_products(expression):
                    sku = str(raw.get("sku") or "").strip()
                    if not sku or sku in found or not self._matches(raw, manufacturer):
                        continue
                    found[sku] = (
                        Product(
                            sku=sku,
                            name=str(raw.get("name") or f"SKU {sku}"),
                            model_number=str(raw.get("modelNumber") or ""),
                            url=str(raw.get("url") or ""),
                            image=str(raw.get("image") or ""),
                            manufacturer=str(raw.get("manufacturer") or ""),
                        ),
                        raw,
                    )
            except BestBuyError as exc:
                log.error("Search %r failed: %s", term, exc)
                raise
        return found

    def _matches(self, raw: dict[str, Any], manufacturer: str) -> bool:
        """Keep real Zephyrus laptops; drop sleeves, warranties and lookalikes."""
        name = str(raw.get("name") or "").lower()
        maker = str(raw.get("manufacturer") or "").lower()

        includes = self.cfg.get("search.include_keywords", []) or []
        if includes and not _has_keyword(name, includes):
            return False

        excludes = self.cfg.get("search.exclude_keywords", []) or []
        if _has_keyword(name, excludes):
            return False

        # Accessories often carry the laptop's name, so also require the brand.
        if manufacturer and manufacturer not in maker and manufacturer not in name:
            return False

        models = self.cfg.get("search.models", []) or []
        if models and not _has_keyword(name, models):
            return False

        return True

    # ----------------------------------------------------------------- offers

    def build_offers(self, products: dict[str, tuple[Product, dict[str, Any]]]) -> list[Offer]:
        """One Offer per (SKU, condition), across new, refurbished and open-box."""
        offers: list[Offer] = []
        skus = list(products)

        # 1. The listed condition (new / refurbished / pre-owned) and its price.
        for sku, (_product, raw) in products.items():
            offers.append(self._listing_offer(sku, raw))

        # 2. Open-box tiers. SKUs absent from the response simply have no stock.
        try:
            open_box = self.client.open_box(skus)
        except BestBuyError as exc:
            log.error("Open-box lookup failed: %s", exc)
            open_box = {}

        for sku, entry in open_box.items():
            if sku not in products:
                continue
            baseline = _as_float(products[sku][1].get("regularPrice"))
            offers.extend(self._open_box_offers(sku, entry, baseline, products[sku][0].url))

        # 3. Which of those can actually be picked up near Boston today.
        self._attach_store_availability(offers, products)
        return offers

    def _listing_offer(self, sku: str, raw: dict[str, Any]) -> Offer:
        condition = str(raw.get("condition") or "New").strip().lower()
        if condition not in (NEW, REFURBISHED, PRE_OWNED):
            condition = {"pre owned": PRE_OWNED, "preowned": PRE_OWNED}.get(condition, condition or NEW)

        regular = _as_float(raw.get("regularPrice"))
        sale = _as_float(raw.get("salePrice"))
        price = sale if sale is not None else regular

        available = bool(
            raw.get("onlineAvailability")
            or raw.get("inStoreAvailability")
            or str(raw.get("orderable") or "") in ORDERABLE_OK
        )
        return Offer(
            sku=sku,
            condition=condition,
            price=price,
            regular_price=regular,
            available=available,
            url=str(raw.get("url") or ""),
        )

    def _open_box_offers(self, sku: str, entry: dict[str, Any],
                         baseline: float | None, fallback_url: str) -> list[Offer]:
        out: list[Offer] = []
        entry_regular = _as_float(_dig(entry, "prices", "regular"))
        entry_url = _dig(entry, "links", "web") or _dig(entry, "links", "product") or fallback_url

        for raw_offer in entry.get("offers") or []:
            tier = str(raw_offer.get("condition") or "").strip().lower().replace(" ", "-")
            if not tier:
                continue
            price = _as_float(_dig(raw_offer, "prices", "current"))
            if price is None:
                price = _as_float(_dig(raw_offer, "prices", "regular"))
            if price is None:
                continue

            # Discount is only meaningful against the *new* list price.
            regular = baseline or entry_regular or _as_float(_dig(raw_offer, "prices", "regular"))
            out.append(Offer(
                sku=sku,
                condition=f"{OPEN_BOX_PREFIX}{tier}",
                price=price,
                regular_price=regular,
                # Best Buy only returns open-box offers that are buyable.
                available=True,
                url=str(_dig(raw_offer, "links", "web") or entry_url or ""),
            ))
        return out

    def _attach_store_availability(self, offers: list[Offer],
                                   products: dict[str, tuple[Product, dict[str, Any]]]) -> None:
        """Fill in Boston-area pickup stock, one API call per SKU."""
        if not self.cfg.get("location.postal_code"):
            return
        tracked = self.tracked_store_ids()
        if not tracked:
            log.warning("No Boston-area stores resolved; skipping pickup availability")
            return

        postal = str(self.cfg.get("location.postal_code"))
        by_sku: dict[str, list[tuple[str, str, str, bool]]] = {}

        for sku in products:
            try:
                stores = self.client.product_store_availability(sku, postal)
            except BestBuyError as exc:
                log.warning("Store availability for %s failed: %s", sku, exc)
                continue
            nearby = []
            for store in stores:
                store_id = str(store.get("storeID") or store.get("storeId") or "")
                if store_id and store_id in tracked:
                    nearby.append((
                        store_id,
                        str(store.get("name") or ""),
                        str(store.get("city") or ""),
                        bool(store.get("lowStock")),
                    ))
            if nearby:
                by_sku[sku] = nearby

        # Pickup data is reported per SKU, which Best Buy scopes to the new
        # listing -- open-box units are tied to individual stores and aren't
        # exposed here, so only the listing offer gets store detail.
        for offer in offers:
            if not offer.is_open_box and offer.sku in by_sku:
                offer.stores = by_sku[offer.sku]

    # -------------------------------------------------------------------- run

    def run(self, *, dry_run: bool = False, notifiers: list | None = None) -> ScanResult:
        started = time.monotonic()
        result = ScanResult()
        error: str | None = None

        try:
            self.refresh_stores()
            products = self.discover_products()
            result.products = len(products)
            if not products:
                log.warning("No matching Zephyrus products found")

            offers = self.build_offers(products)
            result.offers = offers

            self.process({sku: prod for sku, (prod, _raw) in products.items()},
                         offers, result, dry_run=dry_run, notifiers=notifiers)

        except Exception as exc:  # recorded, re-raised to the caller
            error = f"{type(exc).__name__}: {exc}"
            result.errors.append(error)
            raise
        finally:
            result.duration_s = time.monotonic() - started
            result.api_requests = self.client.request_count
            self.db.record_scan(
                products=result.products,
                offers=len(result.offers),
                alerts=len(result.alerts),
                api_requests=result.api_requests,
                duration_s=result.duration_s,
                error=error,
            )
        return result


class FeedScanner(AlertPipeline):
    """Backend for when no Best Buy API key is available.

    Reads public deal feeds instead of Best Buy's API. It sees what people
    post rather than Best Buy's own inventory, and carries no store-level
    data at all -- see ``feeds.py`` for the full trade-off.
    """

    def __init__(self, config, storage: Storage, client: FeedClient) -> None:
        super().__init__(config, storage)
        self.client = client

    def discover(self) -> list:
        """Every matching deal across the configured queries and feed URLs."""
        seen: dict[str, object] = {}
        errors: list[str] = []

        for query in self.cfg.get("feeds.queries", ["zephyrus"]) or []:
            try:
                for deal in self.client.slickdeals(str(query)):
                    seen.setdefault(deal.uid, deal)
            except FeedError as exc:
                errors.append(f"slickdeals({query}): {exc}")
                log.error("Slickdeals query %r failed: %s", query, exc)

        for url in self.cfg.get("feeds.urls", []) or []:
            try:
                for deal in self.client.feed(str(url)):
                    seen.setdefault(deal.uid, deal)
            except FeedError as exc:
                errors.append(f"{url}: {exc}")
                log.error("Feed %s failed: %s", url, exc)

        if errors and not seen:
            raise FeedError("every configured feed failed: " + "; ".join(errors))

        return [d for d in seen.values() if self._wanted(d)]

    def _wanted(self, deal) -> bool:
        title = deal.title
        includes = self.cfg.get("search.include_keywords", []) or []
        if includes and not _has_keyword(title, includes):
            return False
        if _has_keyword(title, self.cfg.get("search.exclude_keywords", []) or []):
            return False

        models = self.cfg.get("search.models", []) or []
        if models and not _has_keyword(title, models):
            return False

        retailers = self.cfg.get("feeds.retailers", []) or []
        if retailers:
            wanted = {str(r).strip().lower() for r in retailers}
            # An unidentified retailer is dropped when a filter is set --
            # otherwise "Best Buy only" would quietly include everything.
            if deal.retailer.lower() not in wanted:
                return False

        if self.cfg.get("feeds.require_price", True) and deal.price is None:
            return False

        max_age = int(self.cfg.get("feeds.max_age_days", 60) or 0)
        if max_age and deal.posted_at:
            age = (datetime.now(timezone.utc) - deal.posted_at).days
            if age > max_age:
                return False

        return True

    def run(self, *, dry_run: bool = False, notifiers: list | None = None) -> ScanResult:
        started = time.monotonic()
        result = ScanResult()
        error: str | None = None
        try:
            deals = self.discover()
            products = {d.uid: d.as_product() for d in deals}
            offers = [d.as_offer() for d in deals]
            result.products = len(products)
            result.offers = offers
            if not deals:
                log.warning("No matching deals in any configured feed")

            self.process(products, offers, result, dry_run=dry_run, notifiers=notifiers)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            result.errors.append(error)
            raise
        finally:
            result.duration_s = time.monotonic() - started
            result.api_requests = self.client.request_count
            self.db.record_scan(
                products=result.products, offers=len(result.offers),
                alerts=len(result.alerts), api_requests=result.api_requests,
                duration_s=result.duration_s, error=error,
            )
        return result


# --------------------------------------------------------------------- helpers


def _has_keyword(text: str, keywords: list[str]) -> bool:
    """True if any keyword appears in text as a whole word.

    Substring matching is too blunt here: an exclude list containing "case"
    would otherwise throw away a title mentioning "showcase", and feed titles
    are long enough that such collisions are common.
    """
    lowered = (text or "").lower()
    for keyword in keywords:
        word = str(keyword).strip().lower()
        if not word:
            continue
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", lowered):
            return True
    return False


def _changed(prev, offer: Offer) -> bool:
    """True when price, availability or local stock moved since last scan."""
    if prev is None:
        return True
    return (
        _round(prev["price"]) != _round(offer.price)
        or bool(prev["available"]) != offer.available
        or int(prev["store_count"] or 0) != len(offer.stores)
    )


def _round(value) -> float | None:
    return None if value is None else round(float(value), 2)


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dig(data: Any, *keys: str) -> Any:
    """Safely walk nested dicts; the beta open-box shapes vary between SKUs."""
    node = data
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node
