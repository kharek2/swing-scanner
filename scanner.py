import os
import smtplib
from email.mime.text import MIMEText

import pandas as pd
import yfinance as yf

# ---------- सेटिंग्स ----------
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "your_email@gmail.com")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "xxxx xxxx xxxx xxxx")
SEND_TO = os.getenv("SEND_TO", GMAIL_ADDRESS)

# नमूने की सूची। आप इसमें और NSE शेयर जोड़ सकते हैं (अंत में .NS ज़रूरी है)
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

BREAKOUT_DAYS = 20      # पिछले इतने दिन का हाई तोड़ना चाहिए
VOLUME_MULTIPLE = 1.5   # वॉल्यूम औसत का कम से कम इतना गुना
ATR_STOP_MULT = 1.5     # स्टॉप-लॉस = एंट्री - 1.5 x ATR


def atr(df, period=14):
    high_low = df["High"] - df["Low"]
    high_close = (df["High"] - df["Close"].shift()).abs()
    low_close = (df["Low"] - df["Close"].shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return true_range.rolling(period).mean()


def scan_stock(symbol):
    df = yf.download(symbol + ".NS", period="8mo", interval="1d",
                     progress=False, auto_adjust=True)
    if df is None or len(df) < 60:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df["DMA50"] = df["Close"].rolling(50).mean()
    df["VOL_AVG"] = df["Volume"].rolling(20).mean()
    df["HIGH_N"] = df["High"].rolling(BREAKOUT_DAYS).max().shift(1)
    df["ATR"] = atr(df)

    last = df.iloc[-1]
    if pd.isna(last["HIGH_N"]) or pd.isna(last["ATR"]) or pd.isna(last["DMA50"]):
        return None

    breakout = last["Close"] > last["HIGH_N"]
    volume_ok = last["Volume"] >= VOLUME_MULTIPLE * last["VOL_AVG"]
    trend_ok = last["Close"] > last["DMA50"]

    if not (breakout and volume_ok and trend_ok):
        return None

    entry = float(last["Close"])
    risk = ATR_STOP_MULT * float(last["ATR"])
    stop = entry - risk
    target1 = entry + 1.5 * risk
    target2 = entry + 2.5 * risk

    return {
        "symbol": symbol,
        "entry": entry,
        "stop": stop,
        "target1": target1,
        "target2": target2,
        "volume_x": float(last["Volume"] / last["VOL_AVG"]),
        "risk_pct": risk / entry * 100,
    }


def build_email(results):
    lines = ["आज के स्कैन में ब्रेकआउट वाले शेयर:", ""]
    for r in results:
        lines += [
            f"{r['symbol']}",
            f"  एंट्री के आसपास: {r['entry']:.2f}",
            f"  स्टॉप-लॉस: {r['stop']:.2f} (लगभग {r['risk_pct']:.1f}% नीचे)",
            f"  टारगेट 1: {r['target1']:.2f}   टारगेट 2: {r['target2']:.2f}",
            f"  वॉल्यूम औसत का {r['volume_x']:.1f} गुना",
            "  अनुमानित होल्डिंग: 3 से 10 ट्रेडिंग दिन, या स्टॉप/टारगेट लगने तक",
            "",
        ]
    lines += [
        "सुझाव: टारगेट 1 पर आधा मुनाफ़ा निकालकर स्टॉप-लॉस एंट्री पर ले आएँ।",
        "",
        "चेतावनी: यह सिर्फ़ तकनीकी नियमों पर आधारित स्कैन है, निवेश सलाह नहीं। "
        "बाज़ार में नुकसान हो सकता है। अपना शोध करें और जोखिम सीमित रखें।",
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
    results = []
    for s in STOCKS:
        try:
            r = scan_stock(s)
            if r:
                results.append(r)
        except Exception as e:
            print(f"{s}: त्रुटि {e}")

    if results:
        send_email(f"स्विंग स्कैन: {len(results)} शेयर मिले", build_email(results))
        print(f"{len(results)} शेयर मिले, ईमेल भेज दिया।")
    else:
        print("आज कोई शेयर शर्तों पर खरा नहीं उतरा।")


if __name__ == "__main__":
    main()
