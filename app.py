"""
TradingView -> Alpaca webhook (paper trading)

Tar emot två format:
  A) Strategy-alerts (rekommenderat):
     {"symbol":"{{ticker}}","action":"{{strategy.order.action}}",
      "position":"{{strategy.market_position}}","qty":"{{strategy.order.contracts}}"}
     -> servern ser till att Alpaca-positionen blir long / short / flat.
  B) Enkla alerts (t.ex. alertcondition i indicator()):
     {"symbol":"{{ticker}}","action":"buy"}   eller  "sell" / "close"

Miljövariabler på Render:
  ALPACA_API_KEY, ALPACA_SECRET_KEY   (krävs)
  WEBHOOK_SECRET                      (rekommenderas, skyddar webhooken)
  POSITION_PCT                        (valfri, standard 5 = 5 % av kontot per trade, 0 = qty från alerten)
"""

import json
import math
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import CryptoLatestTradeRequest, StockLatestTradeRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

app = Flask(__name__)

API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
POSITION_PCT = float(os.environ.get("POSITION_PCT", "5") or 0)   # 0 = använd qty från alerten
START_EQUITY = float(os.environ.get("START_EQUITY", "100000") or 100000)
TZ = ZoneInfo("Europe/Stockholm")

trading = TradingClient(API_KEY, SECRET_KEY, paper=True) if API_KEY and SECRET_KEY else None
stock_data = StockHistoricalDataClient(API_KEY, SECRET_KEY) if API_KEY and SECRET_KEY else None
crypto_data = CryptoHistoricalDataClient(API_KEY, SECRET_KEY) if API_KEY and SECRET_KEY else None

order_lock = threading.Lock()          # en order i taget
recent_signals = {}                    # skydd mot dubbla alerts
DEDUPE_SECONDS = 60
event_log = deque(maxlen=50)           # syns på /status


def log(msg):
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC | {msg}"
    print(line, flush=True)            # syns i Render -> Logs
    event_log.append(line)


# ---------- Symboler ----------

CRYPTO_QUOTES = ("USDT", "USDC", "USD")


def parse_symbol(raw):
    """Returnerar (order_symbol, position_symbol, is_crypto)."""
    s = (raw or "").upper().strip()
    if ":" in s:                       # t.ex. "COINBASE:BTCUSD"
        s = s.split(":")[1]
    if "/" in s:
        base = s.split("/")[0]
        return f"{base}/USD", f"{base}USD", True
    for q in CRYPTO_QUOTES:
        if s.endswith(q) and len(s) > len(q) + 1:
            base = s[: -len(q)]
            return f"{base}/USD", f"{base}USD", True
    return s, s, False


# ---------- Alpaca-hjälpfunktioner ----------

def current_position(pos_symbol):
    """Returnerar (side, qty) där side är 'long', 'short' eller 'flat'."""
    try:
        p = trading.get_open_position(pos_symbol)
        side = str(p.side.value if hasattr(p.side, "value") else p.side).lower()
        return ("short" if "short" in side else "long"), abs(float(p.qty))
    except APIError:
        return "flat", 0.0


def wait_until_flat(pos_symbol, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if current_position(pos_symbol)[0] == "flat":
            return True
        time.sleep(0.4)
    return False


def latest_price(order_symbol, is_crypto):
    if is_crypto:
        r = crypto_data.get_crypto_latest_trade(CryptoLatestTradeRequest(symbol_or_symbols=order_symbol))
    else:
        r = stock_data.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=order_symbol, feed=DataFeed.IEX))
    return float(r[order_symbol].price)


def order_qty(order_symbol, is_crypto, qty_from_alert):
    if POSITION_PCT > 0:
        equity = float(trading.get_account().equity)
        price = latest_price(order_symbol, is_crypto)
        raw = equity * POSITION_PCT / 100 / price
    else:
        try:
            raw = float(qty_from_alert)
        except (TypeError, ValueError):
            raw = 1.0
    if is_crypto:
        return math.floor(raw * 1_000_000) / 1_000_000
    return max(1, int(math.floor(raw)))


def submit(order_symbol, side, qty, is_crypto):
    req = MarketOrderRequest(
        symbol=order_symbol,
        qty=qty,
        side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
        time_in_force=TimeInForce.GTC if is_crypto else TimeInForce.DAY,
    )
    o = trading.submit_order(req)
    log(f"ORDER {side.upper()} {qty} {order_symbol} -> id {o.id} status {o.status}")


def close(pos_symbol):
    trading.close_position(pos_symbol)
    log(f"CLOSE {pos_symbol}")


# ---------- Signal-logik ----------

def go_to_target(target, order_symbol, pos_symbol, is_crypto, qty_alert):
    """Format A: gör Alpaca-positionen till target (long/short/flat)."""
    side, qty = current_position(pos_symbol)
    if side == target:
        log(f"SKIP {pos_symbol}: redan {target}")
        return
    if side != "flat":
        close(pos_symbol)
        if not wait_until_flat(pos_symbol):
            log(f"FEL {pos_symbol}: positionen stängdes inte i tid, avbryter")
            return
    if target == "flat":
        return
    if target == "short" and is_crypto:
        log(f"SKIP {pos_symbol}: Alpaca tillåter inte short på krypto")
        return
    submit(order_symbol, "buy" if target == "long" else "sell",
           order_qty(order_symbol, is_crypto, qty_alert), is_crypto)


def simple_action(action, order_symbol, pos_symbol, is_crypto, qty_alert):
    """Format B: buy/sell/close. Exit stänger alltid hela positionen."""
    side, _ = current_position(pos_symbol)
    if action == "close":
        if side != "flat":
            close(pos_symbol)
        else:
            log(f"SKIP {pos_symbol}: ingen position att stänga")
        return
    if action == "buy":
        if side == "short":
            close(pos_symbol)                       # exit short
        elif side == "flat":
            submit(order_symbol, "buy", order_qty(order_symbol, is_crypto, qty_alert), is_crypto)
        else:
            log(f"SKIP {pos_symbol}: redan long")
        return
    if action == "sell":
        if side == "long":
            close(pos_symbol)                       # exit long
        elif side == "flat":
            if is_crypto:
                log(f"SKIP {pos_symbol}: Alpaca tillåter inte short på krypto")
            else:
                submit(order_symbol, "sell", order_qty(order_symbol, is_crypto, qty_alert), is_crypto)
        else:
            log(f"SKIP {pos_symbol}: redan short")


def process(data):
    with order_lock:
        try:
            order_symbol, pos_symbol, is_crypto = parse_symbol(data.get("symbol") or data.get("ticker"))
            if not order_symbol:
                log(f"FEL: symbol saknas i {data}")
                return
            qty_alert = data.get("qty") or data.get("contracts")
            target = str(data.get("position") or data.get("market_position") or "").lower().strip()
            action = str(data.get("action") or data.get("side") or "").lower().strip()

            if target in ("long", "short", "flat"):
                go_to_target(target, order_symbol, pos_symbol, is_crypto, qty_alert)
                return

            if action not in ("buy", "sell", "close"):
                log(f"FEL: okänd action '{action}' i {data}")
                return
            key = (pos_symbol, action)
            if time.time() - recent_signals.get(key, 0) < DEDUPE_SECONDS:
                log(f"SKIP {pos_symbol}: dubbel-alert '{action}' inom {DEDUPE_SECONDS}s")
                return
            recent_signals[key] = time.time()
            simple_action(action, order_symbol, pos_symbol, is_crypto, qty_alert)
        except APIError as e:
            log(f"ALPACA-FEL: {e}")
        except Exception as e:
            log(f"FEL: {type(e).__name__}: {e}")


# ---------- Routes ----------

def authorized(data=None):
    if not WEBHOOK_SECRET:
        return True
    given = request.args.get("key") or (data or {}).get("passphrase")
    return given == WEBHOOK_SECRET


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    raw = request.get_data(as_text=True) or ""
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        log(f"FEL: inte giltig JSON: {raw[:200]!r}")
        return jsonify({"error": "invalid JSON"}), 400
    if not authorized(data):
        log("FEL: fel eller saknad nyckel")
        return jsonify({"error": "unauthorized"}), 401
    if trading is None:
        log("FEL: ALPACA_API_KEY / ALPACA_SECRET_KEY saknas")
        return jsonify({"error": "server not configured"}), 500
    log(f"SIGNAL {json.dumps({k: v for k, v in data.items() if k != 'passphrase'})}")
    threading.Thread(target=process, args=(data,), daemon=True).start()   # svara TradingView direkt
    return jsonify({"status": "received"}), 200


@app.route("/status", methods=["GET"])
def status():
    if not authorized():
        return jsonify({"error": "unauthorized"}), 401
    if trading is None:
        return jsonify({"error": "API-nycklar saknas"}), 500
    acc = trading.get_account()
    positions = [{"symbol": p.symbol, "side": str(p.side), "qty": p.qty,
                  "unrealized_pl": p.unrealized_pl} for p in trading.get_all_positions()]
    return jsonify({"equity": acc.equity, "cash": acc.cash, "positions": positions,
                    "position_pct": POSITION_PCT, "log": list(event_log)}), 200


@app.route("/test", methods=["GET"])
def test():
    """Testa kedjan från webbläsaren: /test?key=DIN_NYCKEL&symbol=SPY&action=buy"""
    if not authorized():
        return jsonify({"error": "unauthorized"}), 401
    data = {k: v for k, v in request.args.items() if k != "key"}
    log(f"TEST {data}")
    process(data)
    return jsonify({"status": "done", "log": list(event_log)[-5:]}), 200


def _v(x):
    return str(x.value if hasattr(x, "value") else x).lower().replace("positionside.", "").replace("orderside.", "")


def build_report():
    now = datetime.now(TZ)
    start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    acc = trading.get_account()
    eq, last_eq = float(acc.equity), float(acc.last_equity)
    day_pl, day_pct = eq - last_eq, (eq / last_eq - 1) * 100 if last_eq else 0
    tot_pl, tot_pct = eq - START_EQUITY, (eq / START_EQUITY - 1) * 100

    orders = trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED,
                                                 after=start_today.astimezone(timezone.utc), limit=100))
    fills = []
    for o in orders:
        if o.filled_at is None:
            continue
        fills.append({"time": o.filled_at.astimezone(TZ).strftime("%H:%M"), "symbol": o.symbol,
                      "side": "KÖP" if _v(o.side) == "buy" else "SÄLJ", "qty": o.filled_qty,
                      "price": float(o.filled_avg_price or 0)})
    fills.sort(key=lambda f: f["time"])

    positions = [{"symbol": p.symbol, "side": "LONG" if "long" in _v(p.side) else "SHORT", "qty": p.qty,
                  "entry": float(p.avg_entry_price), "pl": float(p.unrealized_pl),
                  "pl_pct": float(p.unrealized_plpc) * 100} for p in trading.get_all_positions()]

    today_str = now.strftime("%Y-%m-%d")
    events = [l for l in event_log if l.startswith(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
              and any(k in l for k in ("SIGNAL", "ORDER", "CLOSE", "FEL"))]
    errors = [l for l in events if "FEL" in l]

    sign = lambda x: f"+{x:,.2f}" if x >= 0 else f"{x:,.2f}"
    subject = f"Trading {today_str}: {sign(day_pl)} USD idag ({sign(day_pct)} %) | totalt {sign(tot_pct)} %"
    if errors:
        subject = "⚠️ " + subject

    def rows(items, cols):
        if not items:
            return "<p style='color:#666'>Inga.</p>"
        head = "".join(f"<th style='text-align:left;padding:4px 8px;border-bottom:1px solid #ddd'>{c}</th>" for c, _ in cols)
        body = "".join("<tr>" + "".join(f"<td style='padding:4px 8px'>{f(i)}</td>" for _, f in cols) + "</tr>" for i in items)
        return f"<table style='border-collapse:collapse;font-size:14px'><tr>{head}</tr>{body}</table>"

    color = "#1a7f37" if day_pl >= 0 else "#cf222e"
    fills_html = rows(fills, [("Tid", lambda f: f["time"]), ("Symbol", lambda f: f["symbol"]),
                              ("Typ", lambda f: f["side"]), ("Antal", lambda f: f["qty"]),
                              ("Pris", lambda f: "{:,.2f}".format(f["price"]))])
    pos_html = rows(positions, [("Symbol", lambda p: p["symbol"]), ("Riktning", lambda p: p["side"]),
                                ("Antal", lambda p: p["qty"]), ("Inköp", lambda p: "{:,.2f}".format(p["entry"])),
                                ("Orealiserat", lambda p: sign(p["pl"]) + " (" + sign(p["pl_pct"]) + " %)")])
    if events:
        events_html = "<pre style='font-size:12px;background:#f6f8fa;padding:8px'>" + chr(10).join(events) + "</pre>"
    else:
        events_html = "<p style='color:#666'>Inga signaler idag.</p>"
    if errors:
        status_html = "<p style='color:#cf222e'><b>Fel idag: " + str(len(errors)) + " - se händelselistan ovan.</b></p>"
    else:
        status_html = "<p style='color:#1a7f37'>Inga fel. Servern är igång.</p>"
    html = (
        "<div style='font-family:Arial,sans-serif;max-width:620px'>"
        "<h2 style='margin-bottom:4px'>Trading-rapport " + today_str + "</h2>"
        "<p style='margin-top:0;color:#666'>Alpaca paper trading · BTC: EMA Cross 15m · SPY: Casper FCR</p>"
        "<table style='font-size:15px'>"
        "<tr><td style='padding:2px 12px 2px 0'>Resultat idag</td><td style='color:" + color + "'><b>"
        + sign(day_pl) + " USD (" + sign(day_pct) + " %)</b></td></tr>"
        "<tr><td style='padding:2px 12px 2px 0'>Totalt sedan start</td><td><b>"
        + sign(tot_pl) + " USD (" + sign(tot_pct) + " %)</b></td></tr>"
        "<tr><td style='padding:2px 12px 2px 0'>Kontovärde</td><td>" + "{:,.2f}".format(eq) + " USD</td></tr>"
        "</table>"
        "<h3>Affärer idag (" + str(len(fills)) + ")</h3>" + fills_html
        + "<h3>Öppna positioner (" + str(len(positions)) + ")</h3>" + pos_html
        + "<h3>Signaler och händelser idag (" + str(len(events)) + ")</h3>" + events_html
        + status_html + "</div>"
    )
    return {"subject": subject, "html": html, "day_pl": round(day_pl, 2), "total_pct": round(tot_pct, 2),
            "fills": len(fills), "positions": len(positions), "errors": len(errors)}


@app.route("/report", methods=["GET"])
def report():
    if not authorized():
        return jsonify({"error": "unauthorized"}), 401
    if trading is None:
        return jsonify({"error": "API-nycklar saknas"}), 500
    try:
        return jsonify(build_report()), 200
    except Exception as e:
        log(f"FEL i rapport: {type(e).__name__}: {e}")
        return jsonify({"subject": "⚠️ Trading-rapport kunde inte skapas",
                        "html": f"<p>Fel: {type(e).__name__}: {e}</p>", "errors": 1}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
