# detector.py
import requests
import time
from datetime import datetime
from db import init_db, save_pattern

BINANCE_BASE = "https://fapi.binance.com"
SYMBOLS = ["LTCUSDT"]
TIMEFRAMES = ["15m"]      # luego: ["15m", "1h", "4h"]
KLINES_LIMIT = 500


def get_klines(symbol: str, interval: str, limit: int = 500):
    url = f"{BINANCE_BASE}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def detect_pivots(klines, left=3, right=3):
    pivots = []
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]

    for i in range(left, len(klines) - right):
        is_high = all(highs[i] >= highs[i - j] for j in range(1, left + 1)) and \
                  all(highs[i] >= highs[i + j] for j in range(1, right + 1))
        is_low = all(lows[i] <= lows[i - j] for j in range(1, left + 1)) and \
                 all(lows[i] <= lows[i + j] for j in range(1, right + 1))

        if is_high:
            pivots.append({
                "idx": i,
                "type": "high",
                "price": highs[i],
                "time": klines[i][0]
            })
        elif is_low:
            pivots.append({
                "idx": i,
                "type": "low",
                "price": lows[i],
                "time": klines[i][0]
            })
    return pivots


def find_xabcd(pivots):
    patterns = []
    for i in range(len(pivots) - 4):
        x, a, b, c, d = pivots[i:i+5]
        seq = [p["type"] for p in [x, a, b, c, d]]
        is_bull = seq == ["low", "high", "low", "high", "low"]
        is_bear = seq == ["high", "low", "high", "low", "high"]
        if not (is_bull or is_bear):
            continue

        direction = "BULLISH" if is_bull else "BEARISH"
        patterns.append({
            "x": x, "a": a, "b": b, "c": c, "d": d,
            "direction": direction
        })
    return patterns


def validate_harmonic_simple(pat, tol=0.12):
    x = pat["x"]["price"]
    a = pat["a"]["price"]
    b = pat["b"]["price"]
    c = pat["c"]["price"]
    d = pat["d"]["price"]

    xa = abs(a - x)
    ab = abs(b - a)
    bc = abs(c - b)
    cd = abs(d - c)

    if xa == 0 or ab == 0 or bc == 0:
        return False, 0.0, None

    # condiciones tipo Gartley
    ab_xa = ab / xa
    cond1 = abs(ab_xa - 0.618) <= tol

    bc_ab = bc / ab
    cond2 = (0.382 - tol) <= bc_ab <= (0.886 + tol)

    cd_bc = cd / bc
    cond3 = (1.27 - tol) <= cd_bc <= (1.618 + tol)

    is_valid = cond1 and cond2 and cond3
    score = (int(cond1) + int(cond2) + int(cond3)) / 3 * 100.0
    return is_valid, score, "Gartley-ish"


def run_detector(send_fn=None):
    """
    send_fn: función para notificar (por ej. send_telegram)
    """
    init_db()
    print("[detector] DB lista")

    seen = set()

    while True:
        for symbol in SYMBOLS:
            for tf in TIMEFRAMES:
                try:
                    klines = get_klines(symbol, tf, KLINES_LIMIT)
                    pivots = detect_pivots(klines)
                    candidates = find_xabcd(pivots)

                    for cand in candidates:
                        ok, score, pname = validate_harmonic_simple(cand)
                        if not ok:
                            continue

                        d_time = cand["d"]["time"]
                        key = f"{symbol}:{tf}:{d_time}"
                        if key in seen:
                            continue

                        points = {
                            "x": datetime.utcfromtimestamp(cand["x"]["time"]/1000).isoformat(),
                            "a": datetime.utcfromtimestamp(cand["a"]["time"]/1000).isoformat(),
                            "b": datetime.utcfromtimestamp(cand["b"]["time"]/1000).isoformat(),
                            "c": datetime.utcfromtimestamp(cand["c"]["time"]/1000).isoformat(),
                            "d": datetime.utcfromtimestamp(cand["d"]["time"]/1000).isoformat(),
                        }

                        save_pattern(symbol, tf, pname, cand["direction"], score, points)
                        print(f"[detector] patrón {pname} {cand['direction']} {symbol} {tf} score={score:.1f}")

                        # notificación si hay función
                        if send_fn:
                            send_fn(f"📐 Patrón armónico {pname} {cand['direction']} en {symbol} TF={tf} score={score:.1f}")

                        seen.add(key)

                except Exception as e:
                    print("[detector] error:", e)

        time.sleep(30)


if __name__ == "__main__":
    # si lo ejecutas solo: no manda telegram
    run_detector()
