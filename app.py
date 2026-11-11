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

Riskregler (hårdkodade i servern, gäller oavsett vad strategierna säger):
  STOP_PCT_STOCK      standard 2   -> stäng aktieposition vid -2 %
  STOP_PCT_CRYPTO     standard 4   -> stäng kryptoposition vid -4 %
  MAX_DAILY_LOSS_PCT  standard 3   -> vid -3 % på en dag: stäng allt, inga nya köp förrän nästa dag
  SIM_START_DATE      standard 2026-10-17 (start för journal och jämförelse)
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
from alpaca.data.requests import (CryptoBarsRequest, CryptoLatestTradeRequest, StockBarsRequest,
                                  StockLatestTradeRequest)
from alpaca.data.timeframe import TimeFrame
from alpaca.common.enums import Sort
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, GetPortfolioHistoryRequest, MarketOrderRequest

app = Flask(__name__)

API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
POSITION_PCT = float(os.environ.get("POSITION_PCT", "5") or 0)   # 0 = använd qty från alerten
START_EQUITY = float(os.environ.get("START_EQUITY", "100000") or 100000)
# Simulerat konto: handla som om kontot vore SIM_START USD (t.ex. 2000 ≈ 20 000 kr).
# SIM_BASE = Alpacas verkliga kontovärde när simuleringen startade.
SIM_START = float(os.environ.get("SIM_START", "0") or 0)
SIM_BASE = float(os.environ.get("SIM_BASE", "0") or 0)
STOCK_SHORTS = os.environ.get("STOCK_SHORTS", "0") == "1"   # 0 = bara long på aktier
TZ = ZoneInfo("Europe/Stockholm")
STOP_PCT_STOCK = float(os.environ.get("STOP_PCT_STOCK", "2") or 0)
STOP_PCT_CRYPTO = float(os.environ.get("STOP_PCT_CRYPTO", "4") or 0)
MAX_DAILY_LOSS_PCT = float(os.environ.get("MAX_DAILY_LOSS_PCT", "3") or 0)
RISK_CHECK_SECONDS = int(os.environ.get("RISK_CHECK_SECONDS", "30") or 30)
SIM_START_DATE = os.environ.get("SIM_START_DATE", "2026-10-17")

trading = TradingClient(API_KEY, SECRET_KEY, paper=True) if API_KEY and SECRET_KEY else None
stock_data = StockHistoricalDataClient(API_KEY, SECRET_KEY) if API_KEY and SECRET_KEY else None
crypto_data = CryptoHistoricalDataClient(API_KEY, SECRET_KEY) if API_KEY and SECRET_KEY else None

order_lock = threading.Lock()          # en order i taget
recent_signals = {}                    # skydd mot dubbla alerts
DEDUPE_SECONDS = 60
event_log = deque(maxlen=200)          # syns på /status
risk_state = {"day": None, "halted": False, "stop_sent": {}, "last_check": None}


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


def sizing_equity(real_equity):
    """Kontovärdet som positionerna räknas på (simulerat om SIM_START är satt)."""
    if SIM_START > 0:
        return max(0.0, SIM_START + (real_equity - SIM_BASE))
    return real_equity


def order_qty(order_symbol, is_crypto, qty_from_alert, fractional=False):
    if POSITION_PCT > 0:
        equity = sizing_equity(float(trading.get_account().equity))
        price = latest_price(order_symbol, is_crypto)
        raw = equity * POSITION_PCT / 100 / price
    else:
        try:
            raw = float(qty_from_alert)
        except (TypeError, ValueError):
            raw = 1.0
    if is_crypto:
        return math.floor(raw * 1_000_000) / 1_000_000
    if fractional:
        return math.floor(raw * 1000) / 1000          # delaktier (bara long)
    return max(1, int(math.floor(raw)))


def submit(order_symbol, side, qty, is_crypto):
    req = MarketOrderRequest(
        symbol=order_symbol,
        qty=qty,
        side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
        time_in_force=TimeInForce.GTC if is_crypto else TimeInForce.DAY,
    )
    o = trading.submit_order(req)
    _entry_cache["t"] = 0
    log(f"ORDER {side.upper()} {qty} {order_symbol} -> id {o.id} status {o.status}")


def close(pos_symbol):
    trading.close_position(pos_symbol)
    _entry_cache["t"] = 0
    log(f"CLOSE {pos_symbol}")


# ---------- Riskregler (koden handlar, inte strategin eller någon AI) ----------

def _today():
    return datetime.now(TZ).strftime("%Y-%m-%d")


HALT_FILE = "/tmp/alpaca_halt_{}"


def _halted_today():
    return (risk_state["halted"] and risk_state["day"] == _today()) or os.path.exists(HALT_FILE.format(_today()))


def entries_blocked(pos_symbol):
    """Inga nya positioner när dagens förlustgräns har nåtts (gäller alla serverprocesser)."""
    if _halted_today():
        log(f"SKIP {pos_symbol}: handelsstopp, dagens förlustgräns ({MAX_DAILY_LOSS_PCT:g} %) är nådd")
        return True
    return False


def risk_check():
    today = _today()
    if risk_state["day"] != today:
        if risk_state["halted"]:
            log("RISK: nytt handelsdygn, handelsstoppet hävs")
        risk_state.update(day=today, halted=False)
    risk_state["last_check"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    acc = trading.get_account()
    real_eq, last_eq = float(acc.equity), float(acc.last_equity)
    base = sizing_equity(last_eq)
    positions = trading.get_all_positions()
    market_open = None

    # 1) Max daglig förlust: stäng allt och stoppa nya köp resten av dagen
    if MAX_DAILY_LOSS_PCT > 0 and base > 0 and not risk_state["halted"]:
        day_pct = (real_eq - last_eq) / base * 100
        if day_pct <= -MAX_DAILY_LOSS_PCT:
            risk_state["halted"] = True
            try:
                open(HALT_FILE.format(today), "w").close()
            except OSError:
                pass
            log(f"RISK: dagens resultat {day_pct:.2f} % <= -{MAX_DAILY_LOSS_PCT:g} %, stänger allt och stoppar nya köp")
            with order_lock:
                for p in positions:
                    risk_state["stop_sent"][p.symbol] = time.time()
                    try:
                        close(p.symbol)
                    except APIError as e:
                        log(f"FEL RISK stäng {p.symbol}: {e}")
            return

    # 2) Stopp per position
    for p in positions:
        is_crypto = "crypto" in _v(p.asset_class)
        limit = STOP_PCT_CRYPTO if is_crypto else STOP_PCT_STOCK
        if limit <= 0:
            continue
        plpc = float(p.unrealized_plpc) * 100
        try:
            j_entry = journal_entries().get(p.symbol.replace("/", ""), {}).get("entry")
            if j_entry and p.current_price:
                plpc = (float(p.current_price) / j_entry - 1) * 100 * (1 if "long" in _v(p.side) else -1)
        except Exception as e:
            log(f"FEL inköpspris {p.symbol}: {type(e).__name__}: {e}")
        if plpc > -limit:
            continue
        if time.time() - risk_state["stop_sent"].get(p.symbol, 0) < 600:
            continue                                   # redan skickat, vänta på fill
        if not is_crypto:
            if market_open is None:
                market_open = bool(trading.get_clock().is_open)
            if not market_open:
                continue                               # aktier stängs när börsen är öppen
        log(f"STOPP {p.symbol}: {plpc:.2f} % <= -{limit:g} %, stänger positionen")
        risk_state["stop_sent"][p.symbol] = time.time()
        with order_lock:
            try:
                close(p.symbol)
            except APIError as e:
                log(f"FEL STOPP {p.symbol}: {e}")


def risk_loop():
    time.sleep(10)
    while True:
        try:
            if trading is not None:
                risk_check()
        except Exception as e:
            log(f"FEL riskvakt: {type(e).__name__}: {e}")
        time.sleep(RISK_CHECK_SECONDS)


_risk_thread = {"t": None, "pid": None}
_risk_lock = threading.Lock()
_leader = {"fh": None, "pid": None, "tried": 0}


def _take_leader_lock():
    """Bara en process (gunicorn-worker) får köra riskvakten, så att inga order dubbleras."""
    if _leader["fh"] is not None and _leader["pid"] == os.getpid():
        return True
    if time.time() - _leader["tried"] < 60 and _leader["pid"] == os.getpid():
        return False
    _leader.update(tried=time.time(), pid=os.getpid(), fh=None)
    try:
        import fcntl
        fh = open("/tmp/alpaca_risk_guard.lock", "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _leader["fh"] = fh
        return True
    except (OSError, ImportError):
        return False


def ensure_risk_guard():
    """Startar riskvakten i den process som faktiskt svarar (gunicorn kan forka efter import)."""
    if trading is None or os.environ.get("RISK_GUARD", "1") != "1":
        return
    with _risk_lock:
        t = _risk_thread["t"]
        if t is None or not t.is_alive() or _risk_thread["pid"] != os.getpid():
            if not _take_leader_lock():
                return                                 # en annan process kör redan riskvakten
            t = threading.Thread(target=risk_loop, daemon=True)
            t.start()
            _risk_thread.update(t=t, pid=os.getpid())
            log(f"RISK: riskvakten startad (pid {os.getpid()})")


# ---------- Signal-logik ----------

def go_to_target(target, order_symbol, pos_symbol, is_crypto, qty_alert):
    """Format A: gör Alpaca-positionen till target (long/short/flat)."""
    side, qty = current_position(pos_symbol)
    if side == target:
        log(f"SKIP {pos_symbol}: redan {target}")
        return
    if side != "flat":
        close(pos_symbol)
        if target == "flat":
            return                                  # bara stänga, ingen ny order behövs
        if not wait_until_flat(pos_symbol, timeout=20.0):
            log(f"FEL {pos_symbol}: positionen stängdes inte i tid, avbryter")
            return
    if target == "flat":
        return
    if target == "short" and is_crypto:
        log(f"SKIP {pos_symbol}: Alpaca tillåter inte short på krypto")
        return
    if target == "short" and not STOCK_SHORTS:
        log(f"SKIP {pos_symbol}: bara long på aktier")
        return
    if entries_blocked(pos_symbol):
        return
    submit(order_symbol, "buy" if target == "long" else "sell",
           order_qty(order_symbol, is_crypto, qty_alert, fractional=(target == "long" and not is_crypto)),
           is_crypto)


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
            if entries_blocked(pos_symbol):
                return
            submit(order_symbol, "buy", order_qty(order_symbol, is_crypto, qty_alert, fractional=not is_crypto), is_crypto)
        else:
            log(f"SKIP {pos_symbol}: redan long")
        return
    if action == "sell":
        if side == "long":
            close(pos_symbol)                       # exit long
        elif side == "flat":
            if is_crypto:
                log(f"SKIP {pos_symbol}: Alpaca tillåter inte short på krypto")
            elif not STOCK_SHORTS:
                log(f"SKIP {pos_symbol}: bara long på aktier")
            elif not entries_blocked(pos_symbol):
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


@app.before_request
def _guard():
    ensure_risk_guard()


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
                    "position_pct": POSITION_PCT, "risk": {"halted": risk_state["halted"],
                    "last_check": risk_state["last_check"], "pid": os.getpid(),
                    "guard_alive": bool(_risk_thread["t"] and _risk_thread["t"].is_alive())},
                    "log": list(event_log)[-60:]}), 200


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


# ---------- Affärsjournal (från Alpacas orderhistorik, överlever omstarter) ----------

def _start_dt():
    return datetime.strptime(SIM_START_DATE, "%Y-%m-%d").replace(tzinfo=TZ)


def fetch_fills(after):
    fills, cursor, seen = [], after.astimezone(timezone.utc), set()
    for _ in range(20):
        batch = trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED, after=cursor,
                                                    limit=500, direction=Sort.ASC))
        new = [o for o in batch if str(o.id) not in seen]
        for o in new:
            seen.add(str(o.id))
            if o.filled_at is not None and float(o.filled_qty or 0) > 0:
                fills.append({"symbol": o.symbol.replace("/", ""), "side": _v(o.side),
                              "qty": float(o.filled_qty), "price": float(o.filled_avg_price or 0),
                              "time": o.filled_at})
        if len(batch) < 500 or not new:
            break
        cursor = max(o.submitted_at for o in batch)
    fills.sort(key=lambda f: f["time"])
    return fills


def _is_crypto_symbol(sym):
    return sym.endswith(("USD", "USDT", "USDC")) and len(sym) > 4


def build_journal():
    """Räknar ihop avslutade affärer (in -> ut) per symbol. Avgifter ingår inte.
    Smårester (avgifter/avrundning, under 2 % av affären) räknas som stängt, så att
    nästa affär får rätt inköpspris."""
    start = _start_dt()
    fills = fetch_fills(start - timedelta(days=120))   # tidigare fills behövs för att känna inköpspris
    # Startläge: räkna baklänges från dagens faktiska positioner, så att gamla rester stämmer
    current = {}
    for p in trading.get_all_positions():
        q = abs(float(p.qty)) * (1 if "long" in _v(p.side) else -1)
        current[p.symbol.replace("/", "")] = q
    net = {}
    for f in fills:
        net[f["symbol"]] = net.get(f["symbol"], 0.0) + (f["qty"] if f["side"] == "buy" else -f["qty"])
    pos, trades = {}, []
    for sym in set(net) | set(current):
        init = current.get(sym, 0.0) - net.get(sym, 0.0)
        if abs(init) > 1e-6 and not (init < 0 and _is_crypto_symbol(sym)):
            pos[sym] = {"qty": init, "avg": None, "open": None}  # okänt inköpspris (före fönstret)
    for f in fills:
        s = pos.setdefault(f["symbol"], {"qty": 0.0, "avg": 0.0, "open": None})
        signed = f["qty"] if f["side"] == "buy" else -f["qty"]
        if abs(s["qty"]) < 1e-9:                                   # ny position
            if signed < 0 and _is_crypto_symbol(f["symbol"]):
                continue                                           # krypto kan inte blankas: rest från gammal position
            s.update(qty=signed, avg=f["price"], open=f["time"])
            continue
        if (s["qty"] > 0) == (signed > 0):                        # ökar positionen
            tot = s["qty"] + signed
            if s["avg"] is not None:
                s["avg"] = (s["avg"] * abs(s["qty"]) + f["price"] * abs(signed)) / abs(tot)
            s["qty"] = tot
            continue
        closing = min(abs(signed), abs(s["qty"]))                 # minskar/stänger
        direction = 1 if s["qty"] > 0 else -1
        pnl = (f["price"] - s["avg"]) * closing * direction if s["avg"] else 0.0
        if f["time"] >= start and s["avg"] and s["open"] is not None:
            trades.append({"symbol": f["symbol"], "dir": "LONG" if direction > 0 else "SHORT",
                           "entry": round(s["avg"], 2), "exit": round(f["price"], 2), "qty": closing,
                           "pnl": round(pnl, 2), "pct": round((f["price"] / s["avg"] - 1) * 100 * direction, 2),
                           "opened": s["open"].astimezone(TZ).strftime("%Y-%m-%d %H:%M"),
                           "closed": f["time"].astimezone(TZ).strftime("%Y-%m-%d %H:%M"),
                           "hours": round((f["time"] - s["open"]).total_seconds() / 3600, 1)})
        dust = max(1e-6, 0.02 * closing)
        rest = abs(signed) - closing
        remaining = s["qty"] + direction * -closing
        if rest > dust and not _is_crypto_symbol(f["symbol"]):     # vände riktning (bara aktier)
            s.update(qty=rest * (1 if signed > 0 else -1), avg=f["price"], open=f["time"])
        elif abs(remaining) <= dust:
            s.update(qty=0.0, avg=0.0, open=None)
        else:
            s["qty"] = remaining
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gp, gl = sum(t["pnl"] for t in wins), -sum(t["pnl"] for t in losses)
    stats = {"trades": len(trades), "wins": len(wins), "losses": len(losses),
             "win_rate": round(len(wins) / len(trades) * 100, 0) if trades else 0,
             "realized": round(gp - gl, 2), "profit_factor": round(gp / gl, 2) if gl else None,
             "avg_win": round(gp / len(wins), 2) if wins else 0,
             "avg_loss": round(-gl / len(losses), 2) if losses else 0}
    # Öppna positioner = Alpacas faktiska positioner, med inköpspris enligt journalen
    open_pos = {k: {"qty": round(current[k], 9), "entry": round(v["avg"], 2),
                    "opened": v["open"].astimezone(TZ).strftime("%Y-%m-%d %H:%M")}
                for k, v in pos.items() if k in current and v["open"] is not None and v["avg"]}
    return {"since": SIM_START_DATE, "stats": stats, "trades": trades, "open": open_pos, "fills": len(fills)}


_entry_cache = {"t": 0, "open": {}}


def journal_entries(force=False):
    """Inköpspris per öppen position enligt orderhistoriken (cache 10 min)."""
    if force or time.time() - _entry_cache["t"] > 600:
        _entry_cache.update(t=time.time(), open=build_journal()["open"])
    return _entry_cache["open"]


def max_drawdown_pct():
    """Största nedgång i det simulerade kontovärdet sedan start (dagsdata)."""
    h = trading.get_portfolio_history(GetPortfolioHistoryRequest(start=_start_dt().astimezone(timezone.utc),
                                                                 timeframe="1D"))
    peak, mdd = None, 0.0
    for e in (h.equity or []):
        if e is None:
            continue
        v = sizing_equity(float(e))
        peak = v if peak is None or v > peak else peak
        if peak > 0:
            mdd = max(mdd, (1 - v / peak) * 100)
    return round(mdd, 2)


def buy_and_hold_pct():
    """Vad hade hänt om man bara köpt och behållit SPY respektive BTC sedan start?"""
    out = {}
    start = _start_dt().astimezone(timezone.utc)
    try:
        bars = stock_data.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY", timeframe=TimeFrame.Day,
                                                          start=start, end=start + timedelta(days=7),
                                                          feed=DataFeed.IEX))
        first = bars["SPY"][0].close
        out["SPY"] = round((latest_price("SPY", False) / first - 1) * 100, 2)
    except Exception as e:
        log(f"FEL jämförelse SPY: {type(e).__name__}: {e}")
    try:
        bars = crypto_data.get_crypto_bars(CryptoBarsRequest(symbol_or_symbols="BTC/USD", timeframe=TimeFrame.Day,
                                                             start=start, end=start + timedelta(days=3)))
        first = bars["BTC/USD"][0].close
        out["BTC"] = round((latest_price("BTC/USD", True) / first - 1) * 100, 2)
    except Exception as e:
        log(f"FEL jämförelse BTC: {type(e).__name__}: {e}")
    return out


@app.route("/journal", methods=["GET"])
def journal():
    if not authorized():
        return jsonify({"error": "unauthorized"}), 401
    if trading is None:
        return jsonify({"error": "API-nycklar saknas"}), 500
    j = build_journal()
    j["max_drawdown_pct"] = max_drawdown_pct()
    j["buy_and_hold_pct"] = buy_and_hold_pct()
    j["risk"] = {"halted": risk_state["halted"], "last_check": risk_state["last_check"],
                 "stop_pct_stock": STOP_PCT_STOCK, "stop_pct_crypto": STOP_PCT_CRYPTO,
                 "max_daily_loss_pct": MAX_DAILY_LOSS_PCT}
    return jsonify(j), 200


def build_report():
    now = datetime.now(TZ)
    since = now - timedelta(hours=24)   # senaste 24 h, så att USA-eftermiddagen efter förra rapporten kommer med
    acc = trading.get_account()
    real_eq, last_eq = float(acc.equity), float(acc.last_equity)
    day_pl = real_eq - last_eq
    eq = sizing_equity(real_eq)
    start = SIM_START if SIM_START > 0 else START_EQUITY
    day_pct = day_pl / (eq - day_pl) * 100 if (eq - day_pl) else 0
    tot_pl, tot_pct = eq - start, (eq / start - 1) * 100

    orders = trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED,
                                                 after=since.astimezone(timezone.utc), limit=100))
    fills = []
    for o in orders:
        if o.filled_at is None:
            continue
        fills.append({"time": o.filled_at.astimezone(TZ).strftime("%d/%m %H:%M"), "symbol": o.symbol,
                      "side": "KÖP" if _v(o.side) == "buy" else "SÄLJ", "qty": o.filled_qty,
                      "price": float(o.filled_avg_price or 0), "ts": o.filled_at})
    fills.sort(key=lambda f: f["ts"])

    positions = [{"symbol": p.symbol, "side": "LONG" if "long" in _v(p.side) else "SHORT", "qty": p.qty,
                  "entry": float(p.avg_entry_price), "pl": float(p.unrealized_pl),
                  "pl_pct": float(p.unrealized_plpc) * 100} for p in trading.get_all_positions()]

    today_str = now.strftime("%Y-%m-%d")
    cutoff = since.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    events = [l for l in event_log if l[:19] >= cutoff
              and any(k in l for k in ("SIGNAL", "ORDER", "CLOSE", "SKIP", "FEL", "STOPP", "RISK"))]
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
    honest_html = ""
    try:
        j = build_journal()
        st = j["stats"]
        mdd = max_drawdown_pct()
        bh = buy_and_hold_pct()
        pf = "-" if st["profit_factor"] is None else str(st["profit_factor"])
        cmp_rows = "<tr><td style='padding:2px 12px 2px 0'>Systemet</td><td><b>" + sign(tot_pct) + " %</b></td></tr>"
        for k in ("SPY", "BTC"):
            if k in bh:
                cmp_rows += ("<tr><td style='padding:2px 12px 2px 0'>Bara behålla " + k + "</td><td>"
                             + sign(bh[k]) + " %</td></tr>")
        honest_html = (
            "<h3>Ärligt resultat sedan " + SIM_START_DATE + "</h3>"
            "<table style='font-size:14px'>"
            "<tr><td style='padding:2px 12px 2px 0'>Avslutade affärer</td><td>" + str(st["trades"])
            + " (" + str(st["wins"]) + " vinst, " + str(st["losses"]) + " förlust, "
            + "{:.0f}".format(st["win_rate"]) + " % träff)</td></tr>"
            "<tr><td style='padding:2px 12px 2px 0'>Snittvinst / snittförlust</td><td>"
            + sign(st["avg_win"]) + " / " + sign(st["avg_loss"]) + " USD</td></tr>"
            "<tr><td style='padding:2px 12px 2px 0'>Profit factor</td><td>" + pf + " (över 1,0 = lönsamt)</td></tr>"
            "<tr><td style='padding:2px 12px 2px 0'>Största nedgång</td><td>-" + "{:.2f}".format(mdd) + " %</td></tr>"
            "</table>"
            "<p style='margin:8px 0 2px'><b>Jämförelse</b></p><table style='font-size:14px'>" + cmp_rows + "</table>"
        )
        if now.weekday() == 4:                                   # fredag: veckans obduktion
            week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
            lost = [t for t in j["trades"] if t["closed"] >= week_ago and t["pnl"] <= 0]
            honest_html += ("<h3>Veckans förlustaffärer (" + str(len(lost)) + ")</h3>"
                            + rows(lost, [("Symbol", lambda t: t["symbol"]), ("In", lambda t: t["opened"]),
                                          ("Ut", lambda t: t["closed"]), ("Timmar", lambda t: t["hours"]),
                                          ("Resultat", lambda t: sign(t["pnl"]) + " USD (" + sign(t["pct"]) + " %)")]))
    except Exception as e:
        log(f"FEL journal i rapport: {type(e).__name__}: {e}")
        errors.append("journal")
    risk_html = ("<p style='color:#666;font-size:13px'>Riskregler: stopp -" + "{:g}".format(STOP_PCT_STOCK)
                 + " % aktier, -" + "{:g}".format(STOP_PCT_CRYPTO) + " % krypto, handelsstopp vid -"
                 + "{:g}".format(MAX_DAILY_LOSS_PCT) + " % per dag"
                 + (" · <b style='color:#cf222e'>HANDELSSTOPP AKTIVT IDAG</b>" if _halted_today() else "")
                 + "</p>")
    if errors:
        status_html = "<p style='color:#cf222e'><b>Fel idag: " + str(len(errors)) + " - se händelselistan ovan.</b></p>"
    else:
        status_html = "<p style='color:#1a7f37'>Inga fel. Servern är igång.</p>"
    html = (
        "<div style='font-family:Arial,sans-serif;max-width:620px'>"
        "<h2 style='margin-bottom:4px'>Trading-rapport " + today_str + "</h2>"
        "<p style='margin-top:0;color:#666'>Alpaca paper trading · BTC: EMA Cross 15m · SPY: Casper FCR (long)"
        + (" · simulerat konto " + "{:,.0f}".format(SIM_START) + " USD" if SIM_START > 0 else "") + "</p>"
        "<table style='font-size:15px'>"
        "<tr><td style='padding:2px 12px 2px 0'>Resultat idag</td><td style='color:" + color + "'><b>"
        + sign(day_pl) + " USD (" + sign(day_pct) + " %)</b></td></tr>"
        "<tr><td style='padding:2px 12px 2px 0'>Totalt sedan start</td><td><b>"
        + sign(tot_pl) + " USD (" + sign(tot_pct) + " %)</b></td></tr>"
        "<tr><td style='padding:2px 12px 2px 0'>Kontovärde</td><td>" + "{:,.2f}".format(eq) + " USD</td></tr>"
        "</table>"
        "<h3>Affärer senaste 24 h (" + str(len(fills)) + ")</h3>" + fills_html
        + "<h3>Öppna positioner (" + str(len(positions)) + ")</h3>" + pos_html
        + honest_html
        + "<h3>Signaler och händelser senaste 24 h (" + str(len(events)) + ")</h3>" + events_html
        + status_html + risk_html + "</div>"
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
