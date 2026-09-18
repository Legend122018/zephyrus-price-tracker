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
    "openbox-excellent": "Open-Box: Excellent",
    "openbox-certified": "Open-Box: Certified",
    "openbox-satisfactory": "Open-Box: Satisfactory",
    "openbox-fair": "Open-Box: Fair",
}

CONDITION_ORDER = [
    NEW,
    REFURBISHED,
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


@dataclass
class Product:
    """A tracked SKU, independent of any particular offer on it."""

    sku: str
    name: str
    model_number: str = ""
    url: str = ""
    image: str = ""
    manufacturer: str = ""


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
    #: Boston-area stores holding stock, as ``[(store_id, name, city, low_stock)]``.
    stores: list[tuple[str, str, str, bool]] = field(default_factory=list)

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
        return [f"{name} ({city})" for _sid, name, city, _low in self.stores]


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
