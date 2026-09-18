"""Human-facing output: a console summary and a self-contained HTML report."""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

from .detect import money
from .models import condition_label, condition_rank, is_open_box

# Validated palette slots (see references/palette.md). Light / dark pairs.
PALETTE = {
    "surface":    ("#fcfcfb", "#1a1a19"),
    "surface_2":  ("#f0efec", "#232322"),
    "border":     ("#e2e1dc", "#383835"),
    "text":       ("#0b0b0b", "#ffffff"),
    "text_2":     ("#52514e", "#c3c2b7"),
    "series":     ("#2a78d6", "#3987e5"),
    "good":       ("#0ca30c", "#0ca30c"),
    "warning":    ("#fab219", "#fab219"),
    "critical":   ("#d03b3b", "#d03b3b"),
}

SPARK_W, SPARK_H, SPARK_PAD = 170, 38, 5


def _rows_for_report(db) -> list[dict]:
    """Current state of every tracked offer, newest price history attached."""
    rows = []
    for product in db.get_products():
        offers = []
        for state in db.conn.execute(
            "SELECT * FROM offer_state WHERE sku=? ORDER BY condition", (product["sku"],)
        ):
            history = db.price_history(product["sku"], state["condition"], limit=200)
            offers.append({
                "condition": state["condition"],
                "label": condition_label(state["condition"]),
                "price": state["price"],
                "regular_price": state["regular_price"],
                "available": bool(state["available"]),
                "store_count": state["store_count"],
                "low": db.lowest_price(product["sku"], state["condition"]),
                "history": list(reversed(history)),  # oldest -> newest
                "open_box": is_open_box(state["condition"]),
            })
        if offers:
            offers.sort(key=lambda o: condition_rank(o["condition"]))
            rows.append({"product": product, "offers": offers})
    rows.sort(key=lambda r: r["product"]["name"])
    return rows


def _pct_off(price, regular) -> float | None:
    if price is None or not regular or regular <= 0:
        return None
    return round((regular - price) / regular * 100, 1)


# ------------------------------------------------------------------- console


def console_report(db) -> str:
    rows = _rows_for_report(db)
    if not rows:
        return "No products tracked yet. Run `zephyrus scan` first."

    out: list[str] = []
    last = db.last_scan()
    if last:
        out.append(f"Last scan: {last['ts']}  ({last['products']} products, "
                   f"{last['offers']} offers, {last['alerts']} alerts, "
                   f"{last['api_requests']} API calls)")
    stores = db.get_stores()
    if stores:
        out.append(f"Tracking {len(stores)} Best Buy stores near Boston: "
                   + ", ".join(f"{s['city']}" for s in stores[:8])
                   + (" ..." if len(stores) > 8 else ""))
    out.append("")

    for row in rows:
        product = row["product"]
        out.append(f"{product['name']}")
        out.append(f"  SKU {product['sku']}  {product['model_number']}")
        for offer in row["offers"]:
            pct = _pct_off(offer["price"], offer["regular_price"])
            bits = [f"{offer['label']:<24}", f"{money(offer['price']):>11}"]
            bits.append(f"{pct:>5.1f}% off" if pct is not None else " " * 10)
            bits.append("in stock" if offer["available"] else "unavailable")
            if offer["low"] is not None and offer["price"] is not None and offer["low"] < offer["price"]:
                bits.append(f"low {money(offer['low'])}")
            if offer["store_count"]:
                bits.append(f"{offer['store_count']} store(s) nearby")
            out.append("    " + "  ".join(bits))
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------- HTML


def _sparkline(history: list) -> str:
    """Step-interpolated price line.

    Prices are a step function -- a price holds until it changes -- so straight
    interpolation between observations would draw drifts that never happened.
    """
    points = [(h["ts"], h["price"]) for h in history if h["price"] is not None]
    if len(points) < 2:
        return '<div class="spark-empty">not enough history</div>'

    values = [p for _ts, p in points]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    inner_w = SPARK_W - SPARK_PAD * 2
    inner_h = SPARK_H - SPARK_PAD * 2

    def xy(index: int, value: float) -> tuple[float, float]:
        x = SPARK_PAD + (inner_w * index / (len(points) - 1))
        y = SPARK_PAD + inner_h - ((value - lo) / span) * inner_h
        return round(x, 2), round(y, 2)

    # Step-after path: hold the old price, then jump at the observation.
    commands = []
    prev_y = None
    for i, (_ts, value) in enumerate(points):
        x, y = xy(i, value)
        if i == 0:
            commands.append(f"M{x},{y}")
        else:
            commands.append(f"L{x},{prev_y} L{x},{y}")
        prev_y = y
    path = " ".join(commands)

    end_x, end_y = xy(len(points) - 1, values[-1])
    low_i = values.index(lo)
    at_low = low_i == len(points) - 1
    first, last_ts = points[0][0][:10], points[-1][0][:10]

    # A separate low marker would sit exactly under the end marker when the
    # current price is the lowest, so in that case just colour the end marker.
    if at_low:
        markers = (f'<circle cx="{end_x}" cy="{end_y}" r="3.5" fill="var(--good)" '
                   f'stroke="var(--surface)" stroke-width="2"/>')
    else:
        low_x, low_y = xy(low_i, lo)
        markers = (
            f'<circle cx="{low_x}" cy="{low_y}" r="3" fill="var(--good)" '
            f'stroke="var(--surface)" stroke-width="2"/>'
            f'<circle cx="{end_x}" cy="{end_y}" r="3.5" fill="var(--series)" '
            f'stroke="var(--surface)" stroke-width="2"/>'
        )

    return f"""<svg class="spark" viewBox="0 0 {SPARK_W} {SPARK_H}" width="{SPARK_W}" height="{SPARK_H}"
      role="img" aria-label="Price history from {money(values[0])} to {money(values[-1])},
      low {money(lo)}, high {money(hi)}">
      <title>{len(points)} price changes, {first} to {last_ts} — high {money(hi)}, low {money(lo)}{' — currently at its lowest' if at_low else ''}</title>
      <path d="{path}" fill="none" stroke="var(--series)" stroke-width="2"
            stroke-linejoin="round" stroke-linecap="round"/>
      {markers}
    </svg>"""


def _badge(pct: float | None, heavy: float) -> str:
    if pct is None:
        return '<span class="badge muted">&mdash;</span>'
    if pct >= heavy * 1.5:
        cls, icon = "good", "&#9660;&#9660;"   # double down-arrow
    elif pct >= heavy:
        cls, icon = "good", "&#9660;"
    elif pct > 0:
        cls, icon = "warn", "&#9660;"
    else:
        cls, icon = "muted", ""
    return f'<span class="badge {cls}">{icon} {pct:.1f}% off</span>'


def html_report(db, out_path: str | Path, config=None) -> Path:
    rows = _rows_for_report(db)
    heavy = float(config.get("thresholds.heavy_discount_pct", 15.0)) if config else 15.0
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    stores = db.get_stores()
    last = db.last_scan()

    cards = []
    for row in rows:
        product = row["product"]
        offer_rows = []
        for offer in row["offers"]:
            pct = _pct_off(offer["price"], offer["regular_price"])
            price_txt = money(offer["price"]) if offer["price"] is not None else "&mdash;"
            regular_txt = money(offer["regular_price"]) if offer["regular_price"] else "&mdash;"
            low_txt = money(offer["low"]) if offer["low"] is not None else "&mdash;"
            if offer["available"]:
                stock = '<span class="stock in">&#9679; In stock</span>'
            else:
                stock = '<span class="stock out">&#9675; Unavailable</span>'
            pickup = (f'<span class="pickup">&#127968; {offer["store_count"]} Boston store'
                      f'{"s" if offer["store_count"] != 1 else ""}</span>'
                      if offer["store_count"] else "")
            offer_rows.append(f"""
              <tr class="{'openbox' if offer['open_box'] else ''}">
                <td class="cond">{html.escape(offer['label'])}</td>
                <td class="num price">{price_txt}</td>
                <td class="num dim">{regular_txt}</td>
                <td>{_badge(pct, heavy)}</td>
                <td class="num dim">{low_txt}</td>
                <td>{stock} {pickup}</td>
                <td class="sparkcell">{_sparkline(offer['history'])}</td>
              </tr>""")

        image = (f'<img src="{html.escape(product["image"])}" alt="" loading="lazy">'
                 if product["image"] else "")
        link = (f'<a href="{html.escape(product["url"])}">View on Best Buy &rarr;</a>'
                if product["url"] else "")
        cards.append(f"""
        <section class="card">
          <header>
            <div class="thumb">{image}</div>
            <div class="meta">
              <h2>{html.escape(product['name'])}</h2>
              <p class="dim">SKU {html.escape(product['sku'])}
                 {('&middot; ' + html.escape(product['model_number'])) if product['model_number'] else ''}
                 &middot; first seen {product['first_seen'][:10]}</p>
              {link}
            </div>
          </header>
          <table>
            <thead><tr>
              <th>Condition</th><th class="num">Price</th><th class="num">List</th>
              <th>Discount</th><th class="num">Lowest seen</th><th>Availability</th>
              <th>Price history</th>
            </tr></thead>
            <tbody>{''.join(offer_rows)}</tbody>
          </table>
        </section>""")

    store_list = ", ".join(f"{html.escape(s['name'])} ({html.escape(s['city'])})" for s in stores[:12])
    scan_line = (f"{last['products']} products &middot; {last['offers']} offers &middot; "
                 f"{last['alerts']} alerts &middot; {last['api_requests']} API calls"
                 if last else "no scans yet")

    light = {k: v[0] for k, v in PALETTE.items()}
    dark = {k: v[1] for k, v in PALETTE.items()}
    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Zephyrus Price Report</title>
<style>
  :root {{
    color-scheme: light;
{_css_vars(light)}
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      color-scheme: dark;
{_css_vars(dark)}
    }}
  }}
  :root[data-theme="dark"] {{
    color-scheme: dark;
{_css_vars(dark)}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 24px 16px 64px;
    background: var(--surface); color: var(--text);
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  }}
  .wrap {{ max-width: 1080px; margin: 0 auto; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .dim {{ color: var(--text_2); }}
  .sub {{ color: var(--text_2); font-size: 13px; margin: 0 0 24px; }}
  .card {{
    background: var(--surface_2); border: 1px solid var(--border);
    border-radius: 10px; padding: 16px; margin: 0 0 16px; overflow: hidden;
  }}
  .card header {{ display: flex; gap: 14px; align-items: flex-start; margin-bottom: 12px; }}
  .thumb img {{ width: 64px; height: 64px; object-fit: contain; border-radius: 6px; background: #fff; }}
  .meta h2 {{ font-size: 16px; margin: 0 0 2px; line-height: 1.35; }}
  .meta p {{ margin: 0 0 6px; font-size: 12px; }}
  .meta a {{ color: var(--series); text-decoration: none; font-size: 13px; font-weight: 600; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13.5px; }}
  th {{
    text-align: left; font-weight: 600; font-size: 11px; letter-spacing: .04em;
    text-transform: uppercase; color: var(--text_2);
    border-bottom: 1px solid var(--border); padding: 0 10px 6px 0;
  }}
  td {{ padding: 9px 10px 9px 0; border-bottom: 1px solid var(--border); vertical-align: middle; }}
  tr:last-child td {{ border-bottom: none; }}
  tr.openbox .cond {{ color: var(--series); }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .price {{ font-weight: 700; font-size: 15px; }}
  .badge {{
    display: inline-block; padding: 2px 8px; border-radius: 999px;
    font-size: 12px; font-weight: 600; white-space: nowrap;
  }}
  .badge.good {{ color: var(--good); border: 1px solid var(--good); }}
  .badge.warn {{ color: var(--text_2); border: 1px solid var(--border); }}
  .badge.muted {{ color: var(--text_2); }}
  .stock {{ font-size: 12.5px; white-space: nowrap; }}
  .stock.in {{ color: var(--good); }}
  .stock.out {{ color: var(--text_2); }}
  .pickup {{ display: inline-block; margin-left: 6px; font-size: 12px; color: var(--text_2); }}
  .sparkcell {{ width: {SPARK_W + 10}px; }}
  .spark {{ display: block; }}
  .spark-empty {{ font-size: 11.5px; color: var(--text_2); }}
  footer {{ margin-top: 28px; font-size: 12px; color: var(--text_2); }}
  @media (max-width: 720px) {{
    table, thead, tbody, tr, td, th {{ display: block; }}
    thead {{ display: none; }}
    tr {{ border-bottom: 1px solid var(--border); padding: 8px 0; }}
    td {{ border: none; padding: 2px 0; }}
    td.num {{ text-align: left; }}
    td.cond {{ font-weight: 700; }}
  }}
</style></head>
<body><div class="wrap">
  <h1>ASUS Zephyrus &mdash; Best Buy price report</h1>
  <p class="sub">Generated {generated} &middot; {scan_line}<br>
     Stores tracked: {store_list or 'none resolved yet'}</p>
  {''.join(cards) or '<p class="dim">No products tracked yet. Run a scan first.</p>'}
  <footer>
    A green end marker means the offer is at the lowest price we've recorded; otherwise
    the green dot marks where that low happened. The line is a step
    chart &mdash; a price holds until it changes. Data from the Best Buy Developer API;
    prices and stock can move before you reach checkout.
  </footer>
</div></body></html>"""

    path = Path(out_path)
    if path.parent != Path(""):
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc, encoding="utf-8")
    return path


def _css_vars(values: dict[str, str]) -> str:
    return "\n".join(f"    --{k}: {v};" for k, v in values.items())
