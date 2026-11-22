"""ORB options bot (paper). One trade per ETF per day, same-day at-the-money options.

Phases (run by GitHub Actions on a schedule; times ET):
  morning    start early, wait for 9:35, trade, manage until 14:30
  afternoon  pick up open positions, manage until 15:45, then close everything
  close      backup: close every open option position now
  dry        no orders: check account, data, contract lookup, quotes and sizing, then report

Rules (playbook, paper only until the owner approves live trading):
- First 5-minute candle (9:30-9:35 ET, IEX feed) closed up -> buy a call; down -> buy a put; flat -> no trade.
- Strike nearest the price, expiry today (next listed expiry if none today).
- Premium paid = 1% of equity per trade (playbook allows up to 2%; 1% chosen for paper start).
- Exit when the ETF hits the stop (far end of the first candle) or 10x the stop distance, else at 15:45 ET.
- Kill switch: equity 15% below its high -> no new trades, alert.
Keys come only from environment variables. Never printed.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SYMS = ["SPY", "QQQ", "IWM"]
RISK = float(os.environ.get("RISK_PER_TRADE", "0.01"))
# Optional virtual account: size trades as if the account held VIRTUAL_START dollars on VIRTUAL_SINCE,
# plus the realized P&L of this bot's own trades since then. The real paper balance is ignored for sizing.
VIRTUAL_START = float(os.environ.get("VIRTUAL_START", "0") or 0)
VIRTUAL_SINCE = os.environ.get("VIRTUAL_SINCE", "")
MODE = os.environ.get("MODE", "PAPER").upper()
TRADE = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"
KEY, SECRET = os.environ.get("ALPACA_KEY_ID", ""), os.environ.get("ALPACA_SECRET_KEY", "")
TG_TOKEN, TG_CHAT = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip()
HDR = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET}
TAG = "orbd"


# ------------------------------------------------------------------ plumbing
def now():
    return datetime.now(ET)


def log(msg):
    print(f"[{now():%Y-%m-%d %H:%M:%S %Z}] {msg}", flush=True)


def req(method, url, body=None, tries=4):
    data = json.dumps(body).encode() if body is not None else None
    h = dict(HDR, **({"Content-Type": "application/json"} if data else {}))
    for k in range(tries):
        try:
            r = urllib.request.Request(url, data=data, headers=h, method=method)
            with urllib.request.urlopen(r, timeout=20) as resp:
                txt = resp.read().decode()
                return json.loads(txt) if txt else {}
        except urllib.error.HTTPError as e:
            msg = e.read().decode()[:300]
            if e.code in (429, 500, 502, 503, 504) and k < tries - 1:
                time.sleep(2 + 2 * k)
                continue
            raise RuntimeError(f"{method} {url.split('?')[0]} -> HTTP {e.code}: {msg}")
        except (urllib.error.URLError, TimeoutError) as e:
            if k < tries - 1:
                time.sleep(2 + 2 * k)
                continue
            raise RuntimeError(f"{method} {url.split('?')[0]} -> {e}")


def get(base, path, **params):
    q = ("?" + urllib.parse.urlencode(params)) if params else ""
    return req("GET", base + path + q)


def tg(text):
    log("TG: " + text.replace("\n", " | "))
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        body = json.dumps({"chat_id": TG_CHAT, "text": text}).encode()
        r = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=body,
                                   headers={"Content-Type": "application/json"})
        urllib.request.urlopen(r, timeout=15).read()
    except Exception as e:  # alerts must never crash the bot
        log(f"telegram failed: {e}")


def sleep_until(t: datetime):
    while True:
        s = (t - now()).total_seconds()
        if s <= 0:
            return
        time.sleep(min(s, 30))


# ------------------------------------------------------------------ market data
def first_candle(sym, d: date):
    st = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET)
    js = get(DATA, "/v2/stocks/bars", symbols=sym, timeframe="5Min", feed="iex",
             start=st.isoformat(), end=(st + timedelta(minutes=5)).isoformat(), limit=10)
    bars = (js.get("bars") or {}).get(sym) or []
    b = [x for x in bars if datetime.fromisoformat(x["t"].replace("Z", "+00:00")).astimezone(ET) == st]
    return b[0] if b else None


def last_price(syms):
    js = get(DATA, "/v2/stocks/trades/latest", symbols=",".join(syms), feed="iex")
    return {s: float(v["p"]) for s, v in (js.get("trades") or {}).items()}


def pick_contract(sym, cp, price, d: date):
    for dd in range(0, 6):
        exp = d + timedelta(days=dd)
        js = get(TRADE, "/v2/options/contracts", underlying_symbols=sym, expiration_date=exp.isoformat(),
                 type="call" if cp == "C" else "put", strike_price_gte=f"{price * 0.97:.2f}",
                 strike_price_lte=f"{price * 1.03:.2f}", limit=200)
        cs = [c for c in js.get("option_contracts", []) if c.get("tradable", True)]
        if cs:
            return min(cs, key=lambda c: abs(float(c["strike_price"]) - price))
    return None


def option_quote(occ):
    js = get(DATA, "/v1beta1/options/snapshots", symbols=occ, feed="indicative")
    q = ((js.get("snapshots") or {}).get(occ) or {}).get("latestQuote") or {}
    return float(q.get("bp") or 0), float(q.get("ap") or 0)


# ------------------------------------------------------------------ account and safety
def account():
    a = get(TRADE, "/v2/account")
    if not a.get("account_number", "").startswith("PA") and MODE != "LIVE":
        raise SystemExit("Not a paper account and MODE is not LIVE. Stopping.")
    return a


def bot_realized_pnl(since: str):
    """Realized P&L of this bot's filled option orders since `since` (YYYY-MM-DD), in dollars."""
    st = datetime.fromisoformat(since).replace(tzinfo=ET)
    total, after = 0.0, st.isoformat()
    while True:
        batch = get(TRADE, "/v2/orders", status="closed", after=after, limit=500, direction="asc")
        for o in batch:
            if not o.get("client_order_id", "").startswith(TAG + "-") or o.get("status") != "filled":
                continue
            amt = float(o["filled_qty"]) * float(o["filled_avg_price"]) * 100
            total += amt if o["side"] == "sell" else -amt
        if len(batch) < 500:
            return total
        after = batch[-1]["submitted_at"]


def sizing_equity(real_equity):
    if VIRTUAL_START > 0 and VIRTUAL_SINCE:
        return VIRTUAL_START + bot_realized_pnl(VIRTUAL_SINCE)
    return real_equity


def kill_switch(equity):
    try:
        h = get(TRADE, "/v2/account/portfolio/history", period="1A", timeframe="1D")
        hwm = max([x for x in h.get("equity", []) if x] + [equity])
    except Exception:
        hwm = equity
    return equity < 0.85 * hwm, hwm


def option_positions():
    return [p for p in get(TRADE, "/v2/positions") if p.get("asset_class") == "us_option"]


def sell_all(occ, qty, why):
    o = req("POST", TRADE + "/v2/orders", {"symbol": occ, "qty": str(qty), "side": "sell", "type": "market",
                                         "time_in_force": "day", "client_order_id": f"{TAG}-x-{why}-{int(time.time())}"})
    return wait_fill(o["id"], 60)


def wait_fill(oid, secs):
    t0 = time.time()
    while time.time() - t0 < secs:
        o = get(TRADE, f"/v2/orders/{oid}")
        if o["status"] == "filled":
            return o
        if o["status"] in ("canceled", "expired", "rejected"):
            return o
        time.sleep(2)
    return get(TRADE, f"/v2/orders/{oid}")


# ------------------------------------------------------------------ trading
def plan_today(d: date, dry=False):
    plans = []
    for sym in SYMS:
        b = first_candle(sym, d)
        if not b:
            log(f"{sym}: no 9:30 candle"); continue
        side = 1 if b["c"] > b["o"] else -1 if b["c"] < b["o"] else 0
        if side == 0:
            plans.append({"sym": sym, "skip": "flat first candle"}); continue
        stop = b["l"] if side == 1 else b["h"]
        plans.append({"sym": sym, "side": side, "stop": stop, "candle": b})
    return plans


def enter(plan, equity, d, dry=False):
    sym, side = plan["sym"], plan["side"]
    px = last_price([sym])[sym]
    risk = side * (px - plan["stop"])
    if risk <= 0:
        return {**plan, "skip": f"price {px:.2f} already beyond the stop {plan['stop']:.2f}"}
    target = px + side * 10 * risk
    cp = "C" if side == 1 else "P"
    c = pick_contract(sym, cp, px, d)
    if not c:
        return {**plan, "skip": "no contract found"}
    occ = c["symbol"]
    bid, ask = option_quote(occ)
    if ask <= 0:
        return {**plan, "skip": f"no quote for {occ}"}
    qty = math.floor(RISK * equity / (ask * 100))
    info = {**plan, "entry_px": px, "target": target, "occ": occ, "bid": bid, "ask": ask, "qty": qty,
            "premium": qty * ask * 100}
    if qty < 1:
        return {**info, "skip": f"{RISK:.0%} of the account (${RISK * equity:,.0f}) buys no contract at ${ask * 100:,.0f}"}
    if dry:
        return {**info, "dry": True}
    limit = round(ask + 0.02, 2)
    cid = f"{TAG}-{sym}-{cp}-s{plan['stop']:.2f}-t{target:.2f}-{int(time.time())}"
    o = req("POST", TRADE + "/v2/orders", {"symbol": occ, "qty": str(qty), "side": "buy", "type": "limit",
                                         "limit_price": f"{limit:.2f}", "time_in_force": "day", "client_order_id": cid})
    o = wait_fill(o["id"], 60)
    if o["status"] != "filled":
        try:
            req("DELETE", TRADE + f"/v2/orders/{o['id']}")
        except Exception:
            pass
        return {**info, "skip": f"buy not filled in 60s (status {o['status']}), canceled"}
    info["fill"] = float(o["filled_avg_price"]); info["qty"] = int(float(o["filled_qty"]))
    return info


def manage(trades, until: datetime):
    """Watch the ETFs every 10 s; exit on stop or target. Returns trades still open at `until`."""
    open_t = [t for t in trades if "fill" in t and "exit" not in t]
    while open_t and now() < until:
        try:
            px = last_price([t["sym"] for t in open_t])
        except Exception as e:
            log(f"price error: {e}"); time.sleep(10); continue
        for t in open_t:
            p = px.get(t["sym"])
            if p is None:
                continue
            s = t["side"]
            why = "stop" if s * (p - t["stop"]) <= 0 else "target" if s * (p - t["target"]) >= 0 else None
            if why:
                o = sell_all(t["occ"], t["qty"], why)
                t["exit"] = float(o.get("filled_avg_price") or 0); t["why"] = why
                pnl = (t["exit"] - t["fill"]) * 100 * t["qty"]
                tg(f"EXIT {t['sym']} {t['occ']} ({why} at {p:.2f})\nsold {t['qty']} @ {t['exit']:.2f}, "
                   f"bought @ {t['fill']:.2f}\nP&L ${pnl:,.0f} ({(t['exit'] / t['fill'] - 1):+.0%})")
        open_t = [t for t in open_t if "exit" not in t]
        time.sleep(10)
    return open_t


def rebuild_open():
    """Afternoon: rebuild open trades from today's filled buy orders (stop/target are in the client id)."""
    pos = {p["symbol"]: p for p in option_positions()}
    d0 = now().replace(hour=0, minute=0, second=0, microsecond=0)
    orders = get(TRADE, "/v2/orders", status="all", after=d0.isoformat(), limit=200, direction="asc")
    out = []
    for o in orders:
        cid = o.get("client_order_id", "")
        if not cid.startswith(TAG + "-") or o["side"] != "buy" or o["status"] != "filled" or o["symbol"] not in pos:
            continue
        _, sym, cp, s, tt, _ = cid.split("-")
        out.append({"sym": sym, "side": 1 if cp == "C" else -1, "stop": float(s[1:]), "target": float(tt[1:]),
                    "occ": o["symbol"], "qty": int(float(pos[o["symbol"]]["qty"])), "fill": float(o["filled_avg_price"])})
    return out


def close_everything(why):
    res = []
    for p in option_positions():
        q = int(float(p["qty"]))
        if q > 0:
            o = sell_all(p["symbol"], q, why)
            res.append(f"{p['symbol']} x{q} @ {o.get('filled_avg_price')}, P&L ${float(p.get('unrealized_pl') or 0):,.0f}")
    return res


# ------------------------------------------------------------------ phases
def main(phase):
    log(f"MODE: {MODE} | phase {phase} | risk per trade {RISK:.1%}")
    if MODE != "PAPER":
        raise SystemExit("This bot only runs in PAPER mode until the owner approves live trading.")
    a = account()
    real_equity = float(a["equity"])
    equity = sizing_equity(real_equity)
    virt = VIRTUAL_START > 0 and VIRTUAL_SINCE
    if virt:
        log(f"virtual account: started ${VIRTUAL_START:,.0f} on {VIRTUAL_SINCE}, now ${equity:,.2f} "
            f"(real paper equity ${real_equity:,.0f})")
    today = now().date()
    clock = get(TRADE, "/v2/clock")
    cal = get(TRADE, "/v2/calendar", start=today.isoformat(), end=today.isoformat())
    is_trading_day = bool(cal) and cal[0]["date"] == today.isoformat()

    if phase == "dry":
        d = today if is_trading_day else date.fromisoformat(
            get(TRADE, "/v2/calendar", start=(today - timedelta(days=7)).isoformat(), end=today.isoformat())[-1]["date"])
        plans = plan_today(d)
        lines = [f"MODE: {MODE} | DRY RUN (no orders)",
                 f"Sizing off {'VIRTUAL' if virt else 'real'} equity ${equity:,.2f}, {RISK:.0%} per trade",
                 f"Market open now: {clock['is_open']}. Using first candles from {d}"]
        nxt = date.fromisoformat(clock["next_open"][:10])
        for p in plans:
            if p.get("skip"):
                lines.append(f"{p['sym']}: {p['skip']}"); continue
            r = enter(p, equity, nxt, dry=True)
            if r.get("skip"):
                lines.append(f"{p['sym']}: {r['skip']}")
            else:
                lines.append(f"{p['sym']}: {'CALL' if p['side'] == 1 else 'PUT'} {r['occ']} ask ${r['ask']:.2f} -> "
                             f"{r['qty']} contracts (${r['premium']:,.0f}); stop {p['stop']:.2f}, target {r['target']:.2f}")
        tg("\n".join(lines))
        return

    if not is_trading_day:
        log("Market closed today. Nothing to do."); return

    if phase == "close":
        res = close_everything("backup")
        tg("Backup close: " + ("; ".join(res) if res else "nothing open"))
        return

    close_t = datetime.combine(today, datetime.strptime(cal[0]["close"], "%H:%M").time(), ET) if is_trading_day else None
    exit_t = close_t - timedelta(minutes=15) if close_t else None

    if phase == "morning":
        if now() > now().replace(hour=9, minute=45, second=0, microsecond=0):
            tg("Morning job started after 9:45 ET, too late for the 9:35 entry. No trades today."); return
        d0 = now().replace(hour=0, minute=0, second=0, microsecond=0)
        prior = [o for o in get(TRADE, "/v2/orders", status="all", after=d0.isoformat(), limit=200)
                 if o.get("client_order_id", "").startswith(TAG + "-") and o["side"] == "buy"]
        if prior:
            log("Entries already placed today by another run. Not entering again."); return
        killed, hwm = kill_switch(real_equity) if not virt else (False, equity)
        if virt and equity < 50:
            tg(f"Virtual account is down to ${equity:,.2f}. No trades. Waiting for the owner."); return
        if killed:
            tg(f"KILL SWITCH: equity ${equity:,.0f} is more than 15% below its high ${hwm:,.0f}. No trades. Waiting for the owner.")
            return
        tg(f"MODE: {MODE}\nORB bot is up. {'Virtual account' if virt else 'Equity'} ${equity:,.2f}, "
           f"{RISK:.0%} per trade (${RISK * equity:,.0f}). Waiting for the 9:35 ET candle.")
        sleep_until(now().replace(hour=9, minute=35, second=4, microsecond=0))
        plans = plan_today(today)
        trades = []
        for p in plans:
            if p.get("skip"):
                trades.append(p); continue
            try:
                trades.append(enter(p, equity, today))
            except Exception as e:
                trades.append({**p, "skip": f"error: {e}"})
        msg = ["9:35 ET entries:"]
        for t in trades:
            if t.get("skip"):
                msg.append(f"{t['sym']}: no trade ({t['skip']})")
            else:
                msg.append(f"{t['sym']}: BUY {t['qty']} {t['occ']} @ {t['fill']:.2f} (${t['fill'] * 100 * t['qty']:,.0f}); "
                           f"ETF {t['entry_px']:.2f}, stop {t['stop']:.2f}, target {t['target']:.2f}")
        tg("\n".join(msg))
        hand_off = now().replace(hour=14, minute=30, second=0, microsecond=0)
        if exit_t <= hand_off:  # early close: the morning job finishes the day itself
            manage(trades, exit_t)
            res = close_everything("eod")
            tg("End of day (early close): " + ("; ".join(res) if res else "nothing open"))
            return
        manage(trades, hand_off)
        log("Morning phase done; afternoon job takes over.")
        return

    if phase == "afternoon":
        if exit_t <= now().replace(hour=14, minute=30, second=0, microsecond=0):
            log("Early close day: the morning job handled the exit."); return
        sleep_until(now().replace(hour=14, minute=31, second=0, microsecond=0))
        trades = rebuild_open()
        log(f"open trades picked up: {len(trades)}")
        manage(trades, exit_t)
        res = close_everything("eod")
        a2 = account()
        end_eq = sizing_equity(float(a2["equity"]))
        tg("End of day: " + ("; ".join(res) if res else "no positions left at 15:45") +
           (f"\nVirtual account ${end_eq:,.2f} (started ${VIRTUAL_START:,.0f} on {VIRTUAL_SINCE})" if virt
            else f"\nEquity ${end_eq:,.0f} (start ${equity:,.0f})"))
        return


if __name__ == "__main__":
    try:
        main(sys.argv[1] if len(sys.argv) > 1 else "dry")
    except SystemExit:
        raise
    except Exception as e:
        tg(f"BOT ERROR ({sys.argv[1] if len(sys.argv) > 1 else 'dry'}): {e}")
        raise
