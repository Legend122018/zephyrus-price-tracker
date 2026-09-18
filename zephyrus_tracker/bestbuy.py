"""Minimal Best Buy Developer API client (stdlib only).

Covers the three endpoints this tracker needs:

* ``/v1/products(...)``            -- new / refurbished listings and list prices
* ``/beta/products/openBox(...)``  -- open-box offers per condition tier
* ``/v1/products/{sku}/stores``    -- which stores actually hold stock right now
* ``/v1/stores(area(zip,radius))`` -- resolve the Boston-area store list

Best Buy allows roughly 5 requests/second and 50k/day per key, so every call
goes through a simple rate limiter with exponential backoff on 429/5xx.
"""

from __future__ import annotations

import json
import logging
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

log = logging.getLogger(__name__)

BASE = "https://api.bestbuy.com"

#: Attributes we pull for every product. Keep this tight -- the API is faster
#: and the payloads far smaller when you ask for exactly what you use.
PRODUCT_FIELDS = (
    "sku",
    "name",
    "manufacturer",
    "modelNumber",
    "condition",
    "regularPrice",
    "salePrice",
    "onSale",
    "percentSavings",
    "dollarSavings",
    "url",
    "image",
    "onlineAvailability",
    "inStoreAvailability",
    "orderable",
    "customerReviewAverage",
)


class BestBuyError(RuntimeError):
    """Any non-recoverable failure talking to the API."""


class AuthError(BestBuyError):
    """The API key was rejected (403). Nothing to retry."""


class _RateLimiter:
    """Blocking token-bucket-ish limiter: never more than N requests/second."""

    def __init__(self, per_second: float) -> None:
        self._min_interval = 1.0 / per_second if per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next_at:
                time.sleep(self._next_at - now)
                now = time.monotonic()
            self._next_at = now + self._min_interval


class BestBuyClient:
    def __init__(
        self,
        api_key: str,
        *,
        requests_per_second: float = 4.0,
        timeout: int = 30,
        max_retries: int = 4,
        ca_bundle: str | None = None,
    ) -> None:
        if not api_key:
            raise AuthError(
                "No Best Buy API key. Get a free one at https://developer.bestbuy.com/ "
                "then set BESTBUY_API_KEY or put it in config.toml."
            )
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self._limiter = _RateLimiter(requests_per_second)
        # Honours SSL_CERT_FILE / REQUESTS_CA_BUNDLE automatically; ca_bundle wins.
        self._ssl = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else ssl.create_default_context()
        self.request_count = 0

    # ------------------------------------------------------------------ HTTP

    def _fetch(self, url: str) -> dict[str, Any]:
        """GET a URL with retries. Returns parsed JSON."""
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._limiter.wait()
            self.request_count += 1
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "zephyrus-price-tracker/1.0 (personal price tracking)",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", "replace")[:400]
                except Exception:  # pragma: no cover - body is best-effort
                    pass
                if exc.code == 403:
                    # Best Buy returns 403 both for a bad key and for over-quota.
                    if "rate" in body.lower() or "limit" in body.lower():
                        last_err = BestBuyError(f"Rate limited (403): {body}")
                    else:
                        raise AuthError(
                            f"Best Buy rejected the API key (403). {body}"
                        ) from exc
                elif exc.code == 404:
                    # A SKU with no data for this endpoint. Caller treats as empty.
                    return {}
                elif exc.code in (429, 500, 502, 503, 504):
                    last_err = BestBuyError(f"HTTP {exc.code}: {body}")
                else:
                    raise BestBuyError(f"HTTP {exc.code} for {_redact(url)}: {body}") from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                last_err = exc

            if attempt < self.max_retries:
                delay = min(2 ** attempt, 16) + (0.1 * attempt)
                log.warning(
                    "Best Buy request failed (%s); retrying in %.1fs [%d/%d]",
                    last_err, delay, attempt + 1, self.max_retries,
                )
                time.sleep(delay)

        raise BestBuyError(f"Gave up on {_redact(url)} after {self.max_retries} retries: {last_err}")

    def _url(self, path_expr: str, params: dict[str, Any] | None = None) -> str:
        """Build a Best Buy URL.

        The parenthesised query expression lives in the *path*, so it must not be
        fully URL-encoded (``&``, ``(``, ``=`` are all significant there). Only
        spaces need escaping -- e.g. ``sku in(1,2)`` -> ``sku%20in(1,2)``.
        """
        query = {"apiKey": self.api_key, "format": "json"}
        query.update(params or {})
        return f"{BASE}{path_expr.replace(' ', '%20')}?{urllib.parse.urlencode(query)}"

    # -------------------------------------------------------------- endpoints

    def search_products(
        self,
        expression: str,
        *,
        page_size: int = 100,
        max_pages: int = 10,
        show: tuple[str, ...] = PRODUCT_FIELDS,
    ) -> Iterator[dict[str, Any]]:
        """Yield every product matching a query expression, following pagination.

        ``expression`` is the bit inside the parens, e.g. ``search=zephyrus``.
        """
        page = 1
        while page <= max_pages:
            data = self._fetch(
                self._url(
                    f"/v1/products({expression})",
                    {"show": ",".join(show), "pageSize": page_size, "page": page},
                )
            )
            products = data.get("products") or []
            for product in products:
                yield product
            total_pages = int(data.get("totalPages") or 1)
            if page >= total_pages or not products:
                return
            page += 1

    def open_box(self, skus: list[str]) -> dict[str, dict[str, Any]]:
        """Open-box offers keyed by SKU.

        SKUs with no open-box stock are simply absent from the response -- that
        absence is exactly what drives restock detection.
        """
        out: dict[str, dict[str, Any]] = {}
        for chunk in _chunks(skus, 50):
            expr = "sku in(" + ",".join(str(s) for s in chunk) + ")"
            data = self._fetch(self._url(f"/beta/products/openBox({expr})"))
            for entry in data.get("results") or []:
                sku = str(entry.get("sku") or "").strip()
                if sku:
                    out[sku] = entry
        return out

    def stores_near(self, postal_code: str, radius_miles: int = 25) -> list[dict[str, Any]]:
        """Every Best Buy within ``radius_miles`` of a ZIP, nearest first."""
        stores: list[dict[str, Any]] = []
        page = 1
        while page <= 5:
            data = self._fetch(
                self._url(
                    f"/v1/stores(area({postal_code},{radius_miles}))",
                    {
                        "show": "storeId,name,longName,address,city,region,postalCode,phone,distance,storeType",
                        "pageSize": 100,
                        "page": page,
                    },
                )
            )
            batch = data.get("stores") or []
            stores.extend(batch)
            if page >= int(data.get("totalPages") or 1) or not batch:
                break
            page += 1
        stores.sort(key=lambda s: float(s.get("distance") or 9999))
        return stores

    def product_store_availability(self, sku: str, postal_code: str) -> list[dict[str, Any]]:
        """Stores near ``postal_code`` that currently hold ``sku`` in stock.

        Best Buy only returns stores *with* stock, so an empty list means the
        SKU cannot be picked up anywhere nearby today.
        """
        url = self._url(f"/v1/products/{sku}/stores", {"postalCode": postal_code})
        data = self._fetch(url)
        return data.get("stores") or []

    def validate_key(self) -> bool:
        """Cheap round-trip that raises AuthError on a bad key."""
        self._fetch(self._url("/v1/stores(area(02108,5))", {"pageSize": 1, "show": "storeId"}))
        return True


def _chunks(items: list, size: int) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _redact(url: str) -> str:
    """Strip the API key before a URL lands in a log line or traceback."""
    return urllib.parse.urlsplit(url)._replace(query="apiKey=***").geturl()
