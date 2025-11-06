import requests
from flask import Flask, jsonify

app = Flask(__name__)

BINANCE_URL = "https://fapi.binance.com/fapi/v1/klines"
TEST_PARAMS = {
    "symbol": "LTCUSDT",
    "interval": "1m",
    "limit": 2
}

@app.route("/")
def root():
    return "Binance render test OK", 200

@app.route("/test-binance")
def test_binance():
    try:
        r = requests.get(BINANCE_URL, params=TEST_PARAMS, timeout=5)
        return jsonify({
            "ok": r.status_code == 200,
            "status_code": r.status_code,
            "len": len(r.json()) if r.status_code == 200 else None
        }), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == "__main__":
    # Render usa la variable PORT
    import os
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
