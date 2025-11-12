# detector.py
import requests
import time
from datetime import datetime, timezone
from db import init_db, save_pattern

# =========================================================
# CONFIG
# =========================================================
BINANCE_BASE = "https://fapi.binance.com"
SYMBOLS = ["LTCUSDT"]
KLINES_LIMIT = 500

# Umbral y tolerancia más estrictos
MIN_SCORE = 70.0
DEFAULT_TOLERANCE = 0.08  # 8%

# Plantillas con chequeo extra por AD/XA o AD/XA extensión
HARMONIC_TEMPLATES = [
    # Gartley: D ≈ 0.786 de XA
    {
        "name": "Gartley",
        "ab_xa": (0.618, 0.618),
        "bc_ab": (0.382, 0.886),
        "cd_bc": (1.27, 1.618),
        "ad_xa": (0.76, 0.82),   # ventana alrededor de 0.786 (ajustable)
    },
    # Bat: D ≈ 0.886 de XA
    {
        "name": "Bat",
        "ab_xa": (0.382, 0.50),
        "bc_ab": (0.382, 0.886),
        "cd_bc": (1.618, 2.618),
        "ad_xa": (0.86, 0.91),   # ventana alrededor de 0.886
    },
    # Butterfly: D = extensión 1.27–1.618 de XA
    {
        "name": "Butterfly",
        "ab_xa": (0.786, 0.786),
        "bc_ab": (0.382, 0.886),
        "cd_bc": (1.618, 2.24),
        "ad_xa_ext": (1.27, 1.618),
    },
    # Crab: D = extensión ~1.618 de XA (rango amplio)
    {
        "name": "Crab",
        "ab_xa": (0.382, 0.618),
        "bc_ab": (0.382, 0.886),
        "cd_bc": (2.24, 3.618),
        "ad_xa_ext": (1.55, 1.75),
    },
    # Cypher → la añadimos bien en el siguiente paso (usa AD/XC y C extensión de XA)
]



# =========================================================
# HELPERS DE RED Y VELAS
# =========================================================
def get_klines(symbol: str, interval: str, limit: int = 500):
    url = f"{BINANCE_BASE}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    out = []
    for k in data:
        out.append(
            {
                "open_time": k[0],
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
            }
        )
    return out


# =========================================================
# DETECCIÓN DE PIVOTS Y CANDIDATOS
# =========================================================
def find_pivots(candles, left=2, right=2):
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    pivots = []

    for i in range(left, len(candles) - right):
        is_high = all(highs[i] >= highs[i - j] for j in range(1, left + 1)) and \
                  all(highs[i] > highs[i + j] for j in range(1, right + 1))
        is_low = all(lows[i] <= lows[i - j] for j in range(1, left + 1)) and \
                 all(lows[i] < lows[i + j] for j in range(1, right + 1))

        if is_high:
            pivots.append({
                "index": i,
                "price": highs[i],
                "time": candles[i]["open_time"],
                "type": "high",
            })
        if is_low:
            pivots.append({
                "index": i,
                "price": lows[i],
                "time": candles[i]["open_time"],
                "type": "low",
            })

    pivots.sort(key=lambda x: x["index"])
    return pivots


def build_candidates(pivots):
    """
    arma secuencias XABCD con alternancia clara
    low-high-low-high-low  -> bullish
    high-low-high-low-high -> bearish
    """
    out = []
    n = len(pivots)
    for i in range(n - 4):
        x, a, b, c, d = pivots[i:i + 5]
        seq = [p["type"] for p in (x, a, b, c, d)]
        is_bull = seq == ["low", "high", "low", "high", "low"]
        is_bear = seq == ["high", "low", "high", "low", "high"]
        if not (is_bull or is_bear):
            continue
        direction = "BULLISH" if is_bull else "BEARISH"
        out.append({
            "x": x,
            "a": a,
            "b": b,
            "c": c,
            "d": d,
            "direction": direction,
        })
    return out


# =========================================================
# SCORING CONTRA PLANTILLAS
# =========================================================
def _ratio(val, ref):
    return abs(val / ref) if ref != 0 else None


def score_ratio(actual, expected_min, expected_max, tolerance=DEFAULT_TOLERANCE):
    if actual is None:
        return 0.0

    if expected_min <= actual <= expected_max:
        return 1.0

    lower = expected_min * (1 - tolerance)
    upper = expected_max * (1 + tolerance)

    if lower <= actual <= upper:
        if actual < expected_min:
            return 1 - (expected_min - actual) / (expected_min - lower)
        else:
            return 1 - (actual - expected_max) / (upper - expected_max)

    return 0.0


def validate_against_templates(cand):
    x = cand["x"]["price"]
    a = cand["a"]["price"]
    b = cand["b"]["price"]
    c = cand["c"]["price"]
    d = cand["d"]["price"]

    xa = abs(a - x)
    ab = abs(b - a)
    bc = abs(c - b)
    cd = abs(d - c)
    ad = abs(d - a)

    if xa == 0 or ab == 0 or bc == 0:
        return False, 0.0, None

    best_ok = False
    best_score = 0.0
    best_name = None

    for tpl in HARMONIC_TEMPLATES:
        r_ab_xa = _ratio(ab, xa)
        r_bc_ab = _ratio(bc, ab)
        r_cd_bc = _ratio(cd, bc)
        r_ad_xa = _ratio(ad, xa)

        # pesos: retracciones 60–70%, AD/XA 30–40%
        s1 = score_ratio(r_ab_xa, *tpl["ab_xa"])
        s2 = score_ratio(r_bc_ab, *tpl["bc_ab"])
        s3 = score_ratio(r_cd_bc, *tpl["cd_bc"])

        # chequeo extra por AD/XA o extensión
        if "ad_xa" in tpl:
            s4 = score_ratio(r_ad_xa, *tpl["ad_xa"])
        elif "ad_xa_ext" in tpl:
            s4 = score_ratio(r_ad_xa, *tpl["ad_xa_ext"])
        else:
            s4 = 0.0

        # ponderación: (AB/XA, BC/AB, CD/BC, AD/XA)
        score = (s1 * 0.28) + (s2 * 0.24) + (s3 * 0.28) + (s4 * 0.20)
        score_pct = score * 100.0

        if score_pct > best_score:
            best_score = score_pct
            best_name = tpl["name"]
            best_ok = score_pct >= MIN_SCORE

    return best_ok, best_score, best_name



# =========================================================
# DETECCIÓN POR TF (llamado solo cuando toca)
# =========================================================
def detect_for_tf(symbol: str, tf: str, send_fn, seen: set):
    try:
        klines = get_klines(symbol, tf, KLINES_LIMIT)
        pivots = find_pivots(klines, left=2, right=2)
        cands = build_candidates(pivots)

        # 1) evaluamos todos
        evaluated = []
        for cand in cands:
            ok, score, pname = validate_against_templates(cand)
            if not ok:
                continue
            d_time = cand["d"]["time"]
            evaluated.append((d_time, score, pname, cand))

        if not evaluated:
            return

        # 2) agrupamos por "bucket" de D (misma vela/minuto)
        buckets = {}
        for d_time, score, pname, cand in evaluated:
            key = f"{symbol}:{tf}:{d_time//60000}"   # minuto de la D
            cur = buckets.get(key)
            if (cur is None) or (score > cur["score"]):
                buckets[key] = {"score": score, "pname": pname, "cand": cand}

        # 3) enviamos solo el mejor de cada bucket (y deduplicamos de verdad)
        for key, item in buckets.items():
            cand = item["cand"]
            score = item["score"]
            pname = item["pname"]
            d_time = cand["d"]["time"]
            direction = cand["direction"]

            dedup_key = f"{symbol}:{tf}:{d_time}:{pname}:{direction}"
            if dedup_key in seen:
                continue

            points = {
                "x": datetime.utcfromtimestamp(cand["x"]["time"] / 1000).isoformat(),
                "a": datetime.utcfromtimestamp(cand["a"]["time"] / 1000).isoformat(),
                "b": datetime.utcfromtimestamp(cand["b"]["time"] / 1000).isoformat(),
                "c": datetime.utcfromtimestamp(cand["c"]["time"] / 1000).isoformat(),
                "d": datetime.utcfromtimestamp(cand["d"]["time"] / 1000).isoformat(),
            }

            save_pattern(symbol, tf, pname, direction, score, points)
            msg = f"📐 Patrón armónico {pname} {direction} en {symbol} TF={tf} score={score:.1f}"
            print(f"[detector] {msg}")
            if send_fn:
                send_fn(msg)
            seen.add(dedup_key)

    except Exception as e:
        print(f"[detector] error en {tf}: {e}")


# =========================================================
# LOOP PRINCIPAL CON TUS HORARIOS
# =========================================================
def run_detector(send_fn=None):
    print("[detector] iniciando detector armónico (horario 3x15m y 3x1h)")
    init_db()
    seen = set()

    # para no ejecutar dos veces el mismo disparo
    last_run_15m = None
    last_run_1h = None

    while True:
        now = datetime.utcnow().replace(tzinfo=timezone.utc)
        m = now.minute
        s = now.second
        h = now.hour
        y = now.year
        mo = now.month
        d = now.day

        # ---------- 15m ----------
        # velas: 00, 15, 30, 45
        # queremos 3 disparos en cada bloque de 15m: 4:30, 9:30, 14:30
        run_15m = False
        slot_15m = None

        mod15 = m % 15
        if mod15 == 4 and 28 <= s <= 32:
            slot_15m = f"{y}-{mo}-{d}-{h}-{m}-s1"
            run_15m = True
        elif mod15 == 9 and 28 <= s <= 32:
            slot_15m = f"{y}-{mo}-{d}-{h}-{m}-s2"
            run_15m = True
        elif mod15 == 14 and 28 <= s <= 32:
            slot_15m = f"{y}-{mo}-{d}-{h}-{m}-s3"
            run_15m = True

        if run_15m and last_run_15m != slot_15m:
            # ejecutar para todos los símbolos en 15m
            for sym in SYMBOLS:
                detect_for_tf(sym, "15m", send_fn, seen)
            last_run_15m = slot_15m

        # ---------- 1h ----------
        # 3 disparos: 20:30, 40:30, 59:30
        run_1h = False
        slot_1h = None

        if m == 20 and 28 <= s <= 32:
            slot_1h = f"{y}-{mo}-{d}-{h}-20-s1"
            run_1h = True
        elif m == 40 and 28 <= s <= 32:
            slot_1h = f"{y}-{mo}-{d}-{h}-40-s2"
            run_1h = True
        elif m == 59 and 28 <= s <= 32:
            slot_1h = f"{y}-{mo}-{d}-{h}-59-s3"
            run_1h = True

        if run_1h and last_run_1h != slot_1h:
            for sym in SYMBOLS:
                detect_for_tf(sym, "1h", send_fn, seen)
            last_run_1h = slot_1h

        # loop ligero
        time.sleep(1)


if __name__ == "__main__":
    run_detector()
