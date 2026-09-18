"""Alert rules: decide what about an offer is worth waking someone up for."""

from __future__ import annotations

import sqlite3

from .models import Alert, Offer, NEW, REFURBISHED, PRE_OWNED

# Alert kinds, most to least urgent.
OPENBOX_RESTOCK = "openbox_restock"
ALL_TIME_LOW = "all_time_low"
HEAVY_DISCOUNT = "heavy_discount"
RESTOCK = "restock"
PRICE_DROP = "price_drop"
NEW_PRODUCT = "new_product"
BOSTON_PICKUP = "boston_pickup"

#: Higher wins when two alerts describe the same offer.
KIND_PRIORITY = {
    OPENBOX_RESTOCK: 60,
    ALL_TIME_LOW: 50,
    HEAVY_DISCOUNT: 40,
    RESTOCK: 35,
    PRICE_DROP: 30,
    NEW_PRODUCT: 20,
    BOSTON_PICKUP: 10,
}

#: Which kinds describe *stock* vs *price* -- one of each may fire per offer.
STOCK_KINDS = {OPENBOX_RESTOCK, RESTOCK, NEW_PRODUCT, BOSTON_PICKUP}

#: Minimum history points before "all-time low" means anything.
MIN_HISTORY_FOR_LOW = 3


def money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


class Detector:
    """Turns an offer plus its history into zero or more alerts."""

    def __init__(self, config, storage) -> None:
        self.cfg = config
        self.db = storage
        t = config.section("thresholds")
        self.heavy_pct = float(t.get("heavy_discount_pct", 15.0))
        self.openbox_pct = float(t.get("open_box_discount_pct", 12.0))
        self.drop_pct = float(t.get("price_drop_pct", 5.0))
        self.drop_dollars = float(t.get("price_drop_dollars", 75.0))
        self.min_drop = float(t.get("min_drop_dollars", 20.0))
        self.want_all_time_low = bool(t.get("alert_on_all_time_low", True))
        self.want_new_product = bool(t.get("alert_on_new_product", True))
        self.max_price = float(t.get("max_price", 0) or 0)
        self.include_unavailable = bool(config.get("alerts.include_unavailable", False))

    # ------------------------------------------------------------------ API

    def evaluate(self, offer: Offer, product_name: str, prev: sqlite3.Row | None,
                 *, product_is_new: bool) -> list[Alert]:
        """All alerts this offer earns, already collapsed to at most two."""
        candidates: list[Alert] = []
        candidates += self._stock_alerts(offer, product_name, prev, product_is_new)
        candidates += self._price_alerts(offer, product_name, prev)
        return self._collapse(candidates)

    def over_price_cap(self, offer: Offer) -> bool:
        return bool(self.max_price and offer.price is not None and offer.price > self.max_price)

    # ------------------------------------------------------------ stock rules

    def _stock_alerts(self, offer: Offer, name: str, prev: sqlite3.Row | None,
                      product_is_new: bool) -> list[Alert]:
        alerts: list[Alert] = []
        if not offer.available or self.over_price_cap(offer):
            return alerts

        second_hand = offer.is_open_box or offer.condition in (REFURBISHED, PRE_OWNED)
        was_absent = prev is None
        was_out_of_stock = prev is not None and not prev["available"]

        if second_hand and (was_absent or was_out_of_stock):
            verb = "is back in stock" if was_out_of_stock else "just appeared"
            alerts.append(self._alert(
                OPENBOX_RESTOCK, offer, name,
                f"{offer.label} {verb} at {money(offer.price)}"
                + (f" ({offer.discount_pct}% off {money(offer.regular_price)})"
                   if offer.discount_pct else ""),
                severity=2,
                dedupe_key=f"{OPENBOX_RESTOCK}:{offer.sku}:{offer.condition}",
            ))
        elif was_out_of_stock:
            alerts.append(self._alert(
                RESTOCK, offer, name,
                f"{offer.label} is back in stock at {money(offer.price)}",
                severity=1,
                dedupe_key=f"{RESTOCK}:{offer.sku}:{offer.condition}",
            ))
        elif was_absent and offer.condition == NEW and self.want_new_product and product_is_new:
            alerts.append(self._alert(
                NEW_PRODUCT, offer, name,
                f"New model now tracked: {money(offer.price)}",
                severity=1,
                dedupe_key=f"{NEW_PRODUCT}:{offer.sku}",
            ))

        # Local pickup is its own signal: the SKU may have been buyable online
        # for weeks before a Boston store actually stocked it.
        had_stores = bool(prev is not None and prev["store_count"])
        if offer.stores and not had_stores:
            alerts.append(self._alert(
                BOSTON_PICKUP, offer, name,
                f"{offer.label} now available for pickup near Boston: "
                + ", ".join(offer.store_names[:4]),
                severity=1,
                dedupe_key=f"{BOSTON_PICKUP}:{offer.sku}:{offer.condition}",
            ))
        return alerts

    # ------------------------------------------------------------ price rules

    def _price_alerts(self, offer: Offer, name: str, prev: sqlite3.Row | None) -> list[Alert]:
        alerts: list[Alert] = []
        if offer.price is None:
            return alerts
        if not offer.available and not self.include_unavailable:
            return alerts
        if self.over_price_cap(offer):
            return alerts

        # 1. All-time low, judged against everything we've recorded before now.
        if self.want_all_time_low:
            seen = self.db.observation_count(offer.sku, offer.condition)
            prior_low = self.db.lowest_price(offer.sku, offer.condition)
            if (seen >= MIN_HISTORY_FOR_LOW and prior_low is not None
                    and offer.price < prior_low - 0.01):
                alerts.append(self._alert(
                    ALL_TIME_LOW, offer, name,
                    f"Lowest price we've ever recorded for {offer.label}: "
                    f"{money(offer.price)} (previous low {money(prior_low)})",
                    severity=2,
                    dedupe_key=f"{ALL_TIME_LOW}:{offer.sku}:{offer.condition}:{offer.price:.2f}",
                ))

        # 2. Heavy discount off the regular list price.
        threshold = self.openbox_pct if (offer.is_open_box or offer.condition in (REFURBISHED, PRE_OWNED)) else self.heavy_pct
        pct = offer.discount_pct
        if pct is not None and pct >= threshold and (offer.discount_dollars or 0) >= self.min_drop:
            alerts.append(self._alert(
                HEAVY_DISCOUNT, offer, name,
                f"{offer.label} is {pct}% off -- {money(offer.price)}, "
                f"down {money(offer.discount_dollars)} from {money(offer.regular_price)}",
                severity=2 if pct >= threshold * 1.5 else 1,
                dedupe_key=f"{HEAVY_DISCOUNT}:{offer.sku}:{offer.condition}:{offer.price:.2f}",
            ))

        # 3. Meaningful move since the last time we looked.
        prev_price = prev["price"] if prev is not None else None
        if prev_price is not None and offer.price < prev_price:
            delta = prev_price - offer.price
            delta_pct = round(delta / prev_price * 100, 1) if prev_price else 0.0
            if delta >= self.min_drop and (delta_pct >= self.drop_pct or delta >= self.drop_dollars):
                alerts.append(self._alert(
                    PRICE_DROP, offer, name,
                    f"{offer.label} dropped {money(delta)} ({delta_pct}%) since last check: "
                    f"{money(prev_price)} -> {money(offer.price)}",
                    severity=1,
                    dedupe_key=f"{PRICE_DROP}:{offer.sku}:{offer.condition}:{offer.price:.2f}",
                ))
        return alerts

    # ------------------------------------------------------------- internals

    def _alert(self, kind: str, offer: Offer, product_name: str, message: str,
               *, severity: int, dedupe_key: str) -> Alert:
        return Alert(
            kind=kind,
            sku=offer.sku,
            condition=offer.condition,
            product_name=product_name,
            message=message,
            price=offer.price,
            regular_price=offer.regular_price,
            discount_pct=offer.discount_pct,
            url=offer.url,
            dedupe_key=dedupe_key,
            severity=severity,
            stores=offer.store_names,
        )

    @staticmethod
    def _collapse(candidates: list[Alert]) -> list[Alert]:
        """Keep the single best stock alert and the single best price alert.

        Without this a genuinely good deal fires three near-identical alerts
        (all-time low *and* heavy discount *and* price drop) for one offer.
        """
        best_stock: Alert | None = None
        best_price: Alert | None = None
        for alert in candidates:
            bucket = "stock" if alert.kind in STOCK_KINDS else "price"
            current = best_stock if bucket == "stock" else best_price
            if current is None or KIND_PRIORITY[alert.kind] > KIND_PRIORITY[current.kind]:
                if bucket == "stock":
                    best_stock = alert
                else:
                    best_price = alert
        return [a for a in (best_stock, best_price) if a is not None]


def filter_new_alerts(alerts: list[Alert], storage, cooldown_hours: int) -> list[Alert]:
    """Drop alerts already sent inside the cooldown window."""
    fresh = []
    for alert in alerts:
        if not storage.was_alerted_recently(alert.dedupe_key, cooldown_hours):
            fresh.append(alert)
    return fresh
