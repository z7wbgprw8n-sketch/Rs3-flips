"""RS3 flip finder using the RuneScape Wiki real-time prices API
(instant-buy / instant-sell prices from actual GE trades).
Top flips (persistence + trend + risk filtered) and a staples watchlist.
Writes docs/index.html, docs/flips.json, docs/history.json; optional Discord alerts."""
import json, os, re, time, urllib.request
from datetime import datetime, timezone

BASES = ["https://prices.runescape.wiki/api/v2/rs",
         "https://prices.runescape.wiki/api/v1/rs"]
DUMP = "https://chisel.weirdgloop.org/gazproj/gazbot/rs_dump.json"  # name fallback only
UA = {"User-Agent": "rs3-flip-finder - personal GitHub project"}

# ---- tweak these ----
MIN_BUY, MAX_BUY = 50, 500_000_000
MIN_TRADES_HR = 10        # min trades in the last hour on BOTH the buy and sell side
MIN_MARGIN_PCT = 1.5      # min profit % after tax (top flips table)
MIN_PROFIT = 20           # min gp profit per item after tax (top flips table)
MAX_AGE = 1800            # ignore prices last traded more than 30 min ago (seconds)
TOP_N = 25
DISCORD_MIN_MARGIN = 3.0  # only alert on new flips at/above this %

# Persistence: an item must pass the filters in at least PERSIST_MIN of the last PERSIST_RUNS runs.
PERSIST_RUNS, PERSIST_MIN = 6, 4
# Trend: skip items falling >3% in 6h or >6% in 24h, or spiking >10% above their 24h price.
MAX_DROP_6H, MAX_DROP_24H, MAX_SPIKE_24H = 0.03, 0.06, 0.10
# Position sizing is done on the page: type your cash stack in the box and it works out quantities.
DEFAULT_POSITION_PCT = 15  # starting value for "max % of stack per item" (changeable on the page)

# Watchlist: item-name patterns (regex, case-insensitive) always shown, even when margin is poor.
WATCH = [r"dragon ?hide", r"\blogs\b", r"\bbones\b", r"\bore\b", r"\bbar\b",
         r"^grimy ", r"^raw ", r" rune$", r"\bseed\b"]
AUTO_STAPLES = 15         # plus the most-traded items that currently have a profit
WATCH_MAX = 40


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def api(path):
    err = None
    for base in BASES:
        try:
            return get(base + path)
        except Exception as e:
            err = e
    raise err


def tax(sell):
    return min(int(sell * 0.02), 5_000_000)


def mid(h):
    if not h:
        return None
    a, b = h.get("avgHighPrice"), h.get("avgLowPrice")
    return (a + b) / 2 if a and b else None


def main():
    now = time.time()
    latest = api("/latest")["data"]
    hour = api("/1h")["data"]

    def past(hours):
        ts = int(now // 3600 * 3600) - hours * 3600
        try:
            return api(f"/1h?timestamp={ts}")["data"]
        except Exception as e:
            print(f"{hours}h history unavailable:", e)
            return {}
    past6, past24 = past(6), past(24)

    names, limits = {}, {}
    try:
        for m in api("/mapping"):
            names[str(m["id"])] = m.get("name")
            limits[str(m["id"])] = m.get("limit")
    except Exception as e:
        print("mapping unavailable:", e)
        try:
            for nm, d in get(DUMP).items():
                if isinstance(d, dict) and "id" in d:
                    names[str(d["id"])] = nm
        except Exception as e2:
            print("name fallback failed:", e2)

    rows = []
    for sid, l in latest.items():
        hi, lo = l.get("high"), l.get("low")
        ht, lt = l.get("highTime"), l.get("lowTime")
        if not (hi and lo and ht and lt) or hi <= lo:
            continue
        if now - ht > MAX_AGE or now - lt > MAX_AGE:
            continue
        if not (MIN_BUY <= lo <= MAX_BUY):
            continue
        h = hour.get(sid)
        if not h:
            continue
        ah, al = h.get("avgHighPrice"), h.get("avgLowPrice")
        hv, lv = h.get("highPriceVolume") or 0, h.get("lowPriceVolume") or 0
        if not (ah and al) or min(hv, lv) < MIN_TRADES_HR:
            continue
        if hi > ah * 1.05 or lo < al * 0.95:  # one-off outlier trades
            continue

        profit = min(hi - lo - tax(hi), int(ah - al - tax(ah)))  # cautious figure
        trades = min(hv, lv)
        limit = limits.get(sid)
        units = min(limit, trades * 4) if limit else trades * 4

        m_now, m6, m24 = (ah + al) / 2, mid(past6.get(sid)), mid(past24.get(sid))
        t6 = (m_now - m6) / m6 if m6 else None
        t24 = (m_now - m24) / m24 if m24 else None
        vals = [x for x in (m_now, m6, m24) if x]
        vol = (max(vals) - min(vals)) / (sum(vals) / len(vals)) if len(vals) >= 2 else None
        trend_ok = True
        if t6 is not None and t6 < -MAX_DROP_6H:
            trend_ok = False
        if t24 is not None and (t24 < -MAX_DROP_24H or t24 > MAX_SPIKE_24H):
            trend_ok = False
        risk = "?" if vol is None else "low" if vol < 0.02 else "med" if vol < 0.05 else "high"
        p4h = profit * units
        score = p4h / (1 + 10 * (vol if vol is not None else 0.05))

        row = dict(name=names.get(sid) or f"Item {sid}", buy=lo, sell=hi, profit=profit,
                   margin=round(profit / lo * 100, 1), limit=limit, trades_hr=trades,
                   profit_4h=p4h, risk=risk, trend24=None if t24 is None else round(t24 * 100, 1),
                   score=score, trend_ok=trend_ok, seen="-")
        rows.append(row)

    cand = [r for r in rows if r["profit"] >= MIN_PROFIT and r["margin"] >= MIN_MARGIN_PCT
            and r["trend_ok"]]

    # persistence across runs
    os.makedirs("docs", exist_ok=True)
    try:
        with open("docs/history.json") as f:
            hist = json.load(f)["runs"]
    except Exception:
        hist = []
    runs = (hist + [sorted(r["name"] for r in cand)])[-PERSIST_RUNS:]
    with open("docs/history.json", "w") as f:
        json.dump({"runs": runs}, f)
    warm = len(runs) < PERSIST_MIN
    for r in cand:
        c = sum(r["name"] in run for run in runs)
        r["seen"] = f"{c}/{len(runs)}"
        r["_c"] = c
    flips = sorted((r for r in cand if warm or r["_c"] >= PERSIST_MIN),
                   key=lambda r: r["score"], reverse=True)[:TOP_N]

    pats = [re.compile(p, re.I) for p in WATCH]
    keyword = [r for r in rows if any(p.search(r["name"]) for p in pats)]
    auto = sorted((r for r in rows if r["profit"] > 0),
                  key=lambda r: r["trades_hr"], reverse=True)[:AUTO_STAPLES]
    seen, watch = set(), []
    for r in keyword + auto:
        if r["name"] not in seen:
            seen.add(r["name"])
            watch.append(r)
    watch = sorted(watch, key=lambda r: r["trades_hr"], reverse=True)[:WATCH_MAX]

    try:
        with open("docs/flips.json") as f:
            old = {x["name"] for x in json.load(f)["flips"]}
    except Exception:
        old = set()

    clean = lambda items: [{k: v for k, v in r.items() if not k.startswith("_")} for r in items]
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with open("docs/flips.json", "w") as f:
        json.dump({"updated": stamp, "flips": clean(flips), "watch": clean(watch)}, f, indent=1)

    n = lambda x: "?" if x is None else f"{int(x):,}"

    def table(items):
        extra_h = "<th>Qty</th><th>Exp. profit</th>"
        body = ""
        for f in items:
            t = "?" if f["trend24"] is None else f"{f['trend24']:+}%"
            extra = '<td class="qty">-</td><td class="exp">-</td>'
            body += (f"<tr data-buy='{f['buy']}' data-profit='{f['profit']}' data-limit='{f['limit'] or 0}' data-trades='{f['trades_hr']}'><td>{f['name']}</td><td>{n(f['buy'])}</td><td>{n(f['sell'])}</td>"
                     f"<td>{n(f['profit'])}</td><td>{f['margin']}%</td><td>{f['risk']}</td>"
                     f"<td>{t}</td><td>{f['seen']}</td><td>{n(f['limit'])}</td>"
                     f"<td>{n(f['profit_4h'])}</td><td>{n(f['trades_hr'])}</td>{extra}</tr>")
        return ('<div class="w"><table><tr><th>Item</th><th>Buy</th><th>Sell</th><th>Profit</th>'
                '<th>ROI</th><th>Risk</th><th>24h</th><th>Seen</th><th>Limit</th>'
                f'<th>4h profit</th><th>Trades/hr</th>{extra_h}</tr>{body}</table></div>')

    warm_note = (" History is still building, so persistence filtering starts after a few runs."
                 if warm else "")
    sizing = " Enter your cash stack to see Qty (how many to buy) and Exp. profit. Qty is capped by your per-item share, the buy limit, and the recent trade pace."
    CONTROLS = f"""<p><label>Your cash stack: <input id="stack" inputmode="text" placeholder="e.g. 20m, 500k, 1.5b" size="14"></label>
<label>Max % per item: <input id="pct" type="number" min="1" max="100" value="{DEFAULT_POSITION_PCT}" style="width:4em"></label></p>"""
    SCRIPT = r"""<script>
(function(){
var stack=document.getElementById("stack"),pct=document.getElementById("pct");
try{var sv=localStorage.getItem("rs3stack");if(sv)stack.value=sv;var pv=localStorage.getItem("rs3pct");if(pv)pct.value=pv;}catch(e){}
function parseGp(s){s=(s||"").toLowerCase().replace(/[, ]/g,"");var m=s.match(/^([0-9]*\.?[0-9]+)([kmb]?)$/);if(!m)return 0;var u={"":1,k:1e3,m:1e6,b:1e9};return parseFloat(m[1])*u[m[2]];}
function fmt(x){return Math.round(x).toLocaleString();}
function calc(){
var gp=parseGp(stack.value),p=parseFloat(pct.value)||0;
try{localStorage.setItem("rs3stack",stack.value);localStorage.setItem("rs3pct",pct.value);}catch(e){}
document.querySelectorAll("tr[data-buy]").forEach(function(tr){
var buy=+tr.dataset.buy,profit=+tr.dataset.profit,limit=+tr.dataset.limit,trades=+tr.dataset.trades;
var q=gp>0&&buy>0?Math.floor(gp*p/100/buy):0;
if(limit>0)q=Math.min(q,limit);
q=Math.max(Math.min(q,trades*4),0);
tr.querySelector(".qty").textContent=gp>0?fmt(q):"-";
tr.querySelector(".exp").textContent=gp>0?fmt(q*profit):"-";
});
}
stack.addEventListener("input",calc);pct.addEventListener("input",calc);calc();
})();
</script>"""
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RS3 Flips</title>
<style>:root{{color-scheme:light dark}}body{{font-family:system-ui,sans-serif;margin:12px}}
.w{{overflow-x:auto}}table{{border-collapse:collapse;width:100%}}
th,td{{padding:6px 8px;border-bottom:1px solid #8884;text-align:right;white-space:nowrap}}
th:first-child,td:first-child{{text-align:left}}</style></head><body>
<h2>Top flips</h2>
{CONTROLS}
<p>Updated {stamp}. Place a buy offer at the Buy price and a sell offer at the Sell price. Profit is per item after 2% GE tax. Risk is how much the price has moved over 24h. Seen is how many recent runs the flip held up. Items with a falling or spiking price are excluded. 4h profit assumes you fill the buy limit (or the recent trade pace if lower).{sizing}{warm_note}</p>
{table(flips)}
<h2>Staples watchlist</h2>
<p>High-volume everyday items (hides, logs, bones, ores, bars, herbs, raw food, runes, seeds). Shown even when the margin is thin, negative, or the trend is poor, so skip any with a negative Profit or a falling 24h.</p>
{table(watch)}
<p>Prices come from real recent GE trades and can move fast. Check the in-game price before you offer. No flip is guaranteed.</p>{SCRIPT}</body></html>"""
    with open("docs/index.html", "w") as f:
        f.write(html)

    hook = os.environ.get("DISCORD_WEBHOOK")
    new = [f for f in flips if f["name"] not in old and f["margin"] >= DISCORD_MIN_MARGIN][:5]
    if hook and new:
        text = "**New RS3 flips**\n" + "\n".join(
            f"{f['name']}: buy {n(f['buy'])} / sell {n(f['sell'])} "
            f"(+{n(f['profit'])}, {f['margin']}%, risk {f['risk']})" for f in new)
        req = urllib.request.Request(
            hook, data=json.dumps({"content": text}).encode(),
            headers={**UA, "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=30)
        except Exception as e:
            print("discord error:", e)
    print(f"{len(flips)} flips, {len(watch)} watchlist items written")


if __name__ == "__main__":
    main()
