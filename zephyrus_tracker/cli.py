"""Command-line interface."""

from __future__ import annotations

import argparse
import logging
import random
import shutil
import signal
import sys
import time
from pathlib import Path

from . import __version__
from .bestbuy import AuthError, BestBuyClient, BestBuyError
from .feeds import FeedClient, FeedError
from .config import Config, ConfigError
from .detect import money
from datetime import datetime, timezone

from .models import Alert, condition_label
from .notify import build_notifiers, dispatch
from .report import console_report, html_report
from .scan import FeedScanner, Scanner
from .storage import Storage

log = logging.getLogger("zephyrus")

EXAMPLE_CONFIG = Path(__file__).resolve().parent / "config.example.toml"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def _client(cfg: Config) -> BestBuyClient:
    return BestBuyClient(
        cfg.require_api_key(),
        requests_per_second=float(cfg.get("bestbuy.requests_per_second", 4.0)),
        timeout=int(cfg.get("bestbuy.timeout_seconds", 30)),
        max_retries=int(cfg.get("bestbuy.max_retries", 4)),
    )


def _storage(cfg: Config) -> Storage:
    return Storage(cfg.get("storage.database", "zephyrus.db"))


def _feed_client(cfg: Config) -> FeedClient:
    return FeedClient(timeout=int(cfg.get("bestbuy.timeout_seconds", 25)))


def _scanner(cfg: Config, db: Storage):
    """The scanner for the configured backend."""
    if cfg.backend == "bestbuy":
        return Scanner(cfg, db, _client(cfg))
    return FeedScanner(cfg, db, _feed_client(cfg))


# ------------------------------------------------------------------ commands


def cmd_init(args, cfg: Config) -> int:
    target = Path(args.config or "config.toml")
    if target.exists() and not args.force:
        print(f"{target} already exists. Use --force to overwrite.")
        return 1
    if not EXAMPLE_CONFIG.exists():
        print(f"Template missing: {EXAMPLE_CONFIG}", file=sys.stderr)
        return 1
    shutil.copy(EXAMPLE_CONFIG, target)
    Storage(cfg.get("storage.database", "zephyrus.db")).close()
    print(f"Wrote {target}")
    print("\nNext steps:")
    print("  1. Get a free API key: https://developer.bestbuy.com/")
    print("  2. export BESTBUY_API_KEY=your_key")
    print(f"  3. Edit {target} to pick alert thresholds and a notification channel")
    print("  4. zephyrus check && zephyrus scan")
    return 0


def cmd_check(args, cfg: Config) -> int:
    problems = cfg.validate()
    print(f"Config: {cfg.path or '(defaults only -- no config.toml found)'}")
    print(f"  Backend: {cfg.backend}"
          + ("  (public deal feeds -- no credentials needed)" if cfg.backend == "feeds"
             else "  (official Best Buy API)"))
    for problem in problems:
        print(f"  ! {problem}")
    if any("api_key" in p for p in problems):
        return 1

    if cfg.backend == "bestbuy":
        try:
            _client(cfg).validate_key()
            print("  Best Buy API key: OK")
        except AuthError as exc:
            print(f"  ! Best Buy API key rejected: {exc}")
            return 1
        except BestBuyError as exc:
            print(f"  ! Could not reach the Best Buy API: {exc}")
            return 1
    else:
        client = _feed_client(cfg)
        reachable = 0
        for query in cfg.get("feeds.queries", []) or []:
            try:
                found = client.slickdeals(str(query))
                reachable += 1
                print(f"  Feed slickdeals({query}): OK, {len(found)} items")
            except FeedError as exc:
                print(f"  ! Feed slickdeals({query}) failed: {exc}")
        for url in cfg.get("feeds.urls", []) or []:
            try:
                found = client.feed(str(url))
                reachable += 1
                print(f"  Feed {url}: OK, {len(found)} items")
            except FeedError as exc:
                print(f"  ! Feed {url} failed: {exc}")
        if not reachable:
            print("  ! No configured feed could be reached")
            return 1

    channels = [n.name for n in build_notifiers(cfg)]
    print(f"  Notification channels: {', '.join(channels) or 'NONE'}")
    db = _storage(cfg)
    print(f"  Database: {db.path} ({len(db.get_products())} products tracked)")
    db.close()
    return 0 if not problems else 1


def cmd_stores(args, cfg: Config) -> int:
    if cfg.backend != "bestbuy":
        print("Store lookup needs the Best Buy API backend; the deal feeds carry\n"
              "no store-level data at all. Set source.backend = \"bestbuy\" in\n"
              "config.toml once you have an API key.", file=sys.stderr)
        return 1
    with _storage(cfg) as db:
        scanner = Scanner(cfg, db, _client(cfg))
        stores = scanner.refresh_stores(force=args.refresh)
        if not stores:
            print("No stores found. Try a larger location.radius_miles.")
            return 1
        print(f"{len(stores)} Best Buy stores within "
              f"{cfg.get('location.radius_miles')} miles of {cfg.get('location.postal_code')}:\n")
        print(f"{'ID':<8}{'Distance':>9}  {'City':<18}{'Type':<14}Name")
        for store in stores:
            print(f"{str(store.get('storeId')):<8}"
                  f"{float(store.get('distance') or 0):>7.1f}mi  "
                  f"{str(store.get('city') or ''):<18}"
                  f"{str(store.get('storeType') or ''):<14}"
                  f"{store.get('name') or ''}")
    return 0


def cmd_discover(args, cfg: Config) -> int:
    with _storage(cfg) as db:
        if cfg.backend == "feeds":
            deals = FeedScanner(cfg, db, _feed_client(cfg)).discover()
            if not deals:
                print("No matching deals. Loosen search.include_keywords, add more\n"
                      "feeds.queries, or clear feeds.retailers.")
                return 1
            print(f"{len(deals)} matching deals:\n")
            for deal in sorted(deals, key=lambda d: d.posted_at or datetime.min.replace(
                    tzinfo=timezone.utc), reverse=True):
                when = deal.posted_at.strftime("%Y-%m-%d") if deal.posted_at else "unknown"
                print(f"  {when}  {money(deal.price) if deal.price else 'n/a':>11}  "
                      f"{condition_label(deal.condition):<20} "
                      f"{(deal.retailer or '-'):<12} {deal.title[:58]}")
                print(f"              {deal.url}")
            return 0

        scanner = Scanner(cfg, db, _client(cfg))
        products = scanner.discover_products()
        if not products:
            print("No matching products. Loosen search.include_keywords or search.models.")
            return 1
        print(f"{len(products)} matching Zephyrus SKUs:\n")
        for sku, (product, raw) in sorted(products.items(), key=lambda kv: kv[1][0].name):
            price = raw.get("salePrice") or raw.get("regularPrice")
            print(f"  {sku:<10} {money(price) if price else 'n/a':>11}  "
                  f"{str(raw.get('condition') or 'New'):<12} {product.name[:70]}")
    return 0


def cmd_scan(args, cfg: Config) -> int:
    notifiers = build_notifiers(cfg)
    with _storage(cfg) as db:
        scanner = _scanner(cfg, db)
        try:
            result = scanner.run(dry_run=args.dry_run, notifiers=notifiers)
        except AuthError as exc:
            print(f"API key problem: {exc}", file=sys.stderr)
            return 1
        except (BestBuyError, FeedError) as exc:
            print(f"{cfg.backend} backend error: {exc}", file=sys.stderr)
            return 2

        summary = (f"[{cfg.backend}] scanned {result.products} products / "
                   f"{len(result.offers)} offers in {result.duration_s:.1f}s "
                   f"({result.api_requests} requests)")
        if result.baseline:
            summary += " -- baseline recorded, no alerts sent on a first scan"
        elif result.alerts:
            label = "would send" if args.dry_run else "sent"
            summary += f" -- {len(result.alerts)} alert(s) {label}"
        else:
            summary += " -- nothing new"
        if result.suppressed:
            summary += f", {result.suppressed} suppressed by cooldown"
        if result.capped:
            summary += f", {result.capped} withheld by alerts.max_per_scan"
        print(summary)

        if args.dry_run and result.alerts:
            print()
            dispatch([n for n in notifiers if n.name == "console"], result.alerts)
        if result.failed_channels:
            print(f"Warning: these channels failed: {', '.join(result.failed_channels)}",
                  file=sys.stderr)
            return 3
    return 0


def cmd_watch(args, cfg: Config) -> int:
    interval = max(60, args.interval * 60)
    jitter = max(0, int(interval * 0.1))
    stop = {"flag": False}

    def handle(_signum, _frame):
        stop["flag"] = True
        print("\nStopping after the current cycle...")

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)

    print(f"Watching every {args.interval} min. Ctrl-C to stop.")
    consecutive_failures = 0
    while not stop["flag"]:
        try:
            cmd_scan(args, cfg)
            consecutive_failures = 0
        except AuthError as exc:
            print(f"Fatal: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            consecutive_failures += 1
            log.error("Scan failed (%d in a row): %s", consecutive_failures, exc)
            if consecutive_failures >= 5:
                print("Five consecutive failures; giving up.", file=sys.stderr)
                return 2
        if stop["flag"]:
            break
        # Jitter keeps a cron-like cadence from hammering the same second.
        time.sleep(interval + random.randint(-jitter, jitter))
    return 0


def cmd_report(args, cfg: Config) -> int:
    with _storage(cfg) as db:
        if args.html:
            path = html_report(db, args.html, cfg)
            print(f"Wrote {path.resolve()}")
        else:
            print(console_report(db))
    return 0


def cmd_history(args, cfg: Config) -> int:
    with _storage(cfg) as db:
        conditions = [args.condition] if args.condition else [
            row["condition"] for row in db.conn.execute(
                "SELECT DISTINCT condition FROM observations WHERE sku=?", (args.sku,))
        ]
        if not conditions:
            print(f"No history for SKU {args.sku}.")
            return 1
        print(f"{db.product_name(args.sku)}  (SKU {args.sku})\n")
        for condition in conditions:
            rows = db.price_history(args.sku, condition, limit=args.limit)
            if not rows:
                continue
            print(f"  {condition_label(condition)}")
            for row in reversed(rows):
                stock = "in stock" if row["available"] else "gone"
                stores = f"  {row['store_count']} store(s)" if row["store_count"] else ""
                print(f"    {row['ts'][:16].replace('T', ' ')}  "
                      f"{money(row['price']):>11}  {stock}{stores}")
            low = db.lowest_price(args.sku, condition)
            print(f"    lowest recorded: {money(low)}\n")
    return 0


def cmd_alerts(args, cfg: Config) -> int:
    with _storage(cfg) as db:
        rows = db.recent_alerts(args.limit)
        if not rows:
            print("No alerts recorded yet.")
            return 0
        for row in rows:
            print(f"{row['ts'][:16].replace('T', ' ')}  [{row['kind']}]  "
                  f"{db.product_name(row['sku'])[:58]}")
            print(f"    {row['message']}")
    return 0


def cmd_test_alert(args, cfg: Config) -> int:
    notifiers = build_notifiers(cfg)
    if not notifiers:
        print("No notification channels enabled.", file=sys.stderr)
        return 1
    sample = Alert(
        kind="openbox_restock",
        sku="6571749",
        condition="openbox-excellent",
        product_name="ASUS ROG Zephyrus G14 14\" OLED Gaming Laptop (TEST ALERT)",
        message="Open-Box: Excellent just appeared at $1,299.99 (23.5% off $1,699.99)",
        price=1299.99,
        regular_price=1699.99,
        discount_pct=23.5,
        url="https://www.bestbuy.com/",
        dedupe_key="test",
        severity=2,
        stores=["Best Buy Cambridge (Cambridge)", "Best Buy Watertown (Watertown)"],
    )
    failed = dispatch(notifiers, [sample])
    sent = [n.name for n in notifiers if n.name not in failed]
    print(f"Delivered to: {', '.join(sent) or 'nothing'}")
    if failed:
        print(f"Failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


# -------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zephyrus",
        description="Track Best Buy pricing for ASUS ROG Zephyrus laptops around Boston.",
    )
    parser.add_argument("--config", "-c", help="path to config.toml (default: ./config.toml)")
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="write a starter config.toml")
    p.add_argument("--force", action="store_true", help="overwrite an existing config")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("check", help="validate config, API key and notification channels")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("stores", help="list Best Buy stores near the configured ZIP")
    p.add_argument("--refresh", action="store_true", help="re-query instead of using the cache")
    p.set_defaults(func=cmd_stores)

    p = sub.add_parser("discover", help="list the Zephyrus SKUs that match your filters")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("scan", help="run one price check and send any alerts")
    p.add_argument("--dry-run", action="store_true",
                   help="detect and store, but print alerts instead of sending them")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("watch", help="scan on a loop")
    p.add_argument("--interval", type=int, default=30, help="minutes between scans (default 30)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("report", help="show current prices, or write an HTML report")
    p.add_argument("--html", nargs="?", const="report.html",
                   help="write an HTML report (default path: report.html)")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("history", help="price history for one SKU")
    p.add_argument("sku")
    p.add_argument("--condition", help="e.g. new, openbox-excellent")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("alerts", help="recently sent alerts")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_alerts)

    p = sub.add_parser("test-alert", help="send a sample alert through every channel")
    p.set_defaults(func=cmd_test_alert)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        cfg = Config.load(args.config)
        return args.func(args, cfg)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
