"""End-to-end tests driven by a fake Best Buy client -- no API key needed."""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zephyrus_tracker.config import Config, DEFAULTS
from zephyrus_tracker.detect import (
    ALL_TIME_LOW, BOSTON_PICKUP, HEAVY_DISCOUNT, OPENBOX_RESTOCK, PRICE_DROP, Detector,
)
from zephyrus_tracker.feeds import Deal, FeedClient, _last_price
from zephyrus_tracker.models import EXPIRED, LIVE, UNVERIFIED, Offer, Product, liveness_state
from zephyrus_tracker.dashboard import collect, render as render_dashboard
from zephyrus_tracker.report import console_report, html_report
from zephyrus_tracker.scan import FeedScanner, Scanner, _has_keyword
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

#: Stores per area, so merging across ZIPs is actually exercised. Nashua is
#: in New Hampshire, which charges no sales tax.
STORES_BY_AREA = {
    "02108": [
        {"storeId": "1497", "name": "Cambridge", "city": "Cambridge", "region": "MA",
         "postalCode": "02141", "distance": 2.4, "storeType": "Big Box"},
        {"storeId": "1416", "name": "Watertown", "city": "Watertown", "region": "MA",
         "postalCode": "02472", "distance": 6.8, "storeType": "Big Box"},
    ],
    "03063": [
        {"storeId": "0330", "name": "Nashua", "city": "Nashua", "region": "NH",
         "postalCode": "03063", "distance": 1.2, "storeType": "Big Box"},
        {"storeId": "1497", "name": "Cambridge", "city": "Cambridge", "region": "MA",
         "postalCode": "02141", "distance": 41.0, "storeType": "Big Box"},
    ],
}
STORES = STORES_BY_AREA["02108"]


class FakeClient:
    """Stands in for BestBuyClient; every response is scripted per test."""

    def __init__(self, products=None, open_box=None, availability=None):
        self.products = copy.deepcopy(products if products is not None else BASE_PRODUCTS)
        self._open_box = copy.deepcopy(open_box or {})
        self._availability = copy.deepcopy(availability or {})
        self.request_count = 0

    def stores_near(self, postal_code, radius_miles=25):
        self.request_count += 1
        return copy.deepcopy(STORES_BY_AREA.get(str(postal_code), []))

    def search_products(self, expression, **kwargs):
        self.request_count += 1
        yield from copy.deepcopy(self.products)

    def open_box(self, skus):
        self.request_count += 1
        return {s: copy.deepcopy(e) for s, e in self._open_box.items() if s in skus}

    def product_store_availability(self, sku, postal_code):
        self.request_count += 1
        data = self._availability.get(sku, [])
        if isinstance(data, dict):          # keyed by ZIP when a test needs it
            data = data.get(str(postal_code), [])
        return copy.deepcopy(data)


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
        self.scan(FakeClient())                 # baseline scan sends nothing
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


class TestFirstScanBaseline(TrackerTestCase):
    def test_first_scan_records_everything_and_alerts_nothing(self):
        products = copy.deepcopy(BASE_PRODUCTS)
        products[0]["salePrice"] = 999.99       # 50% off -- would normally shout
        result = self.scan(FakeClient(products=products, open_box={
            G14: open_box_entry(G14, 1999.99, [("excellent", 1099.99)])
        }))
        self.assertTrue(result.baseline)
        self.assertEqual(result.alerts, [])
        # ...but the data is all there, ready to compare against next time.
        self.assertIsNotNone(self.db.get_offer_state(G14, "new"))
        self.assertIsNotNone(self.db.get_offer_state(G14, "openbox-excellent"))

    def test_second_scan_alerts_normally(self):
        self.scan(FakeClient())
        products = copy.deepcopy(BASE_PRODUCTS)
        products[0]["salePrice"] = 999.99
        result = self.scan(FakeClient(products=products))
        self.assertFalse(result.baseline)
        self.assertTrue(result.alerts)

    def test_alert_flood_is_capped(self):
        cfg = make_config(self.tmp.name, **{"alerts.max_per_scan": 1})
        cfg.set("storage.database", self.cfg.get("storage.database"))
        self.scan(FakeClient(), cfg)
        products = copy.deepcopy(BASE_PRODUCTS)
        products[0]["salePrice"] = 999.99
        products[1]["salePrice"] = 1299.99
        result = self.scan(FakeClient(products=products), cfg)
        self.assertEqual(len(result.alerts), 1)
        self.assertGreaterEqual(result.capped, 1)


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


class TestNashuaAndTaxFreeStores(TrackerTestCase):
    """New Hampshire has no sales tax, which beats most discounts tracked here."""

    def test_stores_from_every_configured_area_are_merged(self):
        scanner = Scanner(self.cfg, self.db, FakeClient())
        scanner.refresh_stores(force=True)
        ids = {row["store_id"] for row in self.db.get_stores()}
        self.assertEqual(ids, {"1497", "1416", "0330"})

    def test_a_store_listed_in_two_areas_is_not_duplicated(self):
        Scanner(self.cfg, self.db, FakeClient()).refresh_stores(force=True)
        rows = [r["store_id"] for r in self.db.get_stores()]
        self.assertEqual(len(rows), len(set(rows)))

    def test_nashua_stock_is_tracked(self):
        client = FakeClient(availability={G14: [
            {"storeID": "0330", "name": "Nashua", "city": "Nashua",
             "region": "NH", "lowStock": False},
        ]})
        result = self.scan(client)
        offer = next(o for o in result.offers if o.sku == G14 and o.condition == "new")
        self.assertEqual([s[0] for s in offer.stores], ["0330"])
        self.assertTrue(offer.tax_free_stores)
        self.assertIn("no sales tax", offer.store_names[0])

    def test_tax_free_stores_are_listed_first(self):
        client = FakeClient(availability={G14: [
            {"storeID": "1497", "name": "Cambridge", "city": "Cambridge",
             "region": "MA", "lowStock": False},
            {"storeID": "0330", "name": "Nashua", "city": "Nashua",
             "region": "NH", "lowStock": False},
        ]})
        result = self.scan(client)
        offer = next(o for o in result.offers if o.sku == G14 and o.condition == "new")
        self.assertEqual(offer.stores[0][0], "0330", "NH store should lead")

    def test_a_store_only_reachable_from_the_second_area_is_still_found(self):
        # A single Boston query sorts by proximity to Boston and can crowd out
        # a Nashua store, so each area is queried separately.
        client = FakeClient(availability={G14: {
            "02108": [],
            "03063": [{"storeID": "0330", "name": "Nashua", "city": "Nashua",
                       "region": "NH", "lowStock": True}],
        }})
        result = self.scan(client)
        offer = next(o for o in result.offers if o.sku == G14 and o.condition == "new")
        self.assertEqual([s[0] for s in offer.stores], ["0330"])

    def test_region_falls_back_to_the_resolved_store_list(self):
        # The availability endpoint does not always report a state.
        client = FakeClient(availability={G14: [
            {"storeID": "0330", "name": "Nashua", "city": "Nashua", "lowStock": False},
        ]})
        result = self.scan(client)
        offer = next(o for o in result.offers if o.sku == G14 and o.condition == "new")
        self.assertEqual(offer.stores[0][3], "NH")
        self.assertTrue(offer.tax_free_stores)

    def test_pickup_alert_quantifies_the_tax_saving(self):
        self.scan(FakeClient())                      # baseline
        result = self.scan(FakeClient(availability={G14: [
            {"storeID": "0330", "name": "Best Buy Nashua", "city": "Nashua",
             "region": "NH", "lowStock": True},
        ]}))
        alert = next(a for a in result.alerts if a.kind == BOSTON_PICKUP)
        self.assertIn("no sales tax", alert.message)
        self.assertIn("$125.00", alert.message)      # 6.25% of the $1,999.99 list
        self.assertEqual(alert.severity, 2, "a tax-free option outranks most discounts")

    def test_pickup_alert_without_a_tax_free_store_stays_plain(self):
        self.scan(FakeClient())
        result = self.scan(FakeClient(availability={G14: [
            {"storeID": "1497", "name": "Cambridge", "city": "Cambridge",
             "region": "MA", "lowStock": False},
        ]}))
        alert = next(a for a in result.alerts if a.kind == BOSTON_PICKUP)
        self.assertNotIn("sales tax", alert.message)
        self.assertEqual(alert.severity, 1)

    def test_massachusetts_stores_are_not_marked_tax_free(self):
        client = FakeClient(availability={G14: [
            {"storeID": "1497", "name": "Cambridge", "city": "Cambridge",
             "region": "MA", "lowStock": False},
        ]})
        result = self.scan(client)
        offer = next(o for o in result.offers if o.sku == G14 and o.condition == "new")
        self.assertFalse(offer.tax_free_stores)
        self.assertNotIn("no sales tax", offer.store_names[0])


class TestCooldown(TrackerTestCase):
    def test_repeat_scans_do_not_resend_the_same_alert(self):
        products = copy.deepcopy(BASE_PRODUCTS)
        products[0]["salePrice"] = 1449.99
        client = FakeClient(products=products)

        Scanner(self.cfg, self.db, FakeClient()).run(dry_run=False, notifiers=[])  # baseline
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

        Scanner(cfg, self.db, FakeClient()).run(dry_run=False, notifiers=[])  # baseline
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


class TestSchemaMigration(unittest.TestCase):
    def test_a_database_predating_posted_at_gains_the_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            db = Storage(path)
            db.conn.execute("ALTER TABLE products DROP COLUMN posted_at")
            db.conn.execute(
                "INSERT INTO products (sku, name, first_seen, last_seen)"
                " VALUES ('old-1','Existing row','2026-01-01','2026-01-01')")
            db.conn.commit()
            db.close()

            reopened = Storage(path)          # migration runs here
            self.addCleanup(reopened.close)
            columns = {r["name"] for r in reopened.conn.execute("PRAGMA table_info(products)")}
            self.assertIn("posted_at", columns)
            self.assertEqual(len(reopened.get_products()), 1, "existing rows survive")

    def test_posted_at_round_trips_and_is_not_clobbered(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Storage(Path(tmp) / "p.db")
            self.addCleanup(db.close)
            db.upsert_product(Product(sku="sd-1", name="G14", posted_at="2026-07-04"))
            db.conn.commit()
            self.assertEqual(db.get_products()[0]["posted_at"], "2026-07-04")

            # A later scan that cannot supply the date must not erase it.
            db.upsert_product(Product(sku="sd-1", name="G14 renamed"))
            db.conn.commit()
            self.assertEqual(db.get_products()[0]["posted_at"], "2026-07-04")


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


class TestDashboard(TrackerTestCase):
    def build(self, client=None):
        Scanner(self.cfg, self.db, client or FakeClient()).run(dry_run=True, notifiers=[])
        out = Path(self.tmp.name) / "site" / "index.html"
        render_dashboard(self.db, out, self.cfg)
        return out, out.read_text(encoding="utf-8")

    def test_page_is_self_contained(self):
        _out, text = self.build()
        # A dashboard that fetched at view time would be blank inside the
        # artifact sandbox and on any host with a strict CSP.
        for forbidden in ("fetch(", "XMLHttpRequest", "<script src", "import(") :
            self.assertNotIn(forbidden, text, forbidden)

    def test_real_rows_are_embedded(self):
        _out, text = self.build()
        self.assertIn(G14, text)
        self.assertIn('"price"', text)

    def test_open_box_rows_are_flagged(self):
        Scanner(self.cfg, self.db, FakeClient(open_box={
            G14: open_box_entry(G14, 1999.99, [("excellent", 1499.99)])
        })).run(dry_run=True, notifiers=[])
        rows = collect(self.db)
        self.assertTrue(any(r["openBox"] for r in rows))
        self.assertTrue(any(not r["openBox"] for r in rows))

    def test_every_row_carries_a_liveness_state(self):
        self.build()
        for row in collect(self.db):
            self.assertIn(row["state"], (LIVE, UNVERIFIED, EXPIRED))

    def test_rows_from_another_backend_are_archived(self):
        """Switching backends must not leave stale rows looking current."""
        self.build()                                   # Best Buy rows
        db = self.db
        db.upsert_product(Product(sku="sd-999", name="ASUS ROG Zephyrus G14",
                                  posted_at="2026-09-01", source="feeds"))
        db.conn.execute(
            "INSERT INTO offer_state (sku, condition, price, available, store_count,"
            " first_seen, last_seen) VALUES ('sd-999','new',1499.0,1,0,'x','x')")
        db.conn.commit()

        rows = {r["id"]: r for r in collect(db, active_source="bestbuy")}
        self.assertTrue(rows["sd-999"]["archived"], "feed row should be archived")
        self.assertFalse(rows[G14]["archived"], "active-backend row should not be")

    def test_nothing_is_archived_when_the_backend_matches(self):
        self.build()
        for row in collect(self.db, active_source="bestbuy"):
            self.assertFalse(row["archived"])

    def test_rows_are_ordered_oldest_first(self):
        self.build()
        dates = [r["posted"] for r in collect(self.db)]
        self.assertEqual(dates, sorted(dates))

    def test_priceless_offers_are_omitted(self):
        self.build()
        for row in collect(self.db):
            self.assertIsNotNone(row["price"])

    def test_falls_back_to_first_seen_when_no_posting_date(self):
        # The Best Buy backend has no posting date; rows must still appear.
        self.build()
        rows = collect(self.db)
        self.assertTrue(rows)
        for row in rows:
            self.assertRegex(row["posted"], r"^\d{4}-\d{2}-\d{2}$")

    def test_empty_database_still_renders(self):
        out = Path(self.tmp.name) / "empty.html"
        render_dashboard(self.db, out, self.cfg)
        self.assertIn("no deals recorded yet", out.read_text(encoding="utf-8"))


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


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "slickdeals_zephyrus.xml"


class FakeFeedClient:
    """Serves canned feed XML; no network."""

    def __init__(self, deals=None, xml=None, expired=None):
        self._deals = deals
        self._xml = xml if xml is not None else FIXTURE.read_text(encoding="utf-8")
        #: url -> True/False/None, mimicking the source's liveness answer.
        self._expired = expired or {}
        self.request_count = 0
        self.liveness_calls = []

    def check_expired(self, url):
        self.liveness_calls.append(url)
        self.request_count += 1
        return self._expired.get(url, False)

    def slickdeals(self, query):
        self.request_count += 1
        if self._deals is not None:
            return list(self._deals)
        return FeedClient.parse(self._xml, source=f"slickdeals:{query}")

    def feed(self, url):
        self.request_count += 1
        return FeedClient.parse(self._xml, source=url)


def make_deal(uid, title, price, condition="new", retailer="Best Buy", age_days=1):
    return Deal(
        uid=uid, title=title, url=f"https://slickdeals.net/f/{uid}",
        price=price, condition=condition, retailer=retailer,
        posted_at=datetime.now(timezone.utc) - timedelta(days=age_days),
        source="test", raw_title=title,
    )


class TestPriceParsing(unittest.TestCase):
    def test_prices_with_and_without_thousands_separators(self):
        # The first cut of this regex matched "259" inside "$2599.99", because
        # the comma-grouped alternative won and never backtracked.
        cases = {
            "$2599.99": 2599.99, "$1,631.99": 1631.99, "$733.59": 733.59,
            "$2,499": 2499.0, "$99": 99.0, "SSD $2599.99": 2599.99,
        }
        for text, expected in cases.items():
            self.assertEqual(_last_price(text), expected, text)

    def test_trailing_price_wins(self):
        self.assertEqual(_last_price("was $2,499.00 now $1,899.99"), 1899.99)

    def test_no_price_is_none(self):
        self.assertIsNone(_last_price("ROG Zephyrus G16 - 16GB - RTX 5070"))


class TestFeedParsing(unittest.TestCase):
    """Parsed against a real Slickdeals response saved to tests/fixtures."""

    def setUp(self):
        self.deals = FeedClient.parse(FIXTURE.read_text(encoding="utf-8"), source="fixture")

    def test_every_item_parses(self):
        self.assertGreaterEqual(len(self.deals), 4)
        for deal in self.deals:
            self.assertTrue(deal.uid and deal.title and deal.url)

    def test_open_box_postings_are_classified(self):
        open_box = [d for d in self.deals if d.condition == "openbox-listed"]
        self.assertTrue(open_box, "fixture must contain open-box postings")
        for deal in open_box:
            self.assertIsNotNone(deal.price)

    def test_retailer_is_recognised(self):
        self.assertIn("Best Buy", {d.retailer for d in self.deals})

    def test_uid_is_the_stable_thread_id(self):
        for deal in self.deals:
            self.assertTrue(deal.uid.startswith("sd-"), deal.uid)
        self.assertEqual(len({d.uid for d in self.deals}), len(self.deals))

    def test_tracking_urls_are_stripped(self):
        for deal in self.deals:
            self.assertNotIn("utm_", deal.url)

    def test_title_loses_its_trailing_price(self):
        for deal in self.deals:
            self.assertFalse(deal.title.rstrip().endswith(".99"), deal.title)


class TestKeywordMatching(unittest.TestCase):
    def test_matching_is_word_bounded(self):
        # Substring matching would drop this for the exclude keyword "case".
        self.assertFalse(_has_keyword("ASUS Showcase Laptop", ["case"]))
        self.assertTrue(_has_keyword("ASUS Laptop Sleeve Case", ["case"]))


class TestFeedScanner(TrackerTestCase):
    def feed_scan(self, client, cfg=None):
        return FeedScanner(cfg or self.cfg, self.db, client).run(dry_run=True, notifiers=[])

    def test_fixture_deals_become_tracked_offers(self):
        result = self.feed_scan(FakeFeedClient())
        self.assertGreaterEqual(result.products, 4)
        self.assertTrue(result.baseline)
        self.assertEqual(result.alerts, [])

    def test_a_new_open_box_posting_alerts(self):
        self.feed_scan(FakeFeedClient(deals=[
            make_deal("sd-1", "ASUS ROG Zephyrus G14 Gaming Laptop", 2599.99),
        ]))
        result = self.feed_scan(FakeFeedClient(deals=[
            make_deal("sd-1", "ASUS ROG Zephyrus G14 Gaming Laptop", 2599.99),
            make_deal("sd-2", "Open-Box: ASUS ROG Zephyrus G14", 1279.99,
                      condition="openbox-listed"),
        ]))
        self.assertIn(OPENBOX_RESTOCK, {a.kind for a in result.alerts})

    def test_retailer_filter_drops_other_stores_and_unknowns(self):
        cfg = make_config(self.tmp.name, **{"feeds.retailers": ["Best Buy"]})
        cfg.set("storage.database", self.cfg.get("storage.database"))
        result = self.feed_scan(FakeFeedClient(deals=[
            make_deal("sd-1", "ASUS ROG Zephyrus G14", 1999.0, retailer="Best Buy"),
            make_deal("sd-2", "ASUS ROG Zephyrus G14", 1899.0, retailer="Amazon"),
            make_deal("sd-3", "ASUS ROG Zephyrus G14", 1799.0, retailer=""),
        ]), cfg)
        self.assertEqual({o.sku for o in result.offers}, {"sd-1"})

    def test_stale_postings_are_ignored(self):
        cfg = make_config(self.tmp.name, **{"feeds.max_age_days": 30})
        cfg.set("storage.database", self.cfg.get("storage.database"))
        result = self.feed_scan(FakeFeedClient(deals=[
            make_deal("sd-1", "ASUS ROG Zephyrus G14", 1999.0, age_days=5),
            make_deal("sd-2", "ASUS ROG Zephyrus G14", 1899.0, age_days=90),
        ]), cfg)
        self.assertEqual({o.sku for o in result.offers}, {"sd-1"})

    def test_non_zephyrus_results_are_filtered_out(self):
        # Feed search is fuzzy -- an "open box" query really does return
        # MacBooks alongside the laptops we asked about.
        result = self.feed_scan(FakeFeedClient(deals=[
            make_deal("sd-1", "ASUS ROG Zephyrus G14", 1999.0),
            make_deal("sd-2", "Apple MacBook Air (Open-Boxes): 15\", M4", 860.99,
                      condition="openbox-listed"),
        ]))
        self.assertEqual({o.sku for o in result.offers}, {"sd-1"})

    def test_priceless_postings_are_skipped_when_required(self):
        result = self.feed_scan(FakeFeedClient(deals=[
            make_deal("sd-1", "ASUS ROG Zephyrus G14", None),
            make_deal("sd-2", "ASUS ROG Zephyrus G16", 1899.0),
        ]))
        self.assertEqual({o.sku for o in result.offers}, {"sd-2"})

    def test_a_deal_whose_price_is_edited_records_history(self):
        first = [make_deal("sd-1", "ASUS ROG Zephyrus G14", 1999.0)]
        self.feed_scan(FakeFeedClient(deals=first))
        self.feed_scan(FakeFeedClient(deals=[
            make_deal("sd-1", "ASUS ROG Zephyrus G14", 1699.0)]))
        self.assertEqual(self.db.observation_count("sd-1", "new"), 2)
        self.assertEqual(self.db.lowest_price("sd-1", "new"), 1699.0)


class TestLiveness(TrackerTestCase):
    """Feed search returns postings that died months ago."""

    def feed_scan(self, client, cfg=None):
        return FeedScanner(cfg or self.cfg, self.db, client).run(dry_run=True, notifiers=[])

    def test_expired_deals_are_not_available(self):
        deals = [make_deal("sd-1", "ASUS ROG Zephyrus G14", 1999.0),
                 make_deal("sd-2", "ASUS ROG Zephyrus G16", 1599.0)]
        client = FakeFeedClient(deals=deals,
                                expired={"https://slickdeals.net/f/sd-2": True})
        result = self.feed_scan(client)
        by_sku = {o.sku: o for o in result.offers}
        self.assertTrue(by_sku["sd-1"].available)
        self.assertFalse(by_sku["sd-2"].available)
        self.assertEqual(result.expired, 1)

    def test_an_expired_deal_is_never_rechecked(self):
        deals = [make_deal("sd-1", "ASUS ROG Zephyrus G14", 1999.0)]
        dead = {"https://slickdeals.net/f/sd-1": True}

        first = FakeFeedClient(deals=deals, expired=dead)
        self.feed_scan(first)
        self.assertEqual(len(first.liveness_calls), 1)

        # Expiry is one-way, so the second scan must not spend a request on it.
        second = FakeFeedClient(deals=deals, expired=dead)
        self.feed_scan(second)
        self.assertEqual(second.liveness_calls, [])

    def test_a_live_deal_is_not_rechecked_inside_the_window(self):
        deals = [make_deal("sd-1", "ASUS ROG Zephyrus G14", 1999.0)]
        first = FakeFeedClient(deals=deals)
        self.feed_scan(first)
        self.assertEqual(len(first.liveness_calls), 1)

        second = FakeFeedClient(deals=deals)
        self.feed_scan(second)
        self.assertEqual(second.liveness_calls, [])

    def test_expired_deals_do_not_raise_a_restock_alert(self):
        live = [make_deal("sd-1", "ASUS ROG Zephyrus G14", 1999.0)]
        self.feed_scan(FakeFeedClient(deals=live))

        # An open-box posting that is already dead must stay silent.
        deals = live + [make_deal("sd-9", "Open-Box: ASUS ROG Zephyrus G14", 1279.0,
                                  condition="openbox-listed")]
        result = self.feed_scan(FakeFeedClient(
            deals=deals, expired={"https://slickdeals.net/f/sd-9": True}))
        self.assertNotIn(OPENBOX_RESTOCK, {a.kind for a in result.alerts})

    def test_a_live_open_box_posting_still_alerts(self):
        live = [make_deal("sd-1", "ASUS ROG Zephyrus G14", 1999.0)]
        self.feed_scan(FakeFeedClient(deals=live))
        deals = live + [make_deal("sd-9", "Open-Box: ASUS ROG Zephyrus G14", 1279.0,
                                  condition="openbox-listed")]
        result = self.feed_scan(FakeFeedClient(deals=deals))
        self.assertIn(OPENBOX_RESTOCK, {a.kind for a in result.alerts})

    def test_the_check_budget_is_respected(self):
        cfg = make_config(self.tmp.name, **{"feeds.liveness_checks_per_scan": 2})
        cfg.set("storage.database", self.cfg.get("storage.database"))
        deals = [make_deal(f"sd-{i}", "ASUS ROG Zephyrus G14", 1900.0 + i)
                 for i in range(6)]
        client = FakeFeedClient(deals=deals)
        self.feed_scan(client, cfg)
        self.assertEqual(len(client.liveness_calls), 2)

    def test_checking_can_be_switched_off(self):
        cfg = make_config(self.tmp.name, **{"feeds.check_liveness": False})
        cfg.set("storage.database", self.cfg.get("storage.database"))
        client = FakeFeedClient(deals=[make_deal("sd-1", "ASUS ROG Zephyrus G14", 1999.0)])
        self.feed_scan(client, cfg)
        self.assertEqual(client.liveness_calls, [])


class TestLivenessState(unittest.TestCase):
    """Deal sites mark expiry late, so age is evidence against an old listing."""

    TODAY = "2026-09-19"

    def test_marked_expired_wins_regardless_of_age(self):
        self.assertEqual(liveness_state(True, "2026-09-19", today=self.TODAY), EXPIRED)

    def test_recent_and_unmarked_is_live(self):
        self.assertEqual(liveness_state(False, "2026-09-10", today=self.TODAY), LIVE)

    def test_old_and_unmarked_is_unverified_not_live(self):
        # The real case this exists for: a posting open since June that the
        # source has never marked dead. Reporting that as "in stock" is a lie.
        self.assertEqual(liveness_state(False, "2026-06-07", today=self.TODAY), UNVERIFIED)

    def test_boundary_is_inclusive_of_the_window(self):
        self.assertEqual(liveness_state(False, "2026-08-20", today=self.TODAY), LIVE)
        self.assertEqual(liveness_state(False, "2026-08-19", today=self.TODAY), UNVERIFIED)

    def test_missing_or_unparseable_date_does_not_crash(self):
        self.assertEqual(liveness_state(False, "", today=self.TODAY), LIVE)
        self.assertEqual(liveness_state(False, "not-a-date", today=self.TODAY), LIVE)

    def test_window_can_be_disabled(self):
        self.assertEqual(
            liveness_state(False, "2020-01-01", stale_after_days=0, today=self.TODAY), LIVE)


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
