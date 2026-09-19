"""End-to-end tests driven by a fake Best Buy client -- no API key needed."""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zephyrus_tracker.config import Config, DEFAULTS
from zephyrus_tracker.detect import (
    ALL_TIME_LOW, BOSTON_PICKUP, HEAVY_DISCOUNT, OPENBOX_RESTOCK, PRICE_DROP, Detector,
)
from zephyrus_tracker.models import Offer
from zephyrus_tracker.report import console_report, html_report
from zephyrus_tracker.scan import Scanner
from zephyrus_tracker.storage import Storage

G14 = "6571749"
G16 = "6571750"

BASE_PRODUCTS = [
    {
        "sku": G14, "name": 'ASUS - ROG Zephyrus G14 14" OLED Gaming Laptop',
        "manufacturer": "ASUS", "modelNumber": "GA403UV", "condition": "New",
        "regularPrice": 1999.99, "salePrice": 1999.99, "onSale": False,
        "url": "https://www.bestbuy.com/g14", "image": "https://img/g14.jpg",
        "onlineAvailability": True, "inStoreAvailability": True, "orderable": "Available",
    },
    {
        "sku": G16, "name": 'ASUS - ROG Zephyrus G16 16" OLED Gaming Laptop',
        "manufacturer": "ASUS", "modelNumber": "GU605MI", "condition": "New",
        "regularPrice": 2499.99, "salePrice": 2499.99,
        "url": "https://www.bestbuy.com/g16", "image": "",
        "onlineAvailability": True, "orderable": "Available",
    },
    {   # Noise that must be filtered out.
        "sku": "9999999", "name": "ASUS - ROG Zephyrus Laptop Sleeve Case",
        "manufacturer": "ASUS", "condition": "New", "regularPrice": 49.99,
        "orderable": "Available",
    },
    {   # Right keyword, wrong brand.
        "sku": "8888888", "name": "Zephyrus-compatible 240W Charger by Generic",
        "manufacturer": "Generic", "condition": "New", "regularPrice": 89.99,
        "orderable": "Available",
    },
]

STORES = [
    {"storeId": "1497", "name": "Cambridge", "city": "Cambridge", "region": "MA",
     "postalCode": "02141", "distance": 2.4, "storeType": "Big Box"},
    {"storeId": "1416", "name": "Watertown", "city": "Watertown", "region": "MA",
     "postalCode": "02472", "distance": 6.8, "storeType": "Big Box"},
]


class FakeClient:
    """Stands in for BestBuyClient; every response is scripted per test."""

    def __init__(self, products=None, open_box=None, availability=None):
        self.products = copy.deepcopy(products if products is not None else BASE_PRODUCTS)
        self._open_box = copy.deepcopy(open_box or {})
        self._availability = copy.deepcopy(availability or {})
        self.request_count = 0

    def stores_near(self, postal_code, radius_miles=25):
        self.request_count += 1
        return copy.deepcopy(STORES)

    def search_products(self, expression, **kwargs):
        self.request_count += 1
        yield from copy.deepcopy(self.products)

    def open_box(self, skus):
        self.request_count += 1
        return {s: copy.deepcopy(e) for s, e in self._open_box.items() if s in skus}

    def product_store_availability(self, sku, postal_code):
        self.request_count += 1
        return copy.deepcopy(self._availability.get(sku, []))


def make_config(tmpdir, **overrides):
    data = copy.deepcopy(DEFAULTS)
    data["storage"]["database"] = str(Path(tmpdir) / "test.db")
    data["notify"]["console"]["enabled"] = False
    data["notify"]["file"]["enabled"] = False
    for dotted, value in overrides.items():
        node = data
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return Config(data)


def open_box_entry(sku, regular, offers):
    return {
        "sku": sku,
        "names": {"title": f"SKU {sku}"},
        "prices": {"regular": regular},
        "links": {"web": f"https://www.bestbuy.com/openbox/{sku}"},
        "offers": [
            {"condition": cond, "prices": {"current": price, "regular": regular},
             "links": {"web": f"https://www.bestbuy.com/openbox/{sku}/{cond}"}}
            for cond, price in offers
        ],
    }


class TrackerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = make_config(self.tmp.name)
        self.db = Storage(self.cfg.get("storage.database"))
        self.addCleanup(self.db.close)

    def scan(self, client, cfg=None):
        return Scanner(cfg or self.cfg, self.db, client).run(dry_run=True, notifiers=[])

    def kinds(self, result):
        return {a.kind for a in result.alerts}


class TestDiscoveryFilters(TrackerTestCase):
    def test_accessories_and_other_brands_are_excluded(self):
        result = self.scan(FakeClient())
        self.assertEqual(result.products, 2)
        skus = {o.sku for o in result.offers}
        self.assertEqual(skus, {G14, G16})

    def test_model_allowlist_narrows_results(self):
        cfg = make_config(self.tmp.name, **{"search.models": ["G14"]})
        cfg.set("storage.database", self.cfg.get("storage.database"))
        result = self.scan(FakeClient(), cfg)
        self.assertEqual(result.products, 1)
        self.assertEqual(result.offers[0].sku, G14)


class TestOpenBox(TrackerTestCase):
    def test_open_box_offers_become_separate_tracked_offers(self):
        client = FakeClient(open_box={
            G14: open_box_entry(G14, 1999.99, [("excellent", 1499.99), ("fair", 1299.99)])
        })
        result = self.scan(client)
        conditions = {o.condition for o in result.offers if o.sku == G14}
        self.assertIn("openbox-excellent", conditions)
        self.assertIn("openbox-fair", conditions)

    def test_discount_is_measured_against_the_new_list_price(self):
        client = FakeClient(open_box={
            G14: open_box_entry(G14, 1999.99, [("excellent", 1499.99)])
        })
        result = self.scan(client)
        offer = next(o for o in result.offers if o.condition == "openbox-excellent")
        self.assertEqual(offer.regular_price, 1999.99)
        self.assertEqual(offer.discount_pct, 25.0)

    def test_open_box_appearing_fires_a_restock_alert(self):
        self.scan(FakeClient())  # first scan: no open-box stock at all

        client = FakeClient(open_box={
            G14: open_box_entry(G14, 1999.99, [("excellent", 1499.99)])
        })
        result = self.scan(client)
        self.assertIn(OPENBOX_RESTOCK, self.kinds(result))

    def test_open_box_selling_out_then_returning_fires_again(self):
        stocked = FakeClient(open_box={
            G14: open_box_entry(G14, 1999.99, [("excellent", 1499.99)])
        })
        self.scan(stocked)
        self.scan(FakeClient())            # sold out -- offer disappears
        result = self.scan(stocked)        # back in stock
        self.assertIn(OPENBOX_RESTOCK, self.kinds(result))

        state = self.db.get_offer_state(G14, "openbox-excellent")
        self.assertTrue(state["available"])

    def test_disappearing_offer_is_marked_unavailable(self):
        self.scan(FakeClient(open_box={
            G14: open_box_entry(G14, 1999.99, [("excellent", 1499.99)])
        }))
        self.scan(FakeClient())
        state = self.db.get_offer_state(G14, "openbox-excellent")
        self.assertFalse(state["available"])
        self.assertIsNone(state["price"])


class TestPriceAlerts(TrackerTestCase):
    def test_sale_price_below_threshold_is_a_heavy_discount(self):
        products = copy.deepcopy(BASE_PRODUCTS)
        products[0]["salePrice"] = 1449.99      # 27.5% off 1999.99
        result = self.scan(FakeClient(products=products))
        self.assertIn(HEAVY_DISCOUNT, self.kinds(result))

    def test_small_drop_below_thresholds_is_ignored(self):
        self.scan(FakeClient())
        products = copy.deepcopy(BASE_PRODUCTS)
        products[0]["salePrice"] = 1994.99      # $5 off -- noise
        result = self.scan(FakeClient(products=products))
        self.assertEqual(result.alerts, [])

    def test_price_drop_fires_once_history_exists(self):
        self.scan(FakeClient())
        products = copy.deepcopy(BASE_PRODUCTS)
        products[0]["salePrice"] = 1849.99      # $150 off last seen, 7.5%
        result = self.scan(FakeClient(products=products))
        self.assertIn(PRICE_DROP, self.kinds(result))

    def test_all_time_low_needs_history_and_then_wins(self):
        for price in (1999.99, 1899.99, 1949.99):
            products = copy.deepcopy(BASE_PRODUCTS)
            products[0]["salePrice"] = price
            self.scan(FakeClient(products=products))

        products = copy.deepcopy(BASE_PRODUCTS)
        products[0]["salePrice"] = 1799.99       # beats the 1899.99 low
        result = self.scan(FakeClient(products=products))
        alerts = [a for a in result.alerts if a.sku == G14]
        self.assertEqual([a.kind for a in alerts if a.kind == ALL_TIME_LOW], [ALL_TIME_LOW])

    def test_max_price_cap_mutes_expensive_units(self):
        cfg = make_config(self.tmp.name, **{"thresholds.max_price": 1000.0})
        cfg.set("storage.database", self.cfg.get("storage.database"))
        products = copy.deepcopy(BASE_PRODUCTS)
        products[0]["salePrice"] = 1400.00       # 30% off but over the cap
        result = self.scan(FakeClient(products=products), cfg)
        self.assertEqual(result.alerts, [])

    def test_only_one_price_alert_per_offer(self):
        """A great deal is one alert, not three near-identical ones."""
        for price in (1999.99, 1950.00, 1900.00):
            products = copy.deepcopy(BASE_PRODUCTS)
            products[0]["salePrice"] = price
            self.scan(FakeClient(products=products))

        products = copy.deepcopy(BASE_PRODUCTS)
        products[0]["salePrice"] = 1299.99   # all-time low AND heavy discount AND big drop
        result = self.scan(FakeClient(products=products))
        g14_alerts = [a for a in result.alerts if a.sku == G14 and a.condition == "new"]
        self.assertEqual(len(g14_alerts), 1)
        self.assertEqual(g14_alerts[0].kind, ALL_TIME_LOW)


class TestStoreAvailability(TrackerTestCase):
    def test_only_stores_in_the_boston_set_are_counted(self):
        client = FakeClient(availability={G14: [
            {"storeID": "1497", "name": "Cambridge", "city": "Cambridge", "lowStock": False},
            {"storeID": "7777", "name": "Nashua NH", "city": "Nashua", "lowStock": False},
        ]})
        result = self.scan(client)
        offer = next(o for o in result.offers if o.sku == G14 and o.condition == "new")
        self.assertEqual([s[0] for s in offer.stores], ["1497"])

    def test_first_local_stock_fires_a_pickup_alert(self):
        self.scan(FakeClient())
        client = FakeClient(availability={G14: [
            {"storeID": "1497", "name": "Cambridge", "city": "Cambridge", "lowStock": True},
        ]})
        result = self.scan(client)
        self.assertIn(BOSTON_PICKUP, self.kinds(result))


class TestCooldown(TrackerTestCase):
    def test_repeat_scans_do_not_resend_the_same_alert(self):
        products = copy.deepcopy(BASE_PRODUCTS)
        products[0]["salePrice"] = 1449.99
        client = FakeClient(products=products)

        first = Scanner(self.cfg, self.db, client).run(dry_run=False, notifiers=[])
        self.assertIn(HEAVY_DISCOUNT, {a.kind for a in first.alerts})

        second = Scanner(self.cfg, self.db, FakeClient(products=products)).run(
            dry_run=False, notifiers=[])
        self.assertEqual(second.alerts, [])
        self.assertGreaterEqual(second.suppressed, 1)

    def test_zero_cooldown_disables_suppression(self):
        cfg = make_config(self.tmp.name, **{"alerts.cooldown_hours": 0})
        cfg.set("storage.database", self.cfg.get("storage.database"))
        products = copy.deepcopy(BASE_PRODUCTS)
        products[0]["salePrice"] = 1449.99

        Scanner(cfg, self.db, FakeClient(products=products)).run(dry_run=False, notifiers=[])
        second = Scanner(cfg, self.db, FakeClient(products=products)).run(
            dry_run=False, notifiers=[])
        self.assertIn(HEAVY_DISCOUNT, {a.kind for a in second.alerts})


class TestStorage(TrackerTestCase):
    def test_history_only_records_changes(self):
        client = FakeClient()
        for _ in range(3):
            self.scan(client)                       # identical prices each time
        self.assertEqual(self.db.observation_count(G14, "new"), 1)

        products = copy.deepcopy(BASE_PRODUCTS)
        products[0]["salePrice"] = 1799.99
        self.scan(FakeClient(products=products))
        self.assertEqual(self.db.observation_count(G14, "new"), 2)

    def test_lowest_price_tracks_the_minimum(self):
        for price in (1999.99, 1699.99, 1899.99):
            products = copy.deepcopy(BASE_PRODUCTS)
            products[0]["salePrice"] = price
            self.scan(FakeClient(products=products))
        self.assertEqual(self.db.lowest_price(G14, "new"), 1699.99)

    def test_scan_metadata_is_recorded(self):
        self.scan(FakeClient())
        last = self.db.last_scan()
        self.assertEqual(last["products"], 2)
        self.assertIsNone(last["error"])


class TestConfig(unittest.TestCase):
    def test_user_values_merge_over_defaults_without_dropping_siblings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('[thresholds]\nheavy_discount_pct = 30.0\n')
            cfg = Config.load(path)
            self.assertEqual(cfg.get("thresholds.heavy_discount_pct"), 30.0)
            self.assertEqual(cfg.get("thresholds.price_drop_pct"), 5.0)

    def test_env_var_overrides_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('[bestbuy]\napi_key = "from-file"\n')
            os.environ["BESTBUY_API_KEY"] = "from-env"
            try:
                self.assertEqual(Config.load(path).get("bestbuy.api_key"), "from-env")
            finally:
                del os.environ["BESTBUY_API_KEY"]

    def test_validate_flags_a_silent_setup(self):
        cfg = Config(copy.deepcopy(DEFAULTS))
        cfg.set("bestbuy.api_key", "x")
        for channel in ("console", "file", "email", "ntfy", "webhook"):
            cfg.set(f"notify.{channel}.enabled", False)
        self.assertTrue(any("alerts would go nowhere" in p for p in cfg.validate()))


class TestReports(TrackerTestCase):
    def test_html_report_is_self_contained_and_themed(self):
        self.scan(FakeClient(open_box={
            G14: open_box_entry(G14, 1999.99, [("excellent", 1499.99)])
        }))
        products = copy.deepcopy(BASE_PRODUCTS)
        products[0]["salePrice"] = 1799.99
        self.scan(FakeClient(products=products))

        out = Path(self.tmp.name) / "report.html"
        html_report(self.db, out, self.cfg)
        text = out.read_text()

        self.assertIn("Zephyrus", text)
        self.assertIn("prefers-color-scheme: dark", text)
        self.assertIn('data-theme="dark"', text)
        self.assertIn("<svg", text)                      # sparkline rendered
        self.assertNotIn("<script", text)                # no JS, no CDN
        self.assertNotIn("http://", text.replace("http://www.w3.org", ""))

    def test_console_report_lists_every_condition(self):
        self.scan(FakeClient(open_box={
            G14: open_box_entry(G14, 1999.99, [("excellent", 1499.99)])
        }))
        text = console_report(self.db)
        self.assertIn("Open-Box: Excellent", text)
        self.assertIn("New", text)


class TestNotificationPayloads(unittest.TestCase):
    def test_slack_and_discord_get_their_own_shapes(self):
        from zephyrus_tracker.models import Alert
        from zephyrus_tracker.notify import WebhookNotifier

        alert = Alert(kind="x", sku="1", condition="new", product_name="Zephyrus G14",
                      message="cheap", price=1.0, regular_price=2.0, discount_pct=50.0,
                      url="https://example.com", dedupe_key="k")

        slack = WebhookNotifier({"url": "https://hooks.slack.com/services/x"})
        self.assertIn("text", slack._payload([alert]))

        discord = WebhookNotifier({"url": "https://discord.com/api/webhooks/1/x"})
        payload = discord._payload([alert])
        self.assertIn("content", payload)
        self.assertLessEqual(len(payload["content"]), 2000)

        generic = WebhookNotifier({"url": "https://example.com/hook"})
        self.assertIn("alerts", generic._payload([alert]))

    def test_alerts_serialise_to_json(self):
        from zephyrus_tracker.models import Alert
        alert = Alert(kind="x", sku="1", condition="new", product_name="G14",
                      message="m", price=1.0, regular_price=2.0, discount_pct=50.0,
                      url="u", dedupe_key="k")
        self.assertEqual(json.loads(json.dumps(alert.as_dict()))["sku"], "1")


class TestShippedConfigExample(unittest.TestCase):
    """The example config ships twice and restates DEFAULTS -- pin both down."""

    ROOT = Path(__file__).resolve().parent.parent
    PACKAGE_COPY = ROOT / "zephyrus_tracker" / "config.example.toml"
    ROOT_COPY = ROOT / "config.example.toml"

    def test_both_copies_are_identical(self):
        self.assertEqual(
            self.PACKAGE_COPY.read_text(), self.ROOT_COPY.read_text(),
            "config.example.toml differs between the repo root and the package; "
            "the package copy is canonical -- copy it over the root one.",
        )

    def test_every_documented_value_matches_defaults(self):
        """A tuned threshold must be changed in config.py AND in the example."""
        import tomllib
        with open(self.PACKAGE_COPY, "rb") as fh:
            example = tomllib.load(fh)

        mismatches = []

        def walk(doc, defaults, path=""):
            for key, value in doc.items():
                where = f"{path}{key}"
                if key not in defaults:
                    mismatches.append(f"{where}: in the example but not in DEFAULTS")
                elif isinstance(value, dict):
                    walk(value, defaults[key], where + ".")
                elif value != defaults[key]:
                    mismatches.append(f"{where}: example {value!r} != DEFAULTS {defaults[key]!r}")

        walk(example, DEFAULTS)
        self.assertEqual(mismatches, [], "config.example.toml has drifted from DEFAULTS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
