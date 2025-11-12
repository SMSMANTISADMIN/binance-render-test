import os
import time
import threading
from datetime import datetime, timedelta
import requests
from flask import Flask, jsonify, Response, request
from db import list_patterns, stats  # para el frontend
from detector import run_detector    # para arrancar el detector en un thread


# ======================================================
# CONFIG
# ======================================================
SYMBOL = "LTCUSDT"
NO = 3  # número de velas para calcular soporte/resistencia

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

# Estado global (para panel Flask)
state = {
    "bot_started_at": None,

    "last_price_time": None,
    "last_price": None,

    # último cualquiera (1m o 15m)
    "last_signal_time": None,
    "last_signal_type": None,
    "last_signal_price": None,

    # último 1m
    "last_signal_1m_time": None,
    "last_signal_1m_type": None,
    "last_signal_1m_price": None,

    # último 15m
    "last_signal_15m_time": None,
    "last_signal_15m_type": None,
    "last_signal_15m_price": None,

    "next_poll_at": None,
    "last_error": None,

    # NUEVO: switches
    "alerts_1m_enabled": True,
    "alerts_15m_enabled": True,
}

app = Flask(__name__)

# ======================================================
# HELPERS
# ======================================================
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
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=5)
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
    return r.json()["serverTime"]


def seconds_until_next_minute_from_ms(server_ms: int) -> float:
    server_s = server_ms / 1000.0
    sec_in_min = server_s % 60
    return 60 - sec_in_min + 0.5


# ======================================================
# LÓGICA DE UNA TEMPORALIDAD
# ======================================================
def process_new_candle(
    timeframe_label: str,
    closes: list,
    highs: list,
    lows: list,
    last_close_time: int,
    avn_last: int,
    prev_close: float | None,
    prev_tsl: float | None,
    alerts_enabled: bool,  # 👈 NUEVO
):
    """
    timeframe_label: "1m" o "15m"
    """
    i = len(closes) - 1
    start = max(0, i - NO + 1)
    res = max(highs[start : i + 1])
    sup = min(lows[start : i + 1])

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

    tsl = sup if avn_last == 1 else res

    if prev_tsl is not None and prev_close is not None:
        buy = (prev_close <= prev_tsl) and (c > tsl)
        sell = (prev_close >= prev_tsl) and (c < tsl)
    else:
        buy = False
        sell = False

    now_iso = iso_utc(datetime.utcnow())

    # solo si las alarmas de este TF están activas
    if alerts_enabled:
        if buy:
            print(f"🔥 BUY SIGNAL {timeframe_label}")
            send_telegram(f"🟢 BUY {SYMBOL} {timeframe_label} @ {c}")
            send_ifttt(f"Buy {timeframe_label}", c)

            # general
            state["last_signal_time"] = now_iso
            state["last_signal_type"] = f"buy {timeframe_label}"
            state["last_signal_price"] = c

            # específico por timeframe
            if timeframe_label == "1m":
                state["last_signal_1m_time"] = now_iso
                state["last_signal_1m_type"] = "buy"
                state["last_signal_1m_price"] = c
            else:
                state["last_signal_15m_time"] = now_iso
                state["last_signal_15m_type"] = "buy"
                state["last_signal_15m_price"] = c

        if sell:
            print(f"📉 SELL SIGNAL {timeframe_label}")
            send_telegram(f"🔴 SELL {SYMBOL} {timeframe_label} @ {c}")
            send_ifttt(f"Sell {timeframe_label}", c)

            state["last_signal_time"] = now_iso
            state["last_signal_type"] = f"sell {timeframe_label}"
            state["last_signal_price"] = c

            if timeframe_label == "1m":
                state["last_signal_1m_time"] = now_iso
                state["last_signal_1m_type"] = "sell"
                state["last_signal_1m_price"] = c
            else:
                state["last_signal_15m_time"] = now_iso
                state["last_signal_15m_type"] = "sell"
                state["last_signal_15m_price"] = c

    # aunque las alarmas estén apagadas, actualizamos prev_* para que la lógica no se rompa
    prev_close = c
    prev_tsl = tsl
    last_close_time = int(datetime.utcnow().timestamp() * 1000)

    return last_close_time, avn_last, prev_close, prev_tsl


# ======================================================
# LOOP PRINCIPAL
# ======================================================
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
            server_ms = get_binance_time_ms()
            sleep_secs = seconds_until_next_minute_from_ms(server_ms)
            state["next_poll_at"] = iso_utc(datetime.utcnow() + timedelta(seconds=sleep_secs))

            # ===== 1 MINUTO =====
            latest_1m = get_klines(SYMBOL, "1m", 2)
            last_1m = latest_1m[-1]
            state["last_price"] = last_1m["close"]
            state["last_price_time"] = iso_utc(datetime.utcnow())

            if last_1m["close_time"] != last_close_time_1m:
                closes_1m.append(last_1m["close"])
                highs_1m.append(last_1m["high"])
                lows_1m.append(last_1m["low"])
                last_close_time_1m = last_1m["close_time"]

                last_close_time_1m, avn_last_1m, prev_close_1m, prev_tsl_1m = process_new_candle(
                    "1m",
                    closes_1m,
                    highs_1m,
                    lows_1m,
                    last_close_time_1m,
                    avn_last_1m,
                    prev_close_1m,
                    prev_tsl_1m,
                    alerts_enabled=state["alerts_1m_enabled"],
                )

            # ===== 15 MINUTOS =====
            server_minute = int((server_ms / 1000.0) / 60)
            if (server_minute % 15) == 0:
                latest_15m = get_klines(SYMBOL, "15m", 2)
                last_15m = latest_15m[-1]
                if last_15m["close_time"] != last_close_time_15m:
                    closes_15m.append(last_15m["close"])
                    highs_15m.append(last_15m["high"])
                    lows_15m.append(last_15m["low"])
                    last_close_time_15m = last_15m["close_time"]

                    last_close_time_15m, avn_last_15m, prev_close_15m, prev_tsl_15m = process_new_candle(
                        "15m",
                        closes_15m,
                        highs_15m,
                        lows_15m,
                        last_close_time_15m,
                        avn_last_15m,
                        prev_close_15m,
                        prev_tsl_15m,
                        alerts_enabled=state["alerts_15m_enabled"],
                    )

            time.sleep(sleep_secs)

        except Exception as e:
            print("Error en loop:", e)
            state["last_error"] = str(e)
            time.sleep(5)


# ======================================================
# FLASK PANEL
# ======================================================
@app.route("/")
def dashboard():
    html = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Binance LTCUSDT Bot</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root {
      --bg: #0f172a;
      --card: rgba(15,23,42,0.35);
      --border: rgba(148,163,184,0.25);
      --text: #f1f5f9;
      --muted: #cbd5e1;
      --radius: 16px;
      --gap: 1rem;
    }
    * { box-sizing: border-box; }
    body {
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      margin: 0;
      padding: 0;
      font-size: 16px;
    }
    .page {
      max-width: 1200px;
      margin: 0 auto;
      padding: 1rem;
      display: flex;
      flex-direction: column;
      gap: 1rem;
    }
    header h1 { font-size: 1.4rem; margin: 0; }
    header p { margin: .25rem 0 0; color: var(--muted); font-size: .85rem; }

    .grid { display: grid; gap: var(--gap); }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 1rem;
      min-height: 80px;
    }
    .label {
      font-size: .8rem;
      color: var(--muted);
      margin-bottom: .25rem;
    }
    .value { font-size: 1.35rem; font-weight: 600; }
    .controls { display: flex; flex-wrap: wrap; gap: .5rem; margin-top: .5rem; }
    button {
      font-size: .9rem;
      padding: .5rem .9rem;
      border: none;
      border-radius: 9999px;
      cursor: pointer;
      font-weight: 600;
    }
    .on { background: #22c55e; color: #0f172a; }
    .off { background: #ef4444; color: #fff; }

    /* móvil: mejora legibilidad */
    @media (max-width: 599px) {
      body { font-size: 24px; }
      .card { padding: 1.1rem 1.1rem 1rem; }
      .label { font-size: .9rem; }
      header h1 { font-size: 1.35rem; }
    }

    /* tablet */
    @media (min-width: 600px) {
      .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .span-2 { grid-column: span 2; }
    }
    /* escritorio grande */
    @media (min-width: 980px) {
      .grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .span-3 { grid-column: span 3; }
    }
  </style>
</head>
<body>
  <div class="page">
    <header class="card span-3">
      <h1>Binance LTCUSDT Bot</h1>
      <p>Panel en vivo desde Render (1m + 15m)</p>
    </header>

    <div class="grid">
      <div class="card">
        <div class="label">Estado</div>
        <div class="value" id="status">Cargando…</div>
        <div class="label" style="margin-top:.5rem;">Iniciado en</div>
        <div id="started_at">-</div>
      </div>

      <div class="card">
        <div class="label">Controles</div>
        <div class="controls">
          <button id="btn_1m" onclick="toggleAlert('1m')">.</button>
          <button id="btn_15m" onclick="toggleAlert('15m')">.</button>
        </div>
      </div>

      <div class="card">
        <div class="label">Último precio</div>
        <div class="value" id="last_price">-</div>
        <div class="label" style="margin-top:.5rem;">Hora precio</div>
        <div id="last_price_time">-</div>
      </div>

      <div class="card">
        <div class="label">Última señal (cualquiera)</div>
        <div class="value" id="last_signal_type">-</div>
        <div class="label" style="margin-top:.5rem;">Precio señal</div>
        <div id="last_signal_price">-</div>
        <div class="label" style="margin-top:.5rem;">Hora señal</div>
        <div id="last_signal_time">-</div>
      </div>

      <div class="card">
        <div class="label">Última señal 1m</div>
        <div class="value" id="last_signal_1m_type">-</div>
        <div class="label" style="margin-top:.5rem;">Precio 1m</div>
        <div id="last_signal_1m_price">-</div>
        <div class="label" style="margin-top:.5rem;">Hora 1m</div>
        <div id="last_signal_1m_time">-</div>
      </div>

      <div class="card">
        <div class="label">Última señal 15m</div>
        <div class="value" id="last_signal_15m_type">-</div>
        <div class="label" style="margin-top:.5rem;">Precio 15m</div>
        <div id="last_signal_15m_price">-</div>
        <div class="label" style="margin-top:.5rem;">Hora 15m</div>
        <div id="last_signal_15m_time">-</div>
      </div>

      <div class="card">
        <div class="label">Próxima actualización estimada</div>
        <div id="next_poll_at">-</div>
        <div class="label" style="margin-top:.5rem;">Cuenta regresiva</div>
        <div id="countdown">-</div>
      </div>

      <div class="card span-2">
        <div class="label">Último error</div>
        <div id="last_error">-</div>
      </div>
    </div>
  </div>

  <script>
    let alerts1m = true;
    let alerts15m = true;
    let nextPollIso = null;
    const TZ_OFFSET_MIN = -4 * 60; // ya lo tenías así

    function formatToTZ(iso) {
      if (!iso) return "-";
      const d = new Date(iso);
      d.setMinutes(d.getMinutes() + TZ_OFFSET_MIN);
      const base = d.toISOString().replace("T", " ").substring(0, 19);
      const offHr = TZ_OFFSET_MIN / 60;
      return base + " (UTC" + (offHr >= 0 ? "+" + offHr : offHr) + ")";
    }

    async function loadStatus() {
      try {
        const resp = await fetch('/status');
        const data = await resp.json();

        document.getElementById('status').innerText = data.last_error ? 'ERROR' : 'OK';
        document.getElementById('started_at').innerText = formatToTZ(data.bot_started_at);
        document.getElementById('last_price').innerText = data.last_price !== null ? data.last_price : '-';
        document.getElementById('last_price_time').innerText = formatToTZ(data.last_price_time);

        document.getElementById('last_signal_type').innerText = data.last_signal_type || '-';
        document.getElementById('last_signal_price').innerText = data.last_signal_price !== null ? data.last_signal_price : '-';
        document.getElementById('last_signal_time').innerText = formatToTZ(data.last_signal_time);

        document.getElementById('last_signal_1m_type').innerText = data.last_signal_1m_type || '-';
        document.getElementById('last_signal_1m_price').innerText = data.last_signal_1m_price !== null ? data.last_signal_1m_price : '-';
        document.getElementById('last_signal_1m_time').innerText = formatToTZ(data.last_signal_1m_time);

        document.getElementById('last_signal_15m_type').innerText = data.last_signal_15m_type || '-';
        document.getElementById('last_signal_15m_price').innerText = data.last_signal_15m_price !== null ? data.last_signal_15m_price : '-';
        document.getElementById('last_signal_15m_time').innerText = formatToTZ(data.last_signal_15m_time);

        document.getElementById('next_poll_at').innerText = formatToTZ(data.next_poll_at);
        document.getElementById('last_error').innerText = data.last_error || '-';

        alerts1m = data.alerts_1m_enabled;
        alerts15m = data.alerts_15m_enabled;
        paintButtons();

        nextPollIso = data.next_poll_at;
      } catch (e) {
        document.getElementById('status').innerText = 'ERROR';
      }
    }

    function paintButtons() {
      const b1 = document.getElementById('btn_1m');
      const b15 = document.getElementById('btn_15m');
      if (alerts1m) {
        b1.textContent = 'Alarmas 1m: ON';
        b1.className = 'on';
      } else {
        b1.textContent = 'Alarmas 1m: OFF';
        b1.className = 'off';
      }
      if (alerts15m) {
        b15.textContent = 'Alarmas 15m: ON';
        b15.className = 'on';
      } else {
        b15.textContent = 'Alarmas 15m: OFF';
        b15.className = 'off';
      }
    }

    async function toggleAlert(tf) {
      await fetch('/toggle', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ timeframe: tf })
      });
      loadStatus();
    }

    function tickCountdown() {
      if (!nextPollIso) return;
      const target = new Date(nextPollIso).getTime();
      const now = Date.now();
      const diff = Math.floor((target - now) / 1000);
      document.getElementById('countdown').innerText = diff >= 0 ? diff + ' s' : 'actualizando…';
    }

    loadStatus();
    setInterval(loadStatus, 10000);
    setInterval(tickCountdown, 1000);
  </script>
</body>
</html>
    """
    return Response(html, mimetype="text/html")


@app.route("/status")
def status_route():
    return jsonify(state)


@app.route("/toggle", methods=["POST"])
def toggle_route():
    data = request.get_json(silent=True) or {}
    tf = data.get("timeframe")
    if tf == "1m":
        state["alerts_1m_enabled"] = not state["alerts_1m_enabled"]
    elif tf == "15m":
        state["alerts_15m_enabled"] = not state["alerts_15m_enabled"]
    return jsonify({
        "alerts_1m_enabled": state["alerts_1m_enabled"],
        "alerts_15m_enabled": state["alerts_15m_enabled"],
    })

@app.route("/patterns", methods=["GET"])
def patterns_route():
    symbol = request.args.get("symbol")
    tf = request.args.get("timeframe")
    limit = int(request.args.get("limit", 50))
    data = list_patterns(limit=limit, symbol=symbol, timeframe=tf)
    return jsonify({"ok": True, "data": data})


@app.route("/patterns/stats", methods=["GET"])
def patterns_stats_route():
    data = stats()
    return jsonify({"ok": True, "data": data})

def start_bot_thread():
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()

def start_detector_thread():
    # le pasamos la función de telegram del main
    t = threading.Thread(target=run_detector, kwargs={"send_fn": send_telegram}, daemon=True)
    t.start()    


if __name__ == "__main__":
    start_bot_thread()        # tu bot de 1m y 15m
    start_detector_thread()   # nuestro detector armónico
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
