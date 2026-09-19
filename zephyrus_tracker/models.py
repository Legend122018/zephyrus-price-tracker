"""Core value types shared across the tracker."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

# Condition buckets. Anything starting with "openbox-" is an open-box offer.
NEW = "new"
REFURBISHED = "refurbished"
PRE_OWNED = "pre-owned"

OPEN_BOX_PREFIX = "openbox-"

#: Human labels for the conditions Best Buy returns, best -> worst.
CONDITION_LABELS = {
    NEW: "New",
    REFURBISHED: "Geek Squad Refurbished",
    PRE_OWNED: "Pre-owned",
    "openbox-listed": "Open-Box",          # feed backend: tier not stated
    "openbox-excellent": "Open-Box: Excellent",
    "openbox-certified": "Open-Box: Certified",
    "openbox-satisfactory": "Open-Box: Satisfactory",
    "openbox-fair": "Open-Box: Fair",
}

CONDITION_ORDER = [
    NEW,
    REFURBISHED,
    "openbox-listed",
    "openbox-excellent",
    "openbox-certified",
    "openbox-satisfactory",
    "openbox-fair",
    PRE_OWNED,
]


def condition_label(condition: str) -> str:
    """Pretty name for a condition key, tolerant of conditions we've never seen."""
    if condition in CONDITION_LABELS:
        return CONDITION_LABELS[condition]
    if condition.startswith(OPEN_BOX_PREFIX):
        return "Open-Box: " + condition[len(OPEN_BOX_PREFIX):].replace("-", " ").title()
    return condition.replace("-", " ").title()


def condition_rank(condition: str) -> int:
    """Stable sort key so report rows always list conditions in the same order."""
    try:
        return CONDITION_ORDER.index(condition)
    except ValueError:
        return len(CONDITION_ORDER)


def is_open_box(condition: str) -> bool:
    return condition.startswith(OPEN_BOX_PREFIX)


#: States with no statewide sales tax. New Hampshire is the one that matters
#: from Boston: it is a 45-minute drive and saves 6.25% Massachusetts tax,
#: which on a $2,600 laptop beats most of the discounts this tracker watches.
NO_SALES_TAX_STATES = {"NH", "DE", "MT", "OR", "AK"}


def is_tax_free(region: str) -> bool:
    return (region or "").strip().upper() in NO_SALES_TAX_STATES


#: How confident we are that a listing can actually be bought right now.
LIVE = "live"              # source says not expired, and the posting is recent
UNVERIFIED = "unverified"  # source says not expired, but the posting is old
EXPIRED = "expired"        # source says the deal is dead


def liveness_state(expired: bool, posted_at: str, *, stale_after_days: int = 30,
                   today: str | None = None) -> str:
    """Classify a listing's buyability.

    Deal sites mark a posting expired when a human gets round to it, so
    "not expired" is weak evidence on an old posting and strong evidence on a
    fresh one. Rather than present both as "in stock", an old-but-unmarked
    posting is reported as UNVERIFIED -- the honest answer, given no retailer
    permits an automated stock check without its API.
    """
    if expired:
        return EXPIRED
    if not posted_at or stale_after_days <= 0:
        return LIVE
    import datetime as _dt
    try:
        posted = _dt.date.fromisoformat(posted_at[:10])
    except ValueError:
        return LIVE
    now = _dt.date.fromisoformat(today) if today else _dt.date.today()
    return UNVERIFIED if (now - posted).days > stale_after_days else LIVE


@dataclass
class Product:
    """A tracked SKU, independent of any particular offer on it."""

    sku: str
    name: str
    model_number: str = ""
    url: str = ""
    image: str = ""
    manufacturer: str = ""
    #: When the listing was published at its source (ISO date), where the
    #: backend knows it. Distinct from first_seen, which is when WE saw it --
    #: a feed posting can be months old the first time we read it.
    posted_at: str = ""
    #: True when the source says the deal is dead. Feed search returns long
    #: expired postings, so this is what separates buyable from historical.
    expired: bool = False
    #: Which backend produced this row ("feeds" or "bestbuy"). Switching
    #: backends leaves the other one's rows behind, and they must not be
    #: presented as current -- nothing is scanning them any more.
    source: str = ""


@dataclass
class Offer:
    """One purchasable configuration: a SKU in a particular condition.

    ``(sku, condition)`` is the identity we track price history against.
    """

    sku: str
    condition: str
    price: float | None = None
    regular_price: float | None = None
    available: bool = False
    url: str = ""
    #: Nearby stores holding stock, as
    #: ``[(store_id, name, city, region, low_stock)]``.
    stores: list[tuple[str, str, str, str, bool]] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        return (self.sku, self.condition)

    @property
    def is_open_box(self) -> bool:
        return is_open_box(self.condition)

    @property
    def label(self) -> str:
        return condition_label(self.condition)

    @property
    def discount_dollars(self) -> float | None:
        if self.price is None or not self.regular_price:
            return None
        return round(self.regular_price - self.price, 2)

    @property
    def discount_pct(self) -> float | None:
        """Percent off the regular (new, list) price, or None if not computable."""
        if self.price is None or not self.regular_price or self.regular_price <= 0:
            return None
        return round((self.regular_price - self.price) / self.regular_price * 100, 1)

    @property
    def store_names(self) -> list[str]:
        out = []
        for _sid, name, city, region, _low in self.stores:
            label = f"{name} ({city}"
            label += f", {region})" if region else ")"
            if is_tax_free(region):
                label += " \u2014 no sales tax"
            out.append(label)
        return out

    @property
    def tax_free_stores(self) -> list[tuple]:
        return [s for s in self.stores if is_tax_free(s[3])]


@dataclass
class Alert:
    """A single thing worth telling the user about."""

    kind: str
    sku: str
    condition: str
    product_name: str
    message: str
    price: float | None
    regular_price: float | None
    discount_pct: float | None
    url: str
    dedupe_key: str
    #: 0 = informational, 1 = notable, 2 = drop everything.
    severity: int = 1
    stores: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)
