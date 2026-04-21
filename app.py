from flask import Flask, request, jsonify
import alpaca_trade_api as tradeapi
import os

app = Flask(__name__)

API_KEY    = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
BASE_URL   = "https://paper-api.alpaca.markets"

api = tradeapi.REST(API_KEY, SECRET_KEY, BASE_URL)

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    side   = data.get("side")
    symbol = data.get("symbol", "BTC/USD")
    qty    = data.get("qty", "0.001")

    try:
        if side == "buy":
            api.submit_order(symbol=symbol, qty=qty, side="buy", type="market", time_in_force="gtc")
        elif side == "sell":
            api.submit_order(symbol=symbol, qty=qty, side="sell", type="market", time_in_force="gtc")
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
