import os
import time
import threading
from datetime import datetime

import requests
from flask import Flask, jsonify

# =========================
# CONFIG DEL BOT
# =========================
SYMBOL = "LTCUSDT"
INTERVAL = "1m"
NO = 3               # nº de velas para soporte/resistencia
POLL_INTERVAL = 5    # segundos entre consultas a Binance

# IFTTT opcional
IFTTT_EVENT = os.getenv("IFTTT_EVENT", "")
IFTTT_KEY = os.getenv("IFTTT_KEY", "")
IFTTT_URL = (
    f"https://maker.ifttt.com/trigger/{IFTTT_EVENT}/with/key/{IFTTT_KEY}"
    if IFTTT_EVENT and IFTTT_KEY
    else None
)

# estado para exponer por HTTP
last_signal = {
    "time": None,
    "type": None,
    "price": None,
}


def send_ifttt(title, price):
    msg = f"{title} en {SYMBOL} @ {price}"
    if not IFTTT_URL:
        print(f"[IFTTT NO CONFIGURADO] {msg}")
        return
    try:
        r = requests.post(
            IFTTT_URL,
            json={"value1": title, "value2": SYMBOL, "value3": str(price)},
            timeout=5,
        )
        print("IFTTT →", r.status_code, msg)
    except Exception as e:
        print("Error enviando a IFTTT:", e)


def get_klines(limit=500):
    """Trae velas de Binance Futures."""
    url = "https://fapi.binance.com/fapi/v1/klines"
    r = requests.get(
        url,
        params={"symbol": SYMBOL, "interval": INTERVAL, "limit": limit},
        timeout=5,
    )
    r.raise_for_status()
    data = r.json()
    return [
        {
            "open_time": c[0],
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "close_time": c[6],
        }
        for c in data
    ]


def bot_loop():
    """Loop del bot corriendo en segundo plano."""
    print("=" * 60)
    print("Binance LTCUSDT 1m — BUY/SELL Swing (bot activo)")
    print("Hora:", datetime.utcnow(), "UTC")
    print("IFTTT configurado:", bool(IFTTT_URL))
    print("=" * 60)

    # histórico inicial
    candles = get_klines()
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]

    avn_last = 0
    tsl_list = []
    last_close_time = candles[-1]["close_time"]

    while True:
        try:
            latest = get_klines(limit=2)
            last = latest[-1]

            # ¿cerró vela nueva?
            if last["close_time"] != last_close_time:
                closes.append(last["close"])
                highs.append(last["high"])
                lows.append(last["low"])
                last_close_time = last["close_time"]

                i = len(closes) - 1
                start = max(0, i - NO + 1)

                # niveles actuales
                res = max(highs[start : i + 1])
                sup = min(lows[start : i + 1])

                # niveles previos para detectar rompimiento
                prev_res = (
                    max(highs[max(0, i - 1 - NO + 1) : i]) if i - 1 >= 0 else None
                )
                prev_sup = (
                    min(lows[max(0, i - 1 - NO + 1) : i]) if i - 1 >= 0 else None
                )

                c = closes[i]
                avd = 0  # dirección
                if prev_res and c > prev_res:
                    avd = 1
                elif prev_sup and c < prev_sup:
                    avd = -1

                if avd != 0:
                    avn_last = avd

                # trailing stop lógico
                tsl = sup if avn_last == 1 else res
                tsl_list.append(tsl)

                # detectar señales
                if i >= 1:
                    prev_close = closes[i - 1]
                    prev_tsl = tsl_list[i - 1]

                    buy = (prev_close <= prev_tsl) and (c > tsl)
                    sell = (prev_close >= prev_tsl) and (c < tsl)

                    print(
                        f"[{datetime.utcnow().strftime('%H:%M:%S')}] close={c} tsl={tsl} buy={buy} sell={sell}"
                    )

                    if buy:
                        print("🔥 BUY SIGNAL")
                        send_ifttt("Buy Signal", c)
                        last_signal["time"] = datetime.utcnow().isoformat()
                        last_signal["type"] = "buy"
                        last_signal["price"] = c

                    if sell:
                        print("📉 SELL SIGNAL")
                        send_ifttt("Sell Signal", c)
                        last_signal["time"] = datetime.utcnow().isoformat()
                        last_signal["type"] = "sell"
                        last_signal["price"] = c

            time.sleep(POLL_INTERVAL)
        except Exception as e:
            print("Error en loop:", e)
            time.sleep(5)


# =========================
# FLASK para Render
# =========================
app = Flask(__name__)


@app.route("/")
def root():
    return "Binance bot + Flask OK", 200


@app.route("/health")
def health():
    return {"status": "ok", "symbol": SYMBOL}, 200


@app.route("/last-signal")
def last_signal_route():
    return jsonify(last_signal), 200


def start_bot_thread():
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()


if __name__ == "__main__":
    # lanzar el bot en segundo plano
    start_bot_thread()
    # levantar servidor web
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
