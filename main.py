import os
import time
import threading
from datetime import datetime, timedelta

import requests
from flask import Flask, jsonify, Response

# =========================
# CONFIG
# =========================
SYMBOL = "LTCUSDT"
NO = 3  # mismo número de velas para calcular res/sup

# IFTTT opcional
IFTTT_EVENT = os.getenv("IFTTT_EVENT", "")
IFTTT_KEY = os.getenv("IFTTT_KEY", "")
IFTTT_URL = (
    f"https://maker.ifttt.com/trigger/{IFTTT_EVENT}/with/key/{IFTTT_KEY}"
    if IFTTT_EVENT and IFTTT_KEY
    else None
)

# Telegram
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Estado global (para el panel)
state = {
    "bot_started_at": None,
    "last_price_time": None,
    "last_price": None,
    "last_signal_time": None,
    "last_signal_type": None,
    "last_signal_price": None,
    "next_poll_at": None,
    "last_error": None,
}

app = Flask(__name__)


# =========================
# HELPERS
# =========================
def iso_utc(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat() + "Z"


def send_ifttt(title, price):
    if not IFTTT_URL:
        return
    try:
        requests.post(IFTTT_URL, json={"value1": title, "value2": SYMBOL, "value3": str(price)}, timeout=5)
    except Exception:
        pass


def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TG NO CONFIGURADO]", msg)
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=5,
        )
        print("TG →", r.status_code, msg)
    except Exception as e:
        print("Error enviando a Telegram:", e)


def get_klines(symbol: str, interval: str, limit: int = 500):
    url = "https://fapi.binance.com/fapi/v1/klines"
    r = requests.get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=5)
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


def get_binance_time_ms():
    r = requests.get("https://fapi.binance.com/fapi/v1/time", timeout=5)
    r.raise_for_status()
    return r.json()["serverTime"]  # ms


def seconds_until_next_minute_from_ms(server_ms: int) -> float:
    server_s = server_ms / 1000.0
    sec_in_min = server_s % 60
    return 60 - sec_in_min + 0.5  # colchón


# =========================
# LÓGICA DE UNA TEMPORALIDAD
# =========================
def process_new_candle(
    timeframe_label: str,
    closes: list,
    highs: list,
    lows: list,
    last_close_time: int,
    avn_last: int,
    prev_close: float | None,
    prev_tsl: float | None,
):
    """
    Aplica la misma lógica que en tu Pine:
    - calcula res/sup de las últimas NO velas
    - detecta dirección
    - arma tsl
    - detecta cruce
    Devuelve: (nuevo_last_close_time, nuevo_avn_last, nuevo_prev_close, nuevo_prev_tsl)
    """
    i = len(closes) - 1

    # rango de las últimas NO velas
    start = max(0, i - NO + 1)
    res = max(highs[start : i + 1])
    sup = min(lows[start : i + 1])

    # valores previos para ver si cambia la dirección
    if i - 1 >= 0:
        prev_start = max(0, i - 1 - NO + 1)
        prev_res = max(highs[prev_start:i])
        prev_sup = min(lows[prev_start:i])
    else:
        prev_res = None
        prev_sup = None

    c = closes[i]
    avd = 0
    if prev_res is not None and c > prev_res:
        avd = 1
    elif prev_sup is not None and c < prev_sup:
        avd = -1

    if avd != 0:
        avn_last = avd

    # tsl según dirección
    tsl = sup if avn_last == 1 else res

    # detectar cruce
    if prev_tsl is not None and prev_close is not None:
        buy = (prev_close <= prev_tsl) and (c > tsl)
        sell = (prev_close >= prev_tsl) and (c < tsl)
    else:
        buy = False
        sell = False

    # si hay señal, avisamos
    if buy:
        print(f"🔥 BUY SIGNAL {timeframe_label}")
        send_telegram(f"🟢 BUY {SYMBOL} {timeframe_label} @ {c}")
        send_ifttt(f"Buy {timeframe_label}", c)
        # actualizamos estado global del panel con la última señal (la más reciente)
        state["last_signal_time"] = iso_utc(datetime.utcnow())
        state["last_signal_type"] = f"buy {timeframe_label}"
        state["last_signal_price"] = c

    if sell:
        print(f"📉 SELL SIGNAL {timeframe_label}")
        send_telegram(f"🔴 SELL {SYMBOL} {timeframe_label} @ {c}")
        send_ifttt(f"Sell {timeframe_label}", c)
        state["last_signal_time"] = iso_utc(datetime.utcnow())
        state["last_signal_type"] = f"sell {timeframe_label}"
        state["last_signal_price"] = c

    # preparar para la próxima vela
    prev_close = c
    prev_tsl = tsl
    last_close_time = int(datetime.utcnow().timestamp() * 1000)

    return last_close_time, avn_last, prev_close, prev_tsl


# =========================
# LOOP PRINCIPAL
# =========================
def bot_loop():
    print("Iniciando bot multi-timeframe...")
    state["bot_started_at"] = iso_utc(datetime.utcnow())

    # --- 1m setup ---
    candles_1m = get_klines(SYMBOL, "1m", 500)
    closes_1m = [c["close"] for c in candles_1m]
    highs_1m = [c["high"] for c in candles_1m]
    lows_1m = [c["low"] for c in candles_1m]
    last_close_time_1m = candles_1m[-1]["close_time"]
    avn_last_1m = 0
    prev_close_1m = closes_1m[-1]
    prev_tsl_1m = None

    # --- 15m setup ---
    candles_15m = get_klines(SYMBOL, "15m", 200)
    closes_15m = [c["close"] for c in candles_15m]
    highs_15m = [c["high"] for c in candles_15m]
    lows_15m = [c["low"] for c in candles_15m]
    last_close_time_15m = candles_15m[-1]["close_time"]
    avn_last_15m = 0
    prev_close_15m = closes_15m[-1]
    prev_tsl_15m = None

    while True:
        try:
            # 1) sincronizar con Binance
            server_ms = get_binance_time_ms()
            sleep_secs = seconds_until_next_minute_from_ms(server_ms)
            state["next_poll_at"] = iso_utc(datetime.utcnow() + timedelta(seconds=sleep_secs))

            # 2) traer última vela 1m
            latest_1m = get_klines(SYMBOL, "1m", 2)
            last_1m = latest_1m[-1]
            state["last_price"] = last_1m["close"]
            state["last_price_time"] = iso_utc(datetime.utcnow())

            # ¿cerró vela nueva de 1m?
            if last_1m["close_time"] != last_close_time_1m:
                # agregamos a histórico
                closes_1m.append(last_1m["close"])
                highs_1m.append(last_1m["high"])
                lows_1m.append(last_1m["low"])
                last_close_time_1m = last_1m["close_time"]

                # procesar lógica 1m
                (
                    last_close_time_1m,
                    avn_last_1m,
                    prev_close_1m,
                    prev_tsl_1m,
                ) = process_new_candle(
                    "1m",
                    closes_1m,
                    highs_1m,
                    lows_1m,
                    last_close_time_1m,
                    avn_last_1m,
                    prev_close_1m,
                    prev_tsl_1m,
                )

            # 3) ¿toca revisar 15m?
            # minuto actual del server
            server_minute = int((server_ms / 1000.0) / 60)  # minuto absoluto
            # si es múltiplo de 15, chequeamos 15m
            if (server_minute % 15) == 0:
                latest_15m = get_klines(SYMBOL, "15m", 2)
                last_15m = latest_15m[-1]
                if last_15m["close_time"] != last_close_time_15m:
                    closes_15m.append(last_15m["close"])
                    highs_15m.append(last_15m["high"])
                    lows_15m.append(last_15m["low"])
                    last_close_time_15m = last_15m["close_time"]

                    (
                        last_close_time_15m,
                        avn_last_15m,
                        prev_close_15m,
                        prev_tsl_15m,
                    ) = process_new_candle(
                        "15m",
                        closes_15m,
                        highs_15m,
                        lows_15m,
                        last_close_time_15m,
                        avn_last_15m,
                        prev_close_15m,
                        prev_tsl_15m,
                    )

            # dormir hasta el próximo minuto
            time.sleep(sleep_secs)

        except Exception as e:
            print("Error en loop:", e)
            state["last_error"] = str(e)
            time.sleep(5)


# =========================
# FLASK (igual que antes)
# =========================
@app.route("/")
def dashboard():
    # aquí puedes dejar el mismo HTML que ya tienes
    return jsonify({"msg": "usa /status"})


@app.route("/status")
def status_route():
    return jsonify(state)


def start_bot_thread():
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()


if __name__ == "__main__":
    start_bot_thread()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
