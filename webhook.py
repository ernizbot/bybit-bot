import time, json, logging
from decimal import Decimal, ROUND_DOWN
from flask import Flask, request, jsonify
from pybit.unified_trading import HTTP

app = Flask(__name__)

# ---- Load config ----
with open("config.json", "r", encoding="utf-8") as f:
    CFG = json.load(f)

API_KEY   = CFG["BYBIT_API_KEY"]
API_SECRET= CFG["BYBIT_API_SECRET"]
ACCOUNT_TYPE = CFG.get("ACCOUNT_TYPE", "UNIFIED")
CATEGORY  = CFG.get("CATEGORY", "linear")

LEVERAGE  = int(CFG.get("LEVERAGE", 15))
RISK_PCT  = float(CFG.get("RISK_PCT", 0.02))              # 2% per trade
MIN_NOTIONAL_USDT = float(CFG.get("MIN_NOTIONAL_USDT", 5.0))

DEFAULT_TP_PCT = float(CFG.get("DEFAULT_TP_PCT", 0.01))   # 1% TP
DEFAULT_SL_PCT = float(CFG.get("DEFAULT_SL_PCT", 0.005))  # 0.5% SL

COOLDOWN_SEC = int(CFG.get("COOLDOWN_SEC", 60))
MAX_DRAWDOWN_PCT = float(CFG.get("MAX_DRAWDOWN_PCT", 0.10))

PORT = int(CFG.get("PORT", 5000))
LOG_LEVEL = CFG.get("LOG_LEVEL", "INFO").upper()

# Trailing stop fiksuotas 0.7% (kaip absoliuti kaina, Bybit v5)
TRAIL_PCT = 0.007

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("bybit-webhook")

session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)

# ---- State ----
SYMBOL_LAST_TS = {}
START_EQUITY   = None

# ---- Helpers ----
def get_equity_usdt() -> float:
    r = session.get_wallet_balance(accountType=ACCOUNT_TYPE, coin="USDT")
    li = r.get("result", {}).get("list", [])
    if not li: return 0.0
    return float(li[0].get("totalEquity", 0.0))

def get_last_price(symbol: str) -> float:
    r = session.get_tickers(category=CATEGORY, symbol=symbol)
    li = r.get("result", {}).get("list", [])
    if not li: raise ValueError(f"No ticker for {symbol}")
    return float(li[0]["lastPrice"])

def get_symbol_rules(symbol: str):
    r = session.get_instruments_info(category=CATEGORY, symbol=symbol)
    li = r.get("result", {}).get("list", [])
    if not li: raise ValueError(f"No instrument info for {symbol}")
    lot = li[0]["lotSizeFilter"]; prc = li[0]["priceFilter"]
    qty_step = Decimal(lot["qtyStep"])
    min_qty  = Decimal(lot["minOrderQty"])
    tick     = Decimal(prc["tickSize"])
    return qty_step, min_qty, tick

def round_step(val: float, step: Decimal) -> str:
    d = Decimal(str(val))
    return str((d // step) * step)

def ensure_leverage(symbol: str, lev: int):
    try:
        session.set_leverage(category=CATEGORY, symbol=symbol,
                             buyLeverage=str(lev), sellLeverage=str(lev))
    except Exception as e:
        if "leverage not modified" in str(e).lower():
            log.info(f"Leverage for {symbol} already {lev}x — continuing.")
        else:
            raise

def place_market_order(symbol: str, side: str, qty: str, tp: float|None, sl: float|None):
    params = {
        "category": CATEGORY,
        "symbol": symbol,
        "side": "Buy" if side.upper()=="BUY" else "Sell",
        "orderType": "Market",
        "qty": qty,
        "timeInForce": "IOC",
        "reduceOnly": False
    }
    # Hedge / One-way
    params["positionIdx"] = 1 if side.upper()=="BUY" else 2

    if tp is not None:
        params["takeProfit"] = str(tp)
        params["tpTriggerBy"] = "LastPrice"
    if sl is not None:
        params["stopLoss"] = str(sl)
        params["slTriggerBy"] = "LastPrice"

    return session.place_order(**params)

def set_trailing_stop(symbol: str, side: str, entry_price: float, tick: Decimal):
    """
    Bybit v5 trailingStop = absoliutus kainos atstumas.
    Pritaikom 0.7% nuo kainos ir suapvalinam iki tick.
    """
    try:
        distance = float(Decimal(str(entry_price * TRAIL_PCT)).quantize(tick, rounding=ROUND_DOWN))
        if distance <= 0:
            return
        params = {
            "category": CATEGORY,
            "symbol": symbol,
            "positionIdx": 1 if side.upper()=="BUY" else 2,
            "trailingStop": str(distance)
            # "activePrice": str(entry_price)  # jei norėtum aktyvavimo kainos — paliekam aktyvuoti iškart
        }
        resp = session.set_trading_stop(**params)
        log.info(f"Trailing stop set ({distance}) -> {resp}")
    except Exception as e:
        log.warning(f"Could not set trailing stop: {e}")

# ---- Guards ----
def equity_guard() -> tuple[bool,str]:
    global START_EQUITY
    eq = get_equity_usdt()
    if START_EQUITY is None:
        START_EQUITY = eq
        log.info(f"Start equity set: {START_EQUITY:.2f} USDT")
        return True, ""
    if START_EQUITY <= 0:
        return True, ""
    dd = (START_EQUITY - eq) / START_EQUITY
    if dd >= MAX_DRAWDOWN_PCT:
        return False, f"Max drawdown hit: {dd:.2%} >= {MAX_DRAWDOWN_PCT:.0%}"
    return True, ""

def cooldown_guard(symbol: str) -> tuple[bool,str]:
    now = time.time()
    last = SYMBOL_LAST_TS.get(symbol, 0)
    if now - last < COOLDOWN_SEC:
        left = int(COOLDOWN_SEC - (now - last))
        return False, f"Cooldown active for {symbol}: {left}s left"
    SYMBOL_LAST_TS[symbol] = now
    return True, ""

# ---- Routes ----
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True, silent=False)
    except Exception as e:
        return jsonify({"ok": False, "error": f"bad_json: {e}"}), 400

    log.info(f"Alert: {data}")

    symbol = str(data.get("symbol", "")).upper().replace(".P","")
    side   = str(data.get("side", "")).upper()
    tp_pct = float(data.get("tp_pct", DEFAULT_TP_PCT))
    sl_pct = float(data.get("sl_pct", DEFAULT_SL_PCT))

    if not symbol or side not in ("BUY","SELL","FLAT","CLOSE","EXIT"):
        return jsonify({"ok": False, "error": "symbol/side missing"}), 400

    if side in ("FLAT","CLOSE","EXIT"):
        for s in ("Buy","Sell"):
            session.place_order(category=CATEGORY, symbol=symbol,
                                side="Sell" if s=="Buy" else "Buy",
                                orderType="Market", qty="999999",
                                reduceOnly=True, timeInForce="IOC")
        return jsonify({"ok": True, "action": "closed", "symbol": symbol})

    ok,msg = equity_guard()
    if not ok:
        log.warning(msg);  return jsonify({"ok": False, "error": "drawdown_guard", "msg": msg}), 403
    ok,msg = cooldown_guard(symbol)
    if not ok:
        log.info(msg);     return jsonify({"ok": False, "error": "cooldown", "msg": msg}), 429

    try:
        ensure_leverage(symbol, LEVERAGE)

        equity = get_equity_usdt()
        if equity <= 0:
            return jsonify({"ok": False, "error": "no_equity"}), 400

        price = get_last_price(symbol)
        qty_step, min_qty, tick = get_symbol_rules(symbol)

        target_notional = max(equity * RISK_PCT, MIN_NOTIONAL_USDT)
        raw_qty = target_notional / price
        qty_str = round_step(raw_qty, qty_step)
        if Decimal(qty_str) < min_qty:
            qty_str = str(min_qty)

        # TP/SL
        if side == "BUY":
            tp_price = price * (1 + tp_pct) if tp_pct>0 else None
            sl_price = price * (1 - sl_pct) if sl_pct>0 else None
        else:
            tp_price = price * (1 - tp_pct) if tp_pct>0 else None
            sl_price = price * (1 + sl_pct) if sl_pct>0 else None

        if tp_price is not None:
            tp_price = float(Decimal(str(tp_price)).quantize(tick, rounding=ROUND_DOWN))
        if sl_price is not None:
            sl_price = float(Decimal(str(sl_price)).quantize(tick, rounding=ROUND_DOWN))

        # 1) Place order
        resp = place_market_order(symbol, side, qty_str, tp=tp_price, sl=sl_price)
        log.info(f"Order placed: {resp}")

        # 2) Set fixed 0.7% trailing stop (absolute distance)
        set_trailing_stop(symbol, side, price, tick)

        return jsonify({
            "ok": True,
            "symbol": symbol,
            "side": side,
            "qty": qty_str,
            "price": price,
            "tp": tp_price,
            "sl": sl_price,
            "trail_pct": TRAIL_PCT,
            "bybit": resp
        })
    except Exception as e:
        log.exception("Order error")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/", methods=["GET"])
def health():
    eq = get_equity_usdt()
    return jsonify({"ok": True, "equity": eq})

if __name__ == "__main__":
    log.info("Starting webhook…")
    try:
        START_EQUITY = get_equity_usdt()
        log.info(f"Start equity: {START_EQUITY:.2f} USDT")
    except Exception:
        pass
    app.run(host="0.0.0.0", port=PORT)
