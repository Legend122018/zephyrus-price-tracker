# zephyrus-price-tracker

Tracks pricing and stock for **ASUS ROG Zephyrus** laptops across **new,
open-box and refurbished** listings, and alerts you when something is heavily
discounted or when an open-box unit appears.

**Zero dependencies.** Python 3.11+ standard library only — no `pip install`,
no virtualenv required.

---

## Two backends

The tracker can run on either of two data sources. Pick with `source.backend`.

|  | `feeds` (default) | `bestbuy` |
|---|---|---|
| **Credentials** | none | free API key |
| **Works today?** | yes | only if you can get a key |
| **Sees** | deals people post publicly | Best Buy's own catalogue |
| **Coverage** | partial — whatever gets posted | every listed Zephyrus SKU |
| **Open-box tiers** | "open-box", tier rarely stated | Excellent / Certified / Satisfactory / Fair, priced separately |
| **Boston store stock** | **none at all** | per-store pickup availability |
| **% off list** | often unavailable | always |
| **Typical alert rate** | a Zephyrus open-box posting every 4–8 weeks | whenever inventory actually moves |

**The `bestbuy` backend is substantially better**, and it's free. The catch is
that Best Buy [will not issue API keys to free email
providers](https://medium.com/best-buy-developers/announcing-a-change-to-best-buy-s-api-access-b09afc4bc27a) —
gmail, outlook and `.edu` are all refused. You need a company or custom-domain
address. If you have one, use it.

The `feeds` backend exists so the tracker is useful *without* that. It reads
public RSS deal feeds (Slickdeals search by default, plus any feed URL you add)
and watches for Zephyrus postings. Be clear-eyed about what that means: it sees
what the deal community posts, not Best Buy's inventory, so it will be quiet —
measured against live data, Zephyrus open-box deals surface roughly every 4–8
weeks. It will not tell you a G14 just appeared at the Cambridge store.

---

## Quick start

```bash
git clone <your-repo-url> && cd zephyrus-price-tracker

python3 -m zephyrus_tracker init       # writes config.toml
python3 -m zephyrus_tracker check      # confirms feeds are reachable
python3 -m zephyrus_tracker discover   # what it can see right now
python3 -m zephyrus_tracker scan
```

No key, no signup. The first scan records everything it finds as a **baseline
and deliberately sends nothing** — it has nothing to compare against yet. From
the second scan on, it only tells you what changed.

To use the official API instead, once you have a key:

```toml
[source]
backend = "bestbuy"
```

```bash
export BESTBUY_API_KEY=your_key
python3 -m zephyrus_tracker check
```

---

## What it watches

With `bestbuy`, one `(SKU, condition)` pair per listing:

| Condition | Source |
|---|---|
| New | Products API — regular price, sale price, % off |
| Open-Box: Excellent / Certified / Satisfactory / Fair | Open Box API (beta), each tier priced separately |
| Geek Squad Refurbished | Products API (`condition=refurbished`) |
| Boston pickup stock | Per-SKU store availability, filtered to stores near your ZIP |

With `feeds`, one tracked item per posting, classified as new / open-box /
refurbished from its title, with the retailer identified where possible.

### Stores (`bestbuy` only)

`location.postal_code = "02108"` with a 25-mile radius covers the Best Buy
locations Boston shoppers actually use — Cambridge, Watertown, Dorchester/South
Bay, Everett, Saugus, Dedham, Braintree, Framingham. Run `zephyrus stores` to
see which resolved, then pin specific ones with `location.store_ids`.

---

## What triggers an alert

| Alert | Fires when |
|---|---|
| `openbox_restock` | An open-box or refurbished offer **appears or returns** |
| `all_time_low` | Price beats every price recorded for that offer |
| `heavy_discount` | ≥ 20% off list (new) or ≥ 25% (open-box/refurb) |
| `price_drop` | Dropped ≥ 5% **or** ≥ $100 since the last check |
| `boston_pickup` | First time a tracked Boston store has it (`bestbuy` only) |
| `new_product` | A Zephyrus listing that wasn't there before |

Discounts are measured against the regular **list** price, so a sale and an
open-box markdown stack. Best Buy open-box Excellent is routinely ~10% off on
its own, so the open-box bar deliberately sits *above* the new-unit bar — set it
lower and essentially every open-box listing would alert. **Restock alerts
ignore these numbers entirely**: an open-box unit appearing always alerts,
however modest its discount.

Noise control, in `[thresholds]` and `[alerts]`:

- **The first scan is silent.** It records a baseline instead of alerting on
  everything at once.
- **At most two alerts per offer per scan** — one stock event, one price event.
- **`cooldown_hours = 24`** suppresses repeats of an identical alert.
- **`max_per_scan = 12`** caps a flood, which usually means something upstream
  changed shape rather than twelve genuine deals landing at once.
- **`min_drop_dollars = 40`** ignores jitter — $20 is noise on a $2,000 laptop.
- **`max_price`** mutes anything above your budget.

---

## Getting alerts on your phone

The easiest option — no account, no app registration:

```toml
[notify.ntfy]
enabled = true
topic   = "zephyrus-bos-7f3a91"   # pick something unguessable; it's the password
```

Install [ntfy](https://ntfy.sh), subscribe to that topic, done. Tapping an alert
opens the listing.

Also supported: **email** (Gmail app passwords work directly), **Slack** and
**Discord** webhooks (auto-formatted), any **generic JSON webhook**, the console,
and an `alerts.jsonl` audit log.

```bash
python3 -m zephyrus_tracker test-alert   # sends a sample through every channel
```

A failing channel never blocks the others, and never blocks the scan.

---

## Running it continuously

```bash
python3 -m zephyrus_tracker watch --interval 30
```

For something that survives a reboot:

- **cron** — `deploy/crontab.example`
- **systemd timer** — `deploy/zephyrus.service` + `deploy/zephyrus.timer`
- **GitHub Actions** — `.github/workflows/scan.yml`, runs on GitHub's machines
  with the price database cached between runs and an HTML report attached to
  every run. On the `feeds` backend it needs no secrets at all. Its header lists
  what to add for phone push and for switching to the `bestbuy` backend.

---

## Commands

```
zephyrus init                    write a starter config.toml
zephyrus check                   validate config, feeds / API key, and channels
zephyrus stores [--refresh]      Best Buy stores near your ZIP (bestbuy backend)
zephyrus discover                what the configured backend can see right now
zephyrus scan [--dry-run]        one check; --dry-run prints instead of sending
zephyrus watch --interval 30     scan on a loop
zephyrus report [--html [PATH]]  current prices, or a standalone HTML report
zephyrus history SKU             price history for one tracked item
zephyrus alerts                  recently sent alerts
zephyrus test-alert              send a sample alert through every channel
```

`python3 -m zephyrus_tracker <command>` works without installing.
`pip install -e .` gets you the shorter `zephyrus` command.

### The HTML report

`docs/preview.html` is a standalone preview of the interface built on simulated
data. For real numbers, `zephyrus report --html` writes a single self-contained
file — no JavaScript, no CDN — with every tracked offer, its discount, its
lowest recorded price and a step-chart sparkline of its history, in both light
and dark themes.

---

## Configuration

`config.example.toml` documents every option. The ones worth changing first:

```toml
[source]
backend = "feeds"          # or "bestbuy" once you have an API key

[feeds]
retailers = []             # e.g. ["Best Buy"] to ignore other stores
max_age_days = 180

[thresholds]
heavy_discount_pct    = 20.0   # lower to 15 to hear about routine sales
open_box_discount_pct = 25.0
max_price             = 0      # e.g. 1800 to only hear about sub-$1800 deals

[search]
models = []                    # e.g. ["G14"] to track only the 14-inch
```

Secrets stay out of the file: `BESTBUY_API_KEY`, `ZEPHYRUS_BACKEND`,
`ZEPHYRUS_NTFY_TOPIC`, `ZEPHYRUS_WEBHOOK_URL`, `ZEPHYRUS_SMTP_PASSWORD` and
`ZEPHYRUS_DB` override `config.toml`. `config.toml` and the database are both
gitignored.

---

## Worth knowing

- **The `feeds` backend will be quiet.** Zephyrus open-box postings surface
  every 4–8 weeks in the feeds, against Best Buy inventory that turns over
  daily. Quiet is the expected state, not a malfunction.
- **Feed search is fuzzy.** An "open box" query genuinely returns MacBooks, so
  `search.include_keywords` does real work. Loosen it and you will get noise.
- **No store data on `feeds`.** Boston pickup stock is a `bestbuy`-only feature;
  the feeds carry no store information whatsoever.
- **List prices are often missing on `feeds`,** so a percent-off figure
  frequently is too. Price-drop and all-time-low detection still work, since
  those compare against the item's own history.
- **Feeds are public and rate-limited by IP.** Reddit in particular returns 429
  to datacenter addresses; it may work fine from a home connection.
- **Treat an alert as "go look now."** Deal postings go stale, and open-box
  units are single items.

---

## Tests

```bash
python3 -m unittest discover -s tests -v
```

49 tests, no network and no credentials required: discovery filters, open-box
parsing and restock detection, every alert rule, first-scan baseline behaviour,
flood capping, cooldown suppression, store filtering, change-only history,
config merging, report rendering, and drift between the shipped example config
and the in-code defaults. Feed parsing is tested against a real Slickdeals
response saved in `tests/fixtures/`.

`.github/workflows/tests.yml` runs them on every push, on Python 3.11 and 3.12.

---

## Layout

```
zephyrus_tracker/
  bestbuy.py   Best Buy API client: rate limiting, retries, key redaction
  feeds.py     Deal-feed client: fetch, parse, classify condition and retailer
  config.py    TOML config + env overrides + per-backend validation
  models.py    Product / Offer / Alert value types
  storage.py   SQLite: products, price history, offer state, sent alerts
  scan.py      AlertPipeline (shared) + Scanner (API) + FeedScanner (feeds)
  detect.py    The alert rules
  notify.py    Console, JSONL, email, ntfy, Slack/Discord/generic webhook
  report.py    Console summary + self-contained HTML report
  cli.py       Command-line interface
```

Both backends produce the same `Product` / `Offer` shapes, so everything
downstream of discovery — storage, detection, notification, reporting — is
shared. Adding a third source means writing one client.

## License

MIT.
