"""Paper-trading bot: Claude Opus 5.5 picks UP/DOWN on Kalshi 15-minute BTC markets.

Fake money only. Reads public Kalshi + Coinbase data, never places orders.
Run:  python bot.py        (needs ANTHROPIC_API_KEY)
Test: python bot.py test   (math check + live data fetch, no Claude call)
"""
import csv, json, math, sys, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request

# ---- settings ----
SERIES = "KXBTC15M"
CONTRACTS = 300          # contracts per trade (~$150 at 50c, same as video)
START_BALANCE = 1000.0   # fake dollars
TARGET = 1200.0          # stop when balance reaches this
MODEL = "claude-opus-5-5"
IN_PRICE, OUT_PRICE = 4 / 1e6, 20 / 1e6   # Opus 5.5 $ per token

HERE = Path(__file__).parent
STATE = HERE / "state.json"
LOG = HERE / "trades.csv"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"


def get(url):
    with urlopen(Request(url, headers={"User-Agent": "paper-bot"}), timeout=15) as r:
        return json.load(r)


def taker_fee(contracts, price):
    # Kalshi taker fee: 0.07 * C * P * (1-P), rounded up to the cent
    return math.ceil(0.07 * contracts * price * (1 - price) * 100 - 1e-9) / 100


def pnl(contracts, price, won):
    fee = taker_fee(contracts, price)
    return round((contracts * (1 - price) if won else -contracts * price) - fee, 2)


def open_market():
    ms = get(f"{KALSHI}/markets?series_ticker={SERIES}&status=open&limit=5")["markets"]
    return min(ms, key=lambda m: m["close_time"]) if ms else None


def candles(granularity, n):
    # Coinbase rows: [time, low, high, open, close, volume], newest first
    rows = get(f"https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity={granularity}")[:n]
    return [{"t": datetime.fromtimestamp(r[0], timezone.utc).strftime("%H:%M"),
             "o": r[3], "h": r[2], "l": r[1], "c": r[4]} for r in reversed(rows)]


def ask_claude(client, m, mins_left):
    prompt = f"""Kalshi market: "{m['title']}" ({m['ticker']}).
Resolves YES (UP) if BTC's 60-second average price at close is >= the 60-second average at open.
Open reference: {m.get('yes_sub_title')}. Minutes left: {mins_left:.1f}.
UP (yes) ask: ${m['yes_ask_dollars']}  |  DOWN (no) ask: ${m['no_ask_dollars']}  (payout $1 per contract)
Taker fee ~0.07*P*(1-P) per contract.

BTC-USD 1-minute candles (last 30):
{json.dumps(candles(60, 30))}
BTC-USD 15-minute candles (last 24):
{json.dumps(candles(900, 24))}

Pick UP, DOWN, or SKIP. Only pick a side if you think its win chance beats its ask price plus fee."""
    resp = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": {
            "type": "object",
            "properties": {"direction": {"type": "string", "enum": ["UP", "DOWN", "SKIP"]},
                           "reason": {"type": "string"}},
            "required": ["direction", "reason"], "additionalProperties": False}}},
        messages=[{"role": "user", "content": prompt}],
    )
    cost = resp.usage.input_tokens * IN_PRICE + resp.usage.output_tokens * OUT_PRICE
    if resp.stop_reason == "refusal":
        return {"direction": "SKIP", "reason": "model refused"}, cost
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text), cost


def load():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"balance": START_BALANCE, "ai_cost": 0.0, "wins": 0, "trades": 0, "pending": [], "seen": []}


def save(s):
    STATE.write_text(json.dumps(s, indent=1))


def log_row(row):
    new = not LOG.exists()
    with LOG.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


def settle(s):
    for t in list(s["pending"]):
        m = get(f"{KALSHI}/markets/{t['ticker']}")["market"]
        if m.get("result") not in ("yes", "no"):
            continue
        won = (m["result"] == "yes") == (t["side"] == "UP")
        p = pnl(t["contracts"], t["price"], won)
        s["balance"] = round(s["balance"] + p, 2)
        s["trades"] += 1
        s["wins"] += won
        s["pending"].remove(t)
        log_row({**t, "result": m["result"], "won": won, "pnl": p, "balance": s["balance"]})
        print(f"SETTLED {t['ticker']} {t['side']} {'WIN' if won else 'LOSS'} {p:+.2f} | "
              f"balance ${s['balance']:.2f} | {s['wins']}/{s['trades']} wins | AI cost ${s['ai_cost']:.2f}")


def main():
    import anthropic
    client = anthropic.Anthropic()
    s = load()
    print(f"PAPER MODE | balance ${s['balance']:.2f} | target ${TARGET:.0f} | {CONTRACTS} contracts/trade")
    while True:
        try:
            settle(s)
            save(s)
            if s["balance"] >= TARGET or s["balance"] <= 0:
                if not s["pending"]:
                    print(f"DONE | balance ${s['balance']:.2f} | AI cost ${s['ai_cost']:.2f}")
                    return
            else:
                m = open_market()
                if m and m["ticker"] not in s["seen"]:
                    s["seen"] = (s["seen"] + [m["ticker"]])[-50:]
                    close = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
                    mins_left = (close - datetime.now(timezone.utc)).total_seconds() / 60
                    if mins_left >= 12:  # skip markets joined mid-session
                        d, cost = ask_claude(client, m, mins_left)
                        s["ai_cost"] = round(s["ai_cost"] + cost, 4)
                        print(f"{m['ticker']} AI: {d['direction']} - {d['reason'][:200]}")
                        if d["direction"] != "SKIP":
                            m = get(f"{KALSHI}/markets/{m['ticker']}")["market"]  # fresh quote
                            price = float(m["yes_ask_dollars" if d["direction"] == "UP" else "no_ask_dollars"])
                            if 0 < price < 1 and CONTRACTS * price <= s["balance"]:
                                s["pending"].append({"time": datetime.now().isoformat(timespec="seconds"),
                                                     "ticker": m["ticker"], "side": d["direction"],
                                                     "price": price, "contracts": CONTRACTS})
                                print(f"  PAPER BUY {CONTRACTS} {d['direction']} @ ${price:.3f}")
                    save(s)
        except Exception as e:  # network or API blips: log and keep running
            print("error, skipping:", e)
        time.sleep(20)


def selftest():
    assert taker_fee(300, 0.5) == 5.25
    assert pnl(300, 0.5, True) == 150 - 5.25
    assert pnl(300, 0.5, False) == -150 - 5.25
    m = open_market()
    print("market ok:", m["ticker"], "UP", m["yes_ask_dollars"], "DOWN", m["no_ask_dollars"])
    print("candles ok:", candles(60, 2))
    print("selftest passed")


if __name__ == "__main__":
    selftest() if sys.argv[1:] == ["test"] else main()
