# zephyrus-price-tracker

Tracks Best Buy pricing and stock for **ASUS ROG Zephyrus** laptops in the
**Boston area**, across **new, open-box and Geek Squad refurbished** listings,
and alerts you when something is heavily discounted or when an open-box unit
comes back in stock.

Built on the **official Best Buy Developer API** — not scraping. That means it
keeps working, it doesn't get blocked, and it's within Best Buy's terms.

**Zero dependencies.** Python 3.11+ standard library only — no `pip install`,
no virtualenv required.

---

## Quick start

```bash
git clone <your-repo-url> && cd zephyrus-price-tracker

# 1. Free API key, issued instantly: https://developer.bestbuy.com/
export BESTBUY_API_KEY=your_key_here

# 2. Write a config file
python3 -m zephyrus_tracker init

# 3. Confirm the key, the Boston store list and your alert channels
python3 -m zephyrus_tracker check

# 4. See what it found, then take a first snapshot
python3 -m zephyrus_tracker discover
python3 -m zephyrus_tracker scan
```

The first scan establishes a baseline, so it's quiet by design. From the second
scan on, it only tells you what changed.

---

## What it watches

| Condition | Source | Notes |
|---|---|---|
| **New** | Products API | Regular price, sale price, % off |
| **Open-Box: Excellent / Certified / Satisfactory / Fair** | Open Box API (beta) | Each tier priced and tracked separately |
| **Geek Squad Refurbished** | Products API (`condition=refurbished`) | Listed as its own SKU |
| **Boston pickup stock** | Per-SKU store availability | Filtered to stores near your ZIP |

Each `(SKU, condition)` pair gets its own price history, so "Open-Box Excellent
on the G14" is tracked independently of the new unit and of the Fair-condition
listing.

### Stores

`location.postal_code = "02108"` with a 25-mile radius covers the Best Buy
locations Boston shoppers actually use — Cambridge, Watertown, Dorchester/South
Bay, Everett, Saugus, Dedham, Braintree, Framingham and neighbours. Run
`zephyrus stores` to see exactly which ones resolved, then pin specific ones
with `location.store_ids` if you'd rather not drive to Framingham.

---

## What triggers an alert

| Alert | Fires when |
|---|---|
| `openbox_restock` | An open-box or refurbished offer **appears or returns** — the one you asked for |
| `all_time_low` | Price beats every price recorded for that offer |
| `heavy_discount` | ≥ 20% off list (new) or ≥ 25% (open-box/refurb) — both configurable |
| `price_drop` | Dropped ≥ 5% **or** ≥ $75 since the last check |
| `boston_pickup` | First time a tracked Boston store has it on the shelf |
| `new_product` | A Zephyrus SKU Best Buy didn't list before |

Discounts are measured against the regular **list** price, so a sale and an
open-box markdown stack. Best Buy open-box Excellent is routinely ~10% off on its
own, so the open-box bar deliberately sits *above* the new-unit bar — set it
lower and essentially every open-box listing would alert. **Restock alerts
ignore these numbers entirely**: an open-box unit returning to stock always
alerts, however modest its discount.

Noise control, all in `[thresholds]` and `[alerts]`:

- **At most two alerts per offer per scan** — one stock event, one price event.
  A genuinely great deal is one alert, not three overlapping ones.
- **`cooldown_hours = 24`** suppresses repeats of an identical alert.
- **`min_drop_dollars = 40`** ignores jitter — on a $2,000 laptop, $20 is noise.
- **`max_price`** mutes anything above your budget entirely.

---

## Getting alerts on your phone

The easiest option — no account, no app registration:

```toml
[notify.ntfy]
enabled = true
topic   = "zephyrus-bos-7f3a91"   # pick something unguessable; it's the password
```

Install [ntfy](https://ntfy.sh) on your phone, subscribe to that topic, done.
Alerts arrive as push notifications and tapping one opens the Best Buy page.

Also supported: **email** (Gmail app passwords work directly), **Slack** and
**Discord** webhooks (auto-formatted), any **generic JSON webhook**, the
console, and an `alerts.jsonl` audit log. Enable as many as you like, then:

```bash
python3 -m zephyrus_tracker test-alert   # sends a sample through every channel
```

A failing channel never blocks the others, and never blocks the scan.

---

## Running it continuously

Open-box listings are single units that often disappear within hours, so
frequency matters more than for new-stock pricing.

```bash
python3 -m zephyrus_tracker watch --interval 30     # foreground loop
```

For something that survives a reboot, pick one:

- **cron** — `deploy/crontab.example`
- **systemd timer** — `deploy/zephyrus.service` + `deploy/zephyrus.timer`
- **GitHub Actions** — `.github/workflows/scan.yml`, runs on GitHub's machines
  with the price-history database cached between runs, and attaches an HTML
  report to every run. No always-on computer. The workflow header lists the
  secrets to add; until `BESTBUY_API_KEY` exists the scheduled runs finish green
  with a note telling you where to add it, rather than failing and mailing you
  every half hour.

A 30-minute interval across ~20 SKUs is roughly 1,000 API calls/day against a
50,000/day quota.

---

## Commands

```
zephyrus init                    write a starter config.toml
zephyrus check                   validate key, config and notification channels
zephyrus stores [--refresh]      list Best Buy stores near your ZIP, with IDs
zephyrus discover                list the Zephyrus SKUs matching your filters
zephyrus scan [--dry-run]        one price check; --dry-run prints instead of sending
zephyrus watch --interval 30     scan on a loop
zephyrus report [--html [PATH]]  current prices, or a standalone HTML report
zephyrus history SKU             price history for one SKU
zephyrus alerts                  recently sent alerts
zephyrus test-alert              send a sample alert through every channel
```

`python3 -m zephyrus_tracker <command>` works identically without installing.
`pip install -e .` gets you the shorter `zephyrus` command.

### The HTML report

`docs/preview.html` is a standalone preview of the interface built on simulated
data — open it in a browser to see the shape of the output before you have a key.
For real numbers, `zephyrus report --html` writes a single self-contained file — no JavaScript, no
CDN, no network calls — with every tracked offer, its discount, its lowest
recorded price, Boston pickup status and a step-chart sparkline of its price
history. Light and dark themes both included.

---

## Configuration

`config.example.toml` documents every option. The ones worth changing first:

```toml
[location]
postal_code = "02108"      # your ZIP
radius_miles = 25

[thresholds]
heavy_discount_pct    = 20.0   # lower to 15 if you want to hear about routine sales
open_box_discount_pct = 25.0   # open-box starts ~10% off, so the bar sits higher
max_price             = 0      # e.g. 1800 to only hear about sub-$1800 deals

[search]
models = []                    # e.g. ["G14"] to track only the 14-inch
```

Secrets can stay out of the file entirely: `BESTBUY_API_KEY`,
`ZEPHYRUS_NTFY_TOPIC`, `ZEPHYRUS_WEBHOOK_URL`, `ZEPHYRUS_SMTP_PASSWORD` and
`ZEPHYRUS_DB` override whatever is in `config.toml`. `config.toml` and the
database are both gitignored.

---

## Worth knowing

- **An API key is required.** It's free and instant, but there's no way around
  it — Best Buy's storefront is bot-protected, and scraping it would be both
  fragile and against their terms. The API is the supported path.
- **Open-box stock is reported nationally, not per store.** Best Buy's API
  exposes open-box offers and their condition tiers, but not which store holds
  a given unit. Most open-box orders ship or offer pickup at checkout. The
  per-store availability you see in alerts and reports is for the *new* listing.
- **API data can trail the website by a few minutes.** For a fast-moving
  open-box unit, treat an alert as "go look now", not as a reservation.
- **The first scan is quiet.** It has nothing to compare against yet.
  `all_time_low` needs three observations before it means anything.
- **Refurbished coverage depends on Best Buy listing it.** Geek Squad
  refurbished Zephyrus units come and go; when one exists, it's picked up
  automatically as its own SKU.

---

## Tests

```bash
python3 -m unittest discover -s tests -v
```

29 tests covering discovery filters, open-box parsing and restock detection,
every alert rule, cooldown suppression, store filtering, change-only history,
config merging, report rendering, and drift between the shipped example config
and the in-code defaults. They run against a scripted fake API client, so no key
and no network are needed — which is why CI needs no secrets either.

`.github/workflows/tests.yml` runs them on every push to `main`, on Python 3.11
and 3.12.

---

## Layout

```
zephyrus_tracker/
  bestbuy.py   Best Buy API client: rate limiting, retries, key redaction
  config.py    TOML config + env overrides + validation
  models.py    Product / Offer / Alert value types
  storage.py   SQLite: products, price history, offer state, sent alerts
  scan.py      Orchestration: discover -> price -> availability -> detect
  detect.py    The alert rules
  notify.py    Console, JSONL, email, ntfy, Slack/Discord/generic webhook
  report.py    Console summary + self-contained HTML report
  cli.py       Command-line interface
```

## License

MIT.
