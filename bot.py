"""Paper-trading bot: a local AI model (DeepSeek R1 8B, run with Ollama) picks UP/DOWN on Kalshi 15-minute BTC markets.

Fake money only. Reads public Kalshi + Coinbase data, never places orders.
Run:  python bot.py        (needs Ollama running: ollama pull deepseek-r1:8b; no API key)
Test: python bot.py test   (math check + live data fetch, no AI call)
"""
import csv, json, math, os, re, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen, Request

# ---- settings ----
SERIES = "KXBTC15M"
CONTRACTS = 30           # contracts per trade (~$15 at 50c: same 15% bet size as the video)
START_BALANCE = 100.0    # fake dollars (matches the user's real $100 budget)
MODEL = os.environ.get("AI_MODEL", "deepseek-r1:8b")
AI_URL = os.environ.get("AI_URL", "http://localhost:11434/v1/chat/completions")  # Ollama, no key needed
AI_KEY = os.environ.get("AI_KEY", "")                                               # only for hosted APIs

HERE = Path(__file__).parent
STATE = HERE / "state.json"
LOG = HERE / "trades.csv"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", 0))  # 0 = run forever
PUSH = os.environ.get("PUSH") == "1"                 # GitHub Actions: commit scoreboard after each trade


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
             "o": round(r[3]), "h": round(r[2]), "l": round(r[1]), "c": round(r[4])} for r in reversed(rows)]


def chat(prompt, max_tokens=2500):
    body = json.dumps({"model": MODEL, "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "paper-bot"}
    if AI_KEY:
        headers["Authorization"] = f"Bearer {AI_KEY}"
    # ponytail: 13 min timeout fits one 15-min market on a CPU runner; use a GPU or hosted API if it times out
    with urlopen(Request(AI_URL, data=body, headers=headers), timeout=780) as r:
        data = json.load(r)
    u = data.get("usage") or {}
    print(f"  AI tokens: {u.get('prompt_tokens')} in, {u.get('completion_tokens')} out")
    return data["choices"][0]["message"]["content"] or ""


def parse_decision(text):
    # model may think out loud first; take the last {...} that has a valid direction
    for blob in reversed(re.findall(r"\{[^{}]*\}", text)):
        try:
            d = json.loads(blob)
        except ValueError:
            continue
        if d.get("direction") in ("UP", "DOWN", "SKIP"):
            return {"direction": d["direction"], "reason": str(d.get("reason", ""))}
    return {"direction": "SKIP", "reason": "no valid answer from model"}


def ask_ai(m, mins_left):
    prompt = f"""Kalshi market: "{m['title']}" ({m['ticker']}).
Resolves YES (UP) if BTC's 60-second average price at close is >= the 60-second average at open.
Open reference: {m.get('yes_sub_title')}. Minutes left: {mins_left:.1f}.
UP (yes) ask: ${m['yes_ask_dollars']}  |  DOWN (no) ask: ${m['no_ask_dollars']}  (payout $1 per contract)
Taker fee ~0.07*P*(1-P) per contract.

BTC-USD 1-minute candles (last 20):
{json.dumps(candles(60, 20), separators=(",", ":"))}
BTC-USD 15-minute candles (last 12):
{json.dumps(candles(900, 12), separators=(",", ":"))}

Pick UP, DOWN, or SKIP. Only pick a side if you think its win chance beats its ask price plus fee.
End your reply with one line of JSON only: {{"direction": "UP" | "DOWN" | "SKIP", "reason": "<one sentence>"}}"""
    return parse_decision(chat(prompt))


def load():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"balance": START_BALANCE, "wins": 0, "trades": 0, "pending": [], "seen": []}


def save(s):
    STATE.write_text(json.dumps(s, indent=1))


def log_row(row):
    new = not LOG.exists()
    with LOG.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


def publish(s):
    # README.md scoreboard; with PUSH=1 also commit + push it so GitHub shows it
    rows = list(csv.DictReader(LOG.open())) if LOG.exists() else []
    lines = [f"| {r['time']} | {r['side']} | ${float(r['price']):.3f} | {'WIN' if r['won'] == 'True' else 'LOSS'} "
             f"| {float(r['pnl']):+.2f} | ${float(r['balance']):.2f} |" for r in reversed(rows[-20:])]
    rate = f"{s['wins'] / s['trades']:.0%}" if s["trades"] else "n/a"
    (HERE / "README.md").write_text(f"""# Kalshi paper bot

AI model `{MODEL}` trades Kalshi 15-minute BTC up/down markets with **fake money**. It never places real orders.

## Scoreboard

| Balance | Profit | Trades | Win rate | Open bets |
|---|---|---|---|---|
| ${s['balance']:.2f} | {s['balance'] - START_BALANCE:+.2f} | {s['trades']} | {rate} | {len(s['pending'])} |

Start ${START_BALANCE:.0f}, no stop limits (balance can go negative). {CONTRACTS} contracts per trade, Kalshi taker fee included.
Updated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC.

## Last 20 trades

| Time | Side | Price | Result | P&L | Balance |
|---|---|---|---|---|---|
""" + "\n".join(lines) + "\n")
    if PUSH:
        cmd = "git add -A && (git commit -qm 'bot: update scoreboard' || true) && git pull -q --rebase && git push -q"
        subprocess.run(cmd, shell=True, cwd=HERE)


def settle(s):
    done = 0
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
        done += 1
        log_row({**t, "result": m["result"], "won": won, "pnl": p, "balance": s["balance"]})
        print(f"SETTLED {t['ticker']} {t['side']} {'WIN' if won else 'LOSS'} {p:+.2f} | "
              f"balance ${s['balance']:.2f} | {s['wins']}/{s['trades']} wins")
    return done


def main():
    chat("Reply with OK.", max_tokens=200)  # crash now if the AI isn't reachable instead of looping for hours
    s = load()
    print(f"PAPER MODE | {MODEL} | balance ${s['balance']:.2f} | no limits | {CONTRACTS} contracts/trade")
    end = time.time() + RUN_SECONDS if RUN_SECONDS else float("inf")
    publish(s)  # refresh the scoreboard page at every start
    while time.time() < end:
        try:
            if settle(s):
                save(s)
                publish(s)
            m = open_market()
            if m and m["ticker"] not in s["seen"]:
                s["seen"] = (s["seen"] + [m["ticker"]])[-50:]
                close = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
                mins_left = (close - datetime.now(timezone.utc)).total_seconds() / 60
                if mins_left >= 12:  # skip markets joined mid-session
                    t0 = time.time()
                    d = ask_ai(m, mins_left)
                    print(f"{m['ticker']} AI ({time.time() - t0:.0f}s): {d['direction']} - {d['reason'][:200]}")
                    if d["direction"] != "SKIP":
                        m = get(f"{KALSHI}/markets/{m['ticker']}")["market"]  # fresh quote
                        price = float(m["yes_ask_dollars" if d["direction"] == "UP" else "no_ask_dollars"])
                        still_open = (close - datetime.now(timezone.utc)).total_seconds() > 60  # slow model guard
                        if 0 < price < 1 and still_open:  # no balance limit: fake balance may go negative
                            s["pending"].append({"time": datetime.now().isoformat(timespec="seconds"),
                                                 "ticker": m["ticker"], "side": d["direction"],
                                                 "price": price, "contracts": CONTRACTS})
                            print(f"  PAPER BUY {CONTRACTS} {d['direction']} @ ${price:.3f}")
                save(s)
        except HTTPError as e:
            if e.code in (401, 403):
                raise  # bad key: stop the run so it shows red on GitHub
            print("error, skipping:", e)
        except Exception as e:  # network blips: log and keep running
            print("error, skipping:", e)
        time.sleep(20)
    save(s)
    publish(s)


def selftest():
    assert taker_fee(300, 0.5) == 5.25
    assert pnl(300, 0.5, True) == 150 - 5.25
    assert pnl(300, 0.5, False) == -150 - 5.25
    assert parse_decision('thinking {x} ... {"direction": "DOWN", "reason": "r"}')["direction"] == "DOWN"
    assert parse_decision("no json here")["direction"] == "SKIP"
    m = open_market()
    print("market ok:", m["ticker"], "UP", m["yes_ask_dollars"], "DOWN", m["no_ask_dollars"])
    print("candles ok:", candles(60, 2))
    print("selftest passed")


if __name__ == "__main__":
    selftest() if sys.argv[1:] == ["test"] else main()
