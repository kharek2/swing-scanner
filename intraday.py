import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

import pandas as pd
import yfinance as yf

GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
SEND_TO = os.getenv("SEND_TO", GMAIL_ADDRESS)

STOCKS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR",
    "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK", "LT", "AXISBANK",
    "ASIANPAINT", "MARUTI", "SUNPHARMA", "TITAN", "BAJFINANCE",
    "ULTRACEMCO", "NESTLEIND", "WIPRO", "HCLTECH", "M&M", "NTPC",
    "POWERGRID", "TATAMOTORS", "TATASTEEL", "JSWSTEEL", "ADANIENT",
    "ADANIPORTS", "COALINDIA", "ONGC", "TECHM", "DRREDDY", "CIPLA",
    "EICHERMOT", "HEROMOTOCO", "BAJAJ-AUTO", "GRASIM", "HINDALCO",
    "INDUSINDBK",
]

VOLUME_MULTIPLE = 1.5
STOP_ATR_MULT = 1.0

IST = timezone(timedelta(hours=5, minutes=30))
SENT_FILE = "sent_today.txt"


def market_open_now():
    now = datetime.now(IST)
    if now.weekday() > 4:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 50 <= minutes <= 15 * 60 + 15


def load_sent():
    today = datetime.now(IST).strftime("%Y-%m-%d")
    if not os.path.exists(SENT_FILE):
        return today, set()
    with open(SENT_FILE) as f:
        lines = f.read().split()
    if not lines or lines[0] != today:
        return today, set()
    return today, set(lines[1:])


def save_sent(today, sent):
    with open(SENT_FILE, "w") as f:
        f.write(today + "\n" + "\n".join(sorted(sent)))


def scan_stock(symbol):
    df = yf.download(symbol + ".NS", period="1d", interval="5m",
                     progress=False, auto_adjust=True)
    if df is None or len(df) < 8:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = df.index.tz_convert(IST)

    opening = df.between_time("09:15", "09:44")
    if len(opening) < 6:
        return None
    open_high = float(opening["High"].max())

    typical = (df["High"] + df["Low"] + df["Close"]) / 3
    vwap = (typical * df["Volume"]).cumsum() / df["Volume"].cumsum()

    last = df.iloc[-1]
    prev = df.iloc[:-1]
    avg_vol = float(prev["Volume"].mean())
    if avg_vol <= 0:
        return None

    price = float(last["Close"])
    vol_x = float(last["Volume"]) / avg_vol
    above_vwap = price > float(vwap.iloc[-1])
    broke_out = price > open_high
    volume_ok = vol_x >= VOLUME_MULTIPLE
    if not (above_vwap and broke_out and volume_ok):
        return None

    rng = (df["High"] - df["Low"]).tail(12).mean()
    risk = max(float(rng) * STOP_ATR_MULT * 2, price * 0.004)
    stop = max(float(vwap.iloc[-1]), price - risk)
    risk = price - stop
    if risk <= 0:
        return None
    return {
        "symbol": symbol, "entry": price, "stop": stop,
        "target1": price + 1.5 * risk, "target2": price + 2.5 * risk,
        "vol_x": vol_x, "vwap": float(vwap.iloc[-1]),
        "risk_pct": risk / price * 100,
    }


def build_email(results):
    lines = ["इंट्राडे ब्रेकआउट वाले शेयर:", ""]
    for r in results:
        lines += [
            r["symbol"],
            f"  एंट्री के आसपास: {r['entry']:.2f}",
            f"  स्टॉप-लॉस: {r['stop']:.2f} (लगभग {r['risk_pct']:.2f}% नीचे)",
            f"  टारगेट 1: {r['target1']:.2f}   टारगेट 2: {r['target2']:.2f}",
            f"  VWAP: {r['vwap']:.2f}, वॉल्यूम औसत का {r['vol_x']:.1f} गुना",
            "  होल्डिंग: आज ही, ज़्यादा से ज़्यादा दोपहर 3:15 तक",
            "",
        ]
    lines += [
        "सुझाव: टारगेट 1 पर आधा मुनाफ़ा निकालें और स्टॉप-लॉस एंट्री पर ले आएँ।",
        "",
        "चेतावनी: यह सिर्फ़ तकनीकी स्कैन है, निवेश सलाह नहीं। डेटा 15 मिनट देर से हो सकता है, "
        "इसलिए एंट्री से पहले ब्रोकर का भाव देखें। नुकसान हो सकता है।",
    ]
    return "\n".join(lines)


def send_email(subject, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = SEND_TO
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.send_message(msg)


def main():
    if not market_open_now() and os.getenv("FORCE_RUN") != "1":
        print("बाज़ार का समय नहीं है।")
        return
    today, sent = load_sent()
    results = []
    for s in STOCKS:
        if s in sent:
            continue
        try:
            r = scan_stock(s)
            if r:
                results.append(r)
        except Exception as e:
            print(f"{s}: त्रुटि {e}")
    if results:
        send_email(f"इंट्राडे अलर्ट: {len(results)} शेयर", build_email(results))
        sent.update(r["symbol"] for r in results)
        print(f"{len(results)} शेयर मिले, ईमेल भेजा।")
    else:
        print("अभी कोई शेयर शर्तों पर खरा नहीं।")
    save_sent(today, sent)


if __name__ == "__main__":
    main()
