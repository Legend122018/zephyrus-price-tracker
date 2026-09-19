"""Standalone live dashboard generated from the tracker database.

``zephyrus dashboard`` writes a single self-contained HTML file: no JavaScript
libraries, no CDN, no network calls at view time. Every figure is baked in at
generation time, so the file is as fresh as the scan that produced it -- run it
after each scan and serve the output, and the page is live.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

from .models import condition_label, is_open_box

TEMPLATE = r"""<title>Zephyrus Deal Watch</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap">
<style>
  :root {
    color-scheme: light;
    --ground:#f4f6f9; --surface:#fff; --surface-2:#eef1f6; --line:#dde3ec;
    --ink:#10151d; --ink-2:#586273; --ink-3:#8790a1;
    --accent:#2a78d6; --s-new:#2a78d6; --s-ob:#eb6834;
    --good:#0ca30c; --good-bg:#e9f7e9; --good-line:#b6e3b6;
    --shadow:0 1px 2px rgba(16,21,29,.06),0 8px 24px -12px rgba(16,21,29,.18);
    --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
    --sans:"IBM Plex Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --ground:#0d1117; --surface:#161b23; --surface-2:#1c222c; --line:#29313d;
      --ink:#e9edf4; --ink-2:#9aa4b4; --ink-3:#6e7888;
      --accent:#3987e5; --s-new:#3987e5; --s-ob:#d95926;
      --good:#0ca30c; --good-bg:#14261a; --good-line:#255a28;
      --shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px -12px rgba(0,0,0,.6);
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --ground:#0d1117; --surface:#161b23; --surface-2:#1c222c; --line:#29313d;
    --ink:#e9edf4; --ink-2:#9aa4b4; --ink-3:#6e7888;
    --accent:#3987e5; --s-new:#3987e5; --s-ob:#d95926;
    --good:#0ca30c; --good-bg:#14261a; --good-line:#255a28;
    --shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px -12px rgba(0,0,0,.6);
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--sans);
       font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}
  .wrap{max-width:980px;margin:0 auto;padding-inline:20px;padding-block:0 72px}
  header.top{position:sticky;top:env(safe-area-inset-top,0px);z-index:20;
    background:color-mix(in srgb,var(--ground) 88%,transparent);backdrop-filter:blur(10px);
    border-bottom:1px solid var(--line)}
  .top-inner{max-width:980px;margin:0 auto;padding:12px 20px;display:flex;
    align-items:center;gap:12px;flex-wrap:wrap}
  .mark{font-family:var(--mono);font-weight:600;font-size:13px;letter-spacing:.1em;
    text-transform:uppercase;display:flex;align-items:center;gap:8px}
  .dot-live{width:7px;height:7px;border-radius:50%;background:var(--good);
    box-shadow:0 0 0 3px color-mix(in srgb,var(--good) 22%,transparent)}
  .stamp{margin-left:auto;font-family:var(--mono);font-size:11px;letter-spacing:.06em;
    text-transform:uppercase;color:var(--ink-2);border:1px solid var(--line);
    background:var(--surface);padding:3px 9px;border-radius:4px}
  h1{font-size:clamp(27px,5.2vw,38px);line-height:1.1;margin:0 0 10px;
     letter-spacing:-.025em;font-weight:700;text-wrap:balance}
  .lede{font-size:16px;color:var(--ink-2);max-width:60ch;margin:0 0 22px}
  .lede b{color:var(--ink);font-weight:600}
  .freshness{font-family:var(--mono);font-size:11.5px;color:var(--ink-3);margin:-14px 0 20px}
  .freshness .age{color:var(--ink-2)}
  .hero{padding-block:36px 26px}
  .strip{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line);
    border:1px solid var(--line);border-radius:8px;overflow:hidden}
  .stat{background:var(--surface);padding:12px 14px}
  .stat dt{font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;
    color:var(--ink-3);margin:0 0 4px;font-weight:600}
  .stat dd{margin:0;font-family:var(--mono);font-size:19px;font-weight:600;
    font-variant-numeric:tabular-nums;letter-spacing:-.02em}
  .stat dd.win{color:var(--good)}
  section{padding-block:30px}
  h2{font-size:12px;letter-spacing:.11em;text-transform:uppercase;font-weight:600;
     color:var(--ink-3);margin:0 0 14px;font-family:var(--mono);
     display:flex;align-items:baseline;gap:10px}
  h2::after{content:"";flex:1;height:1px;background:var(--line)}
  .filters{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:14px;align-items:center}
  .flabel{font-family:var(--mono);font-size:10.5px;letter-spacing:.07em;
    text-transform:uppercase;color:var(--ink-3);margin-right:2px}
  .chip{font:inherit;font-size:12.5px;font-weight:500;cursor:pointer;background:var(--surface);
    color:var(--ink-2);border:1px solid var(--line);border-radius:999px;padding:5px 13px;
    transition:background .15s,color .15s,border-color .15s}
  .chip:hover{border-color:var(--ink-3);color:var(--ink)}
  .chip[aria-pressed="true"]{background:var(--accent);border-color:var(--accent);color:#fff}
  .chip:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  .card{background:var(--surface);border:1px solid var(--line);border-radius:10px;
    padding:16px;box-shadow:var(--shadow)}
  .card p.cap{margin:0 0 6px;font-size:13px;color:var(--ink-2)}
  .chart-host{position:relative}
  .chart-scroll{overflow-x:auto}
  svg.plot{display:block;width:100%;min-width:560px;height:auto}
  .legend{display:flex;gap:16px;flex-wrap:wrap;margin:10px 0 0;padding:0;list-style:none}
  .legend li{display:flex;align-items:center;gap:7px;font-size:12.5px;color:var(--ink-2)}
  .key{width:10px;height:10px;border-radius:50%;flex:none}
  .tip{position:absolute;pointer-events:none;z-index:5;opacity:0;transition:opacity .12s;
    background:var(--surface);border:1px solid var(--line);border-radius:7px;padding:8px 10px;
    box-shadow:var(--shadow);font-size:12.5px;max-width:250px}
  .tip .d{font-family:var(--mono);font-size:11px;color:var(--ink-3);margin-bottom:3px}
  .tip .p{font-family:var(--mono);font-weight:600;font-size:14px}
  .tip .n{color:var(--ink-2);font-size:12px;margin-top:3px;line-height:1.35}
  .bands{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:4px}
  .band{background:var(--surface);border:1px solid var(--line);border-radius:9px;padding:13px 15px}
  .band.best{background:var(--good-bg);border-color:var(--good-line)}
  .band dt{font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-3);
    font-weight:600;margin:0 0 3px}
  .band dd{margin:0;font-family:var(--mono);font-size:21px;font-weight:600;
    font-variant-numeric:tabular-nums}
  .band.best dd{color:var(--good)}
  .band .sub{font-size:11.5px;color:var(--ink-2);margin-top:3px;font-family:var(--sans)}
  .tbl-scroll{overflow-x:auto;background:var(--surface);border:1px solid var(--line);
    border-radius:10px;box-shadow:var(--shadow)}
  table{width:100%;border-collapse:collapse;font-size:13.5px;min-width:640px}
  thead th{font-family:var(--mono);font-size:10px;letter-spacing:.07em;text-transform:uppercase;
    color:var(--ink-3);font-weight:600;text-align:left;padding:9px 14px;
    border-bottom:1px solid var(--line);background:var(--surface-2)}
  thead th.r{text-align:right}
  tbody td{padding:11px 14px;border-bottom:1px solid var(--line);vertical-align:middle}
  tbody tr:last-child td{border-bottom:none}
  td.r{text-align:right}
  .price{font-family:var(--mono);font-size:14.5px;font-weight:600;font-variant-numeric:tabular-nums}
  .pill{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600;
    padding:2px 9px;border-radius:999px;border:1px solid var(--line);white-space:nowrap}
  .pill .key{width:7px;height:7px}
  .when{font-family:var(--mono);font-size:12.5px;color:var(--ink-2);white-space:nowrap}
  .mdl{font-family:var(--mono);font-size:12px;color:var(--ink-2)}
  td a{color:var(--accent);text-decoration:none;font-weight:500}
  td a:hover{text-decoration:underline}
  .name{max-width:360px}
  .notes{display:grid;gap:9px}
  .note{display:grid;grid-template-columns:auto 1fr;gap:11px;font-size:13.5px;color:var(--ink-2)}
  .note b{color:var(--ink);font-weight:600}
  .note .bul{width:5px;height:5px;border-radius:50%;background:var(--ink-3);margin-top:8px}
  footer.end{border-top:1px solid var(--line);padding-top:18px;margin-top:22px;
    font-size:12.5px;color:var(--ink-3)}
  .empty{padding:26px 16px;text-align:center;color:var(--ink-2);font-size:14px}
  @media (max-width:640px){
    .strip{grid-template-columns:repeat(2,1fr)}
    .bands{grid-template-columns:1fr}
  }
  @media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>

<header class="top">
  <div class="top-inner">
    <span class="mark"><span class="dot-live" aria-hidden="true"></span>Zephyrus Deal Watch</span>
    <span class="stamp">Updated __GENERATED__</span>
  </div>
</header>

<div class="wrap">
  <div class="hero">
    <h1>What a Zephyrus has actually cost.</h1>
    <p class="lede">Every ASUS ROG Zephyrus deal the tracker has picked up, <b>__SPAN__</b>.
      Regenerated on every scan &mdash; use it to judge whether the price in front of
      you is any good.</p>
    <p class="freshness" id="freshness">__SCANNOTE__</p>
    <dl class="strip" id="stats"></dl>
  </div>

  <section>
    <h2>Price landscape</h2>
    <div class="filters" role="group" aria-label="Filter deals">
      <span class="flabel">Model</span>
      <button class="chip" type="button" data-g="model" data-v="all" aria-pressed="true">All</button>
      <button class="chip" type="button" data-g="model" data-v="G14" aria-pressed="false">G14</button>
      <button class="chip" type="button" data-g="model" data-v="G16" aria-pressed="false">G16</button>
      <span class="flabel" style="margin-left:10px">Condition</span>
      <button class="chip" type="button" data-g="cond" data-v="all" aria-pressed="true">All</button>
      <button class="chip" type="button" data-g="cond" data-v="new" aria-pressed="false">New</button>
      <button class="chip" type="button" data-g="cond" data-v="ob" aria-pressed="false">Open-box</button>
    </div>
    <div class="card">
      <p class="cap">Each dot is one deal, placed on the day it was posted. Hover for details.</p>
      <div class="chart-host">
        <div class="chart-scroll"><svg class="plot" id="plot" viewBox="0 0 760 330" role="img"
          aria-labelledby="plotdesc"></svg></div>
        <div class="tip" id="tip"></div>
      </div>
      <p id="plotdesc" class="cap" style="margin:8px 0 0" aria-live="polite"></p>
      <ul class="legend">
        <li><span class="key" style="background:var(--s-new)"></span>New</li>
        <li><span class="key" style="background:var(--s-ob)"></span>Open-box</li>
      </ul>
    </div>
  </section>

  <section>
    <h2>Is that a good price?</h2>
    <dl class="bands" id="bands"></dl>
  </section>

  <section>
    <h2>Every deal tracked</h2>
    <div class="tbl-scroll">
      <table>
        <thead><tr>
          <th>Posted</th><th>Model</th><th>Condition</th>
          <th class="r">Price</th><th class="name">Listing</th><th></th>
        </tr></thead>
        <tbody id="tbody"></tbody>
      </table>
    </div>
  </section>

  <section>
    <h2>How to read this</h2>
    <div class="notes">
      <div class="note"><span class="bul"></span><div><b>These are real tracked figures</b>,
        not estimates &mdash; each row links to the posting it came from.</div></div>
      <div class="note"><span class="bul"></span><div><b>Prices are not like-for-like.</b>
        A $2,600 G14 is an RTX 5080 with 32GB and a 2TB SSD; a $1,400 one is an RTX 5060.
        Check the spec in the listing before treating a low number as a bargain.</div></div>
      <div class="note"><span class="bul"></span><div><b>Coverage is partial.</b> This is what
        the deal community posted, not Best Buy's full inventory &mdash; there were quiet
        stretches where nothing was posted rather than nothing being on sale.</div></div>
      <div class="note"><span class="bul"></span><div><b>This page is rebuilt by the scan.</b>
        The tracker rewrites it every time it runs, so the timestamp in the header is how
        fresh the figures are. If it stops moving, the scan has stopped.</div></div>
    </div>
    <footer class="end">Zephyrus Deal Watch &middot; generated from the live tracker database.
      Deal postings expire without notice; confirm the price at the retailer.</footer>
  </section>
</div>

<script>
(function () {
  "use strict";
  var ROWS = __PAYLOAD__;
  var SVGNS = "http://www.w3.org/2000/svg";
  var filt = { model: "all", cond: "all" };

  function el(t, a) { var n = document.createElementNS(SVGNS, t);
    for (var k in a) n.setAttribute(k, a[k]); return n; }
  function usd(v) { return "$" + Math.round(v).toLocaleString("en-US"); }
  function usd2(v) { return "$" + v.toLocaleString("en-US",
    { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function days(s) { var p = s.split("-"); return Date.UTC(+p[0], +p[1] - 1, +p[2]) / 86400000; }
  function median(a) { if (!a.length) return null; var s = a.slice().sort(function (x, y) { return x - y; });
    var m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; }

  function visible() {
    return ROWS.filter(function (r) {
      if (filt.model !== "all" && r.model !== filt.model) return false;
      if (filt.cond === "new" && r.openBox) return false;
      if (filt.cond === "ob" && !r.openBox) return false;
      return true;
    });
  }

  /* ---------------------------------------------------------- stat strip */
  function renderStats() {
    var ob = ROWS.filter(function (r) { return r.openBox; });
    var obLow = ob.length ? Math.min.apply(null, ob.map(function (r) { return r.price; })) : null;
    var nw = ROWS.filter(function (r) { return !r.openBox; }).map(function (r) { return r.price; });
    var tiles = [
      ["Deals tracked", ROWS.length, false],
      ["Open-box seen", ob.length, false],
      ["Lowest open-box", obLow === null ? "—" : usd(obLow), true],
      ["Median new price", nw.length ? usd(median(nw)) : "—", false]
    ];
    document.getElementById("stats").innerHTML = tiles.map(function (t) {
      return '<div class="stat"><dt>' + t[0] + '</dt><dd' +
        (t[2] ? ' class="win"' : '') + '>' + t[1] + "</dd></div>";
    }).join("");
  }

  /* --------------------------------------------------------------- chart */
  var W = 760, H = 330, L = 64, R = 726, T = 20, B = 272;
  var plot = document.getElementById("plot");
  var tip = document.getElementById("tip");

  function renderChart() {
    var rows = visible();
    while (plot.firstChild) plot.removeChild(plot.firstChild);
    var desc = document.getElementById("plotdesc");

    if (!rows.length) {
      desc.textContent = "No deals match these filters.";
      return;
    }

    var xs = ROWS.map(function (r) { return days(r.posted); });
    var x1 = Math.max.apply(null, xs) + 4;
    // Start at the first of the earliest month so that month gets an axis
    // label; otherwise the leftmost dot sits before the first tick, unlabelled.
    var first = ROWS.map(function (r) { return r.posted; }).sort()[0];
    var x0 = days(first.slice(0, 7) + "-01");
    var prices = ROWS.map(function (r) { return r.price; });
    var lo = Math.floor(Math.min.apply(null, prices) / 400) * 400;
    var hi = Math.ceil(Math.max.apply(null, prices) / 400) * 400;

    function px(d) { return L + (R - L) * (d - x0) / (x1 - x0 || 1); }
    function py(v) { return B - (v - lo) / (hi - lo || 1) * (B - T); }

    for (var v = lo; v <= hi; v += 400) {
      plot.appendChild(el("line", { x1: L, x2: R, y1: py(v), y2: py(v),
        stroke: "var(--line)", "stroke-width": 1 }));
      var yl = el("text", { x: L - 10, y: py(v) + 4, "text-anchor": "end",
        fill: "var(--ink-3)", "font-size": 11, "font-family": "var(--mono)" });
      yl.textContent = "$" + (v / 1000).toFixed(1) + "k";
      plot.appendChild(yl);
    }

    // Month ticks across the observed span.
    var seen = {};
    ROWS.forEach(function (r) {
      var key = r.posted.slice(0, 7);
      if (seen[key]) return;
      seen[key] = 1;
      var d = days(key + "-01");
      if (d < x0 || d > x1) return;
      var t = el("text", { x: px(d), y: B + 20, "text-anchor": "middle",
        fill: "var(--ink-3)", "font-size": 11, "font-family": "var(--mono)" });
      t.textContent = new Date(d * 86400000).toLocaleDateString("en-US",
        { month: "short", timeZone: "UTC" });
      plot.appendChild(t);
    });

    plot.appendChild(el("line", { x1: L, x2: R, y1: B, y2: B,
      stroke: "var(--line)", "stroke-width": 1 }));

    rows.forEach(function (r) {
      var c = el("circle", {
        cx: px(days(r.posted)), cy: py(r.price), r: 5.5,
        fill: r.openBox ? "var(--s-ob)" : "var(--s-new)",
        stroke: "var(--surface)", "stroke-width": 2, "data-id": r.id
      });
      c.style.cursor = "pointer";
      plot.appendChild(c);
    });

    var obv = rows.filter(function (r) { return r.openBox; }).length;
    desc.textContent = rows.length + " deal" + (rows.length === 1 ? "" : "s") +
      " shown, " + obv + " open-box, from " + usd(Math.min.apply(null,
      rows.map(function (r) { return r.price; }))) + " to " +
      usd(Math.max.apply(null, rows.map(function (r) { return r.price; }))) + ".";
  }

  function showTip(evt) {
    var t = evt.target;
    if (!t || t.tagName !== "circle") { hideTip(); return; }
    var r = ROWS.filter(function (x) { return x.id === t.getAttribute("data-id"); })[0];
    if (!r) return;
    tip.innerHTML = '<div class="d">' + r.posted + " &middot; " + r.model + " &middot; " +
      (r.openBox ? "Open-box" : "New") + '</div><div class="p">' + usd2(r.price) +
      '</div><div class="n">' + r.name.replace(/[<>]/g, "") + "</div>";
    tip.style.opacity = 1;
    var host = plot.parentNode.parentNode.getBoundingClientRect();
    var box = t.getBoundingClientRect();
    var cx = box.left - host.left + box.width / 2;
    var w = tip.offsetWidth;
    tip.style.left = Math.max(0, Math.min(cx - w / 2, host.width - w)) + "px";
    tip.style.top = Math.max(0, box.top - host.top - tip.offsetHeight - 10) + "px";
  }
  function hideTip() { tip.style.opacity = 0; }
  plot.addEventListener("mouseover", showTip);
  plot.addEventListener("mouseout", hideTip);
  plot.addEventListener("click", showTip);

  /* --------------------------------------------------------------- bands */
  function renderBands() {
    var rows = visible();
    var host = document.getElementById("bands");
    if (!rows.length) {
      host.innerHTML = '<div class="band"><dd class="sub">No deals match these filters.</dd></div>';
      return;
    }
    var p = rows.map(function (r) { return r.price; });
    var low = Math.min.apply(null, p), high = Math.max.apply(null, p), mid = median(p);
    var lowRow = rows.filter(function (r) { return r.price === low; })[0];
    host.innerHTML =
      '<div class="band best"><dt>Best seen</dt><dd>' + usd(low) +
        '</dd><div class="sub">' + lowRow.posted + " &middot; " +
        (lowRow.openBox ? "open-box" : "new") + " " + lowRow.model + "</div></div>" +
      '<div class="band"><dt>Typical</dt><dd>' + usd(mid) +
        '</dd><div class="sub">median of ' + rows.length + " tracked deal" +
        (rows.length === 1 ? "" : "s") + "</div></div>" +
      '<div class="band"><dt>Highest seen</dt><dd>' + usd(high) +
        '</dd><div class="sub">anything near this is not a deal</div></div>';
  }

  /* --------------------------------------------------------------- table */
  function renderTable() {
    var rows = visible().slice().sort(function (a, b) {
      return a.posted < b.posted ? 1 : a.posted > b.posted ? -1 : 0; });
    var body = document.getElementById("tbody");
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="6" class="empty">No deals match these filters.</td></tr>';
      return;
    }
    body.innerHTML = rows.map(function (r) {
      var colour = r.openBox ? "var(--s-ob)" : "var(--s-new)";
      return "<tr>" +
        '<td><span class="when">' + r.posted + "</span></td>" +
        '<td><span class="mdl">' + r.model + "</span></td>" +
        '<td><span class="pill"><span class="key" style="background:' + colour + '"></span>' +
          (r.openBox ? "Open-box" : "New") + "</span></td>" +
        '<td class="r"><span class="price">' + usd2(r.price) + "</span></td>" +
        '<td class="name">' + r.name.replace(/[<>]/g, "") + "</td>" +
        '<td><a href="' + r.url + '" target="_blank" rel="noopener">View &rarr;</a></td>' +
        "</tr>";
    }).join("");
  }

  function renderAll() { renderChart(); renderBands(); renderTable(); }

  Array.prototype.forEach.call(document.querySelectorAll(".chip"), function (c) {
    c.addEventListener("click", function () {
      var g = c.dataset.g;
      filt[g] = c.dataset.v;
      Array.prototype.forEach.call(document.querySelectorAll('.chip[data-g="' + g + '"]'),
        function (o) { o.setAttribute("aria-pressed", String(o === c)); });
      hideTip();
      renderAll();
    });
  });

  // Make staleness legible without mental arithmetic on a UTC timestamp.
  (function () {
    var node = document.getElementById("freshness");
    if (!node) return;
    var m = node.textContent.match(/last scan (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})/);
    if (!m) return;
    var then = Date.parse(m[1] + "T" + m[2] + ":00Z");
    if (isNaN(then)) return;
    var mins = Math.round((Date.now() - then) / 60000);
    var ago = mins < 2 ? "just now"
            : mins < 60 ? mins + " minutes ago"
            : mins < 1440 ? Math.round(mins / 60) + " hours ago"
            : Math.round(mins / 1440) + " days ago";
    node.innerHTML = node.textContent + ' <span class="age">(' + ago + ")</span>";
  })();

  renderStats();
  renderAll();
})();
</script>
"""


def collect(db) -> list[dict]:
    """Every tracked offer, flattened for the page."""
    rows: list[dict] = []
    for product in db.get_products():
        for state in db.conn.execute(
            "SELECT * FROM offer_state WHERE sku=?", (product["sku"],)
        ):
            if state["price"] is None:
                continue
            # The feed backend knows when a listing was posted; the Best Buy
            # backend does not, so fall back to when we first saw it.
            posted = (product["posted_at"] or product["first_seen"] or "")[:10]
            if not posted:
                continue
            rows.append({
                "id": product["sku"],
                "name": (product["name"] or "").replace("[Best Buy]", "").strip(),
                "model": product["model_number"] or "Other",
                "url": product["url"] or "",
                "posted": posted,
                "label": condition_label(state["condition"]),
                "openBox": is_open_box(state["condition"])
                           or state["condition"] in ("refurbished", "pre-owned"),
                "price": state["price"],
                "list": state["regular_price"],
                "available": bool(state["available"]),
            })
    rows.sort(key=lambda r: r["posted"])
    return rows


def render(db, out_path: str | Path, config=None) -> Path:
    """Write the dashboard. Returns the path written."""
    rows = collect(db)
    generated = datetime.datetime.now().strftime("%-d %b %Y, %H:%M")
    if rows:
        span = f'{rows[0]["posted"]} to {rows[-1]["posted"]}'
    else:
        span = "no deals recorded yet"

    last = db.last_scan()
    scan_note = ""
    if last:
        scan_note = (f'{last["products"]} tracked &middot; '
                     f'last scan {last["ts"][:16].replace("T", " ")} UTC')

    html = (TEMPLATE
            .replace("__PAYLOAD__", json.dumps(rows, separators=(",", ":")))
            .replace("__GENERATED__", generated)
            .replace("__SCANNOTE__", scan_note)
            .replace("__SPAN__", span))

    path = Path(out_path)
    if path.parent != Path(""):
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path
