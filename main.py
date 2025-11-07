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
INTERVAL = "1m"
NO = 3
POLL_INTERVAL = 30  # cada cuánto el bot vuelve a mirar Binance

IFTTT_EVENT = os.getenv("IFTTT_EVENT", "")
IFTTT_KEY = os.getenv("IFTTT_KEY", "")
IFTTT_URL = (
    f"https://maker.ifttt.com/trigger/{IFTTT_EVENT}/with/key/{IFTTT_KEY}"
    if IFTTT_EVENT and IFTTT_KEY
    else None
)

# estado compartido que el panel va a mostrar
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


# =========================
# HELPERS
# =========================
def send_ifttt(title, price):
    if not IFTTT_URL:
        print(f"[IFTTT NO CONFIGURADO] {title} @ {price}")
        return
    try:
        r = requests.post(
            IFTTT_URL,
            json={"value1": title, "value2": SYMBOL, "value3": str(price)},
            timeout=5,
        )
        print("IFTTT →", r.status_code, title, price)
    except Exception as e:
        print("Error enviando a IFTTT:", e)


def get_klines(limit=500):
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


def wait_next_minute_from_binance():
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/time", timeout=5)
        r.raise_for_status()
        server_ms = r.json()["serverTime"]
        server_s = server_ms / 1000.0
        sec_in_min = server_s % 60
        to_sleep = 60 - sec_in_min + 1
        print(f"[SYNC] durmiendo {to_sleep:.1f}s para alinear con Binance…")
        time.sleep(to_sleep)
    except Exception as e:
        print("No se pudo sincronizar con Binance:", e)
        time.sleep(5)


# =========================
# BOT LOOP
# =========================
def bot_loop():
    print("=" * 60)
    print("Binance LTCUSDT 1m — Panel Flask")
    print("Hora inicio:", datetime.utcnow(), "UTC")
    print("=" * 60)

    state["bot_started_at"] = datetime.utcnow().isoformat()

    wait_next_minute_from_binance()

    candles = get_klines()
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]

    avn_last = 0
    tsl_list = []
    last_close_time = candles[-1]["close_time"]

    # inicializar último precio
    state["last_price"] = closes[-1]
    state["last_price_time"] = datetime.utcnow().isoformat()
    state["next_poll_at"] = (datetime.utcnow() + timedelta(seconds=POLL_INTERVAL)).isoformat()

    while True:
        try:
            latest = get_klines(limit=2)
            last = latest[-1]

            # actualizar “último precio”
            state["last_price"] = last["close"]
            state["last_price_time"] = datetime.utcnow().isoformat()

            # ¿cerró vela nueva?
            if last["close_time"] != last_close_time:
                print(f"[{datetime.utcnow().strftime('%H:%M:%S')}] ✅ vela nueva")
                closes.append(last["close"])
                highs.append(last["high"])
                lows.append(last["low"])
                last_close_time = last["close_time"]

                i = len(closes) - 1
                start = max(0, i - NO + 1)
                res = max(highs[start : i + 1])
                sup = min(lows[start : i + 1])

                prev_res = (
                    max(highs[max(0, i - 1 - NO + 1) : i]) if i - 1 >= 0 else None
                )
                prev_sup = (
                    min(lows[max(0, i - 1 - NO + 1) : i]) if i - 1 >= 0 else None
                )

                c = closes[i]
                avd = 0
                if prev_res and c > prev_res:
                    avd = 1
                elif prev_sup and c < prev_sup:
                    avd = -1

                if avd != 0:
                    avn_last = avd

                tsl = sup if avn_last == 1 else res
                tsl_list.append(tsl)

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
                        state["last_signal_time"] = datetime.utcnow().isoformat()
                        state["last_signal_type"] = "buy"
                        state["last_signal_price"] = c

                    if sell:
                        print("📉 SELL SIGNAL")
                        send_ifttt("Sell Signal", c)
                        state["last_signal_time"] = datetime.utcnow().isoformat()
                        state["last_signal_type"] = "sell"
                        state["last_signal_price"] = c

            # programar próxima consulta
            next_poll = datetime.utcnow() + timedelta(seconds=POLL_INTERVAL)
            state["next_poll_at"] = next_poll.isoformat()
            time.sleep(POLL_INTERVAL)

        except Exception as e:
            print("Error en loop:", e)
            state["last_error"] = str(e)
            time.sleep(5)


# =========================
# FLASK APP
# =========================
app = Flask(__name__)


@app.route("/")
def dashboard():
    # HTML simple con JS que pide /status cada 5s
    html = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Binance LTCUSDT Bot</title>
  <style>
    body {{ font-family: sans-serif; background: #0f172a; color: #e2e8f0; padding: 20px; }}
    h1 {{ margin-bottom: .5rem; }}
    .section {{ background: rgba(15,23,42,.35); border: 1px solid rgba(148,163,184,.2); border-radius: 12px; padding: 16px; margin-bottom: 16px; }}
    .label {{ font-size: .75rem; text-transform: uppercase; color: #94a3b8; }}
    .value {{ font-size: 1.25rem; }}
    #countdown {{ font-weight: bold; }}
  </style>
</head>
<body>
  <h1>Binance LTCUSDT Bot</h1>
  <p>Panel en vivo desde Render.</p>

  <div class="section">
    <div class="label">Estado</div>
    <div class="value" id="status">Cargando...</div>
    <div class="label">Iniciado en</div>
    <div id="started_at">-</div>
  </div>

  <div class="section">
    <div class="label">Último precio</div>
    <div class="value" id="last_price">-</div>
    <div class="label">Hora precio</div>
    <div id="last_price_time">-</div>
  </div>

  <div class="section">
    <div class="label">Última señal</div>
    <div class="value" id="last_signal_type">-</div>
    <div class="label">Precio señal</div>
    <div id="last_signal_price">-</div>
    <div class="label">Hora señal</div>
    <div id="last_signal_time">-</div>
  </div>

  <div class="section">
    <div class="label">Próxima actualización estimada</div>
    <div id="next_poll_at">-</div>
    <div class="label">Cuenta regresiva</div>
    <div id="countdown">-</div>
  </div>

  <div class="section">
    <div class="label">Último error</div>
    <div id="last_error">-</div>
  </div>

  <!-- 🔹 SCRIPT DE FORMATO DE FECHAS -->
  <script>
    const TZ_OFFSET_MIN = -4 * 60; // UTC-4

    function formatToUTC4(iso) {{
      if (!iso) return '-';
      const d = new Date(iso);
      const utcMs = d.getTime();
      const localMs = utcMs + TZ_OFFSET_MIN * 60 * 1000;
      const ld = new Date(localMs);
      const pad = (n) => String(n).padStart(2, '0');
      return `${{ld.getFullYear()}}-${{pad(ld.getMonth()+1)}}-${{pad(ld.getDate())}} ` +
             `${{pad(ld.getHours())}}:${{pad(ld.getMinutes())}}:${{pad(ld.getSeconds())}} (UTC-4)`;
    }}
  </script>
</script>




<script>
  let nextPollIso = null;

  async function loadStatus() {{
    try {{
      const res = await fetch('/status');
      const data = await res.json();
      document.getElementById('status').innerText = 'OK';
      document.getElementById('started_at').innerText = data.bot_started_at || '-';
      document.getElementById('last_price').innerText = data.last_price !== null ? data.last_price : '-';
      document.getElementById('last_price_time').innerText = data.last_price_time || '-';
      document.getElementById('last_signal_type').innerText = data.last_signal_type || '-';
      document.getElementById('last_signal_price').innerText = data.last_signal_price !== null ? data.last_signal_price : '-';
      document.getElementById('last_signal_time').innerText = data.last_signal_time || '-';
      document.getElementById('next_poll_at').innerText = data.next_poll_at || '-';
      document.getElementById('last_error').innerText = data.last_error || '-';
      nextPollIso = data.next_poll_at;
    }} catch (e) {{
      document.getElementById('status').innerText = 'ERROR';
    }}
  }}

  function tickCountdown() {{
    if (!nextPollIso) return;
    const target = new Date(nextPollIso).getTime();
    const now = Date.now();
    const diff = Math.floor((target - now) / 1000);
    const el = document.getElementById('countdown');
    if (diff >= 0) {{
      el.innerText = diff + ' s';
    }} else {{
      el.innerText = 'actualizando…';
    }}
  }}

  // carga inicial
  loadStatus();
  // refrescar datos cada 5s
  setInterval(loadStatus, 5000);
  // actualizar cuenta regresiva cada 1s
  setInterval(tickCountdown, 1000);
</script>
</body>
</html>
    """
    return Response(html, mimetype="text/html")


@app.route("/status")
def status():
    return jsonify(state), 200


def start_bot_thread():
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()


if __name__ == "__main__":
    start_bot_thread()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
