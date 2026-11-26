#!/usr/bin/env python3
"""NSE F&O institutional-style swing scanner. Signals are probabilistic, not proof of institutional activity."""
from __future__ import annotations
import io, os, sys, json, time, logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
import requests
import pandas as pd

BASE = "https://api.dhan.co/v2"
MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
TOKEN = os.getenv("DHAN_ACCESS_TOKEN", "")
CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "")
OUT = Path(os.getenv("OUTPUT_DIR", "reports"))
UNIVERSE_FILE = Path(os.getenv("UNIVERSE_FILE", "data/fno_universe.csv"))
DELIVERY_FILE = Path(os.getenv("DELIVERY_FILE", "data/nse_delivery.csv"))
ORDERFLOW_FILE = Path(os.getenv("ORDERFLOW_FILE", "data/orderflow.csv"))
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "260"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "55"))
MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS", "0"))  # 0 = all F&O underlyings
SESSION = requests.Session()
SESSION.headers.update({"access-token": TOKEN, "Content-Type": "application/json", "Accept": "application/json"})
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

def require_credentials():
    if not TOKEN:
        raise RuntimeError("DHAN_ACCESS_TOKEN is missing. Add it as a GitHub Actions secret or environment variable.")

def get_master() -> pd.DataFrame:
    if UNIVERSE_FILE.exists():
        df = pd.read_csv(UNIVERSE_FILE)
        required = {"symbol", "equity_security_id", "future_security_id"}
        if required.issubset(df.columns):
            return df.drop_duplicates("symbol")
    r = requests.get(MASTER_URL, timeout=60)
    r.raise_for_status()
    raw = pd.read_csv(io.BytesIO(r.content), low_memory=False)
    raw.columns = [c.strip().upper() for c in raw.columns]
    # Dhan scrip-master column names are normalized here to tolerate header case changes.
    def col(*names):
        for n in names:
            if n in raw.columns: return n
        raise RuntimeError("Dhan scrip master missing expected column(s): " + ", ".join(names))
    exch = col("SEM_EXM_EXCH_ID")
    seg = col("SEM_SEGMENT")
    sid = col("SEM_SMST_SECURITY_ID")
    inst = col("SEM_INSTRUMENT_NAME")
    sym = col("SEM_TRADING_SYMBOL")
    expiry = col("SEM_EXPIRY_DATE")
    x = raw.copy()
    x["_exch"] = x[exch].astype(str).str.upper()
    x["_seg"] = x[seg].astype(str).str.upper()
    x["_inst"] = x[inst].astype(str).str.upper()
    x["_sym"] = x[sym].astype(str).str.upper().str.strip()
    symbol_name = "SM_SYMBOL_NAME" if "SM_SYMBOL_NAME" in x.columns else sym
    x["_underlying"] = x[symbol_name].astype(str).str.upper().str.strip()
    x["_sid"] = x[sid].astype(str)
    x["_expiry"] = pd.to_datetime(x[expiry], errors="coerce", dayfirst=True)
    fut = x[(x["_exch"] == "NSE") & (x["_seg"] == "D") & (x["_inst"] == "FUTSTK")].copy()
    # Compact master SM_SYMBOL_NAME carries the underlying name; match it to cash trading symbol.
    fut["_underlying"] = fut["_underlying"].str.replace(r"[^A-Z0-9&-]", "", regex=True)
    fut = fut[fut["_expiry"].notna() & (fut["_expiry"].dt.date >= date.today())]
    fut = fut.sort_values("_expiry").drop_duplicates("_underlying")
    eq = x[(x["_exch"] == "NSE") & (x["_seg"] == "E") & (x["_inst"] == "EQUITY")].drop_duplicates("_sym")
    joined = fut.merge(eq[["_sym", "_sid"]], left_on="_underlying", right_on="_sym", how="inner", suffixes=("_fut", "_eq"))
    out = pd.DataFrame({
        "symbol": joined["_sym"],
        "equity_security_id": joined["_sid_eq"],
        "future_security_id": joined["_sid_fut"],
        "future_expiry": joined["_expiry"].dt.strftime("%Y-%m-%d"),
    }).drop_duplicates("symbol")
    if out.empty:
        raise RuntimeError("Could not derive NSE stock F&O universe from Dhan scrip master.")
    UNIVERSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(UNIVERSE_FILE, index=False)
    logging.info("Loaded %d F&O underlyings from Dhan scrip master", len(out))
    return out

def dhan_daily(security_id: str, instrument: str, from_date: str, to_date: str, oi: bool=False) -> pd.DataFrame:
    payload = {"securityId": str(security_id), "exchangeSegment": "NSE_EQ" if instrument == "EQUITY" else "NSE_FNO",
               "instrument": instrument, "expiryCode": 0, "oi": oi, "fromDate": from_date, "toDate": to_date}
    response = SESSION.post(BASE + "/charts/historical", json=payload, timeout=30)
    if response.status_code == 429:
        time.sleep(2); response = SESSION.post(BASE + "/charts/historical", json=payload, timeout=30)
    response.raise_for_status()
    body = response.json()
    if body.get("status") not in (None, "success") and not body.get("open"):
        raise RuntimeError(f"Dhan historical API error: {body.get('remarks', body)}")
    data = body.get("data", body)
    if not data or not data.get("timestamp"):
        return pd.DataFrame()
    frame = pd.DataFrame({k: data.get(k, []) for k in ("open","high","low","close","volume","open_interest","timestamp") if data.get(k) is not None})
    frame["date"] = pd.to_datetime(frame.pop("timestamp"), unit="s", utc=True, errors="coerce").dt.tz_convert("Asia/Kolkata").dt.tz_localize(None).dt.normalize()
    frame = frame.set_index("date").sort_index()
    frame.columns = [c.title() for c in frame.columns]
    for c in frame.columns: frame[c] = pd.to_numeric(frame[c], errors="coerce")
    return frame.dropna(subset=["Close"])

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    x["EMA9"] = x.Close.ewm(span=9, adjust=False).mean()
    x["EMA21"] = x.Close.ewm(span=21, adjust=False).mean()
    x["SMA50"] = x.Close.rolling(50).mean()
    x["SMA200"] = x.Close.rolling(200).mean()
    prev = x.Close.shift()
    tr = pd.concat([(x.High-x.Low), (x.High-prev).abs(), (x.Low-prev).abs()], axis=1).max(axis=1)
    x["ATR14"] = tr.rolling(14).mean()
    delta = x.Close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-10)
    x["RSI14"] = 100 - (100 / (1 + rs))
    x["VOL20"] = x.Volume.rolling(20).mean()
    x["VOL_X"] = x.Volume / x.VOL20
    x["HIGH20"] = x.High.rolling(20).max().shift(1)
    x["LOW20"] = x.Low.rolling(20).min().shift(1)
    return x

def trend_state(df: pd.DataFrame, rule: str) -> str:
    if len(df) < 30: return "NA"
    z = df.resample(rule).agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna()
    if len(z) < 3: return "NA"
    close = z.Close.iloc[-1]
    e1 = z.Close.ewm(span=9, adjust=False).mean().iloc[-1]
    e2 = z.Close.ewm(span=21, adjust=False).mean().iloc[-1]
    return "BULL" if close > e1 > e2 else ("BEAR" if close < e1 < e2 else "SIDEWAYS")

def load_optional(path: Path) -> pd.DataFrame:
    if not path.exists(): return pd.DataFrame()
    return pd.read_csv(path)

def optional_row(df: pd.DataFrame, symbol: str):
    if df.empty or "symbol" not in df.columns: return {}
    rows = df[df.symbol.astype(str).str.upper() == symbol.upper()]
    if rows.empty: return {}
    return rows.iloc[-1].to_dict()

def score_symbol(symbol: str, eq: pd.DataFrame, fut: pd.DataFrame, delivery: pd.DataFrame, orderflow: pd.DataFrame) -> dict[str, Any] | None:
    if len(eq) < 60: return None
    x = add_indicators(eq)
    last, prev = x.iloc[-1], x.iloc[-2]
    if pd.isna(last.ATR14) or pd.isna(last.VOL_X): return None
    px_chg = (last.Close / prev.Close - 1) * 100 if prev.Close else 0
    oi_chg = None
    if not fut.empty and "Open_Interest" in fut.columns and len(fut) >= 2 and fut.Open_Interest.iloc[-2]:
        oi_chg = (fut.Open_Interest.iloc[-1] / fut.Open_Interest.iloc[-2] - 1) * 100
    drow = optional_row(delivery, symbol)
    orow = optional_row(orderflow, symbol)
    # Delivery CSV expected: symbol,delivery_pct,delivery_pct_avg20 (optional).
    delivery_pct = pd.to_numeric(drow.get("delivery_pct"), errors="coerce")
    delivery_avg = pd.to_numeric(drow.get("delivery_pct_avg20"), errors="coerce")
    cvd = pd.to_numeric(orow.get("cvd"), errors="coerce")
    delta = pd.to_numeric(orow.get("delta"), errors="coerce")
    imb = pd.to_numeric(orow.get("imbalance_ratio"), errors="coerce")
    month = trend_state(eq, "ME")
    week = trend_state(eq, "W-FRI")
    day = "BULL" if last.Close > last.EMA9 > last.EMA21 else ("BEAR" if last.Close < last.EMA9 < last.EMA21 else "SIDEWAYS")
    bull = bear = 0
    reasons_b, reasons_s = [], []
    if month == "BULL": bull += 10; reasons_b.append("Monthly trend bullish")
    elif month == "BEAR": bear += 10; reasons_s.append("Monthly trend bearish")
    if week == "BULL": bull += 15; reasons_b.append("Weekly EMA structure bullish")
    elif week == "BEAR": bear += 15; reasons_s.append("Weekly EMA structure bearish")
    if day == "BULL": bull += 15; reasons_b.append("Daily EMA 9>21")
    elif day == "BEAR": bear += 15; reasons_s.append("Daily EMA 9<21")
    if last.Close > last.SMA50: bull += 8; reasons_b.append("Above 50 DMA")
    else: bear += 8; reasons_s.append("Below 50 DMA")
    if last.Close > last.SMA200: bull += 5
    else: bear += 5
    if px_chg > 0.5: bull += 8; reasons_b.append(f"Price +{px_chg:.2f}%")
    elif px_chg < -0.5: bear += 8; reasons_s.append(f"Price {px_chg:.2f}%")
    volx = float(last.VOL_X)
    if volx >= 1.5 and px_chg > 0: bull += 10; reasons_b.append(f"Volume {volx:.1f}x with price up")
    elif volx >= 1.5 and px_chg < 0: bear += 10; reasons_s.append(f"Volume {volx:.1f}x with price down")
    if oi_chg is not None:
        if oi_chg > 2 and px_chg > 0: bull += 12; reasons_b.append(f"Futures OI +{oi_chg:.1f}% (long build-up candidate)")
        elif oi_chg > 2 and px_chg < 0: bear += 12; reasons_s.append(f"Futures OI +{oi_chg:.1f}% (short build-up candidate)")
        elif oi_chg < -2 and px_chg > 0: bull += 5; reasons_b.append(f"Futures OI {oi_chg:.1f}% (short covering candidate)")
        elif oi_chg < -2 and px_chg < 0: bear += 5; reasons_s.append(f"Futures OI {oi_chg:.1f}% (long unwinding candidate)")
    if pd.notna(delivery_pct):
        if pd.notna(delivery_avg) and delivery_pct > delivery_avg and px_chg > 0:
            bull += 10; reasons_b.append(f"Delivery {delivery_pct:.1f}% above 20D average")
        elif pd.notna(delivery_avg) and delivery_pct > delivery_avg and px_chg < 0:
            bear += 10; reasons_s.append(f"Delivery {delivery_pct:.1f}% above 20D average on down day")
    if pd.notna(cvd):
        if cvd > 0: bull += 8; reasons_b.append("Order-flow CVD positive (user feed)")
        elif cvd < 0: bear += 8; reasons_s.append("Order-flow CVD negative (user feed)")
    if pd.notna(delta):
        if delta > 0: bull += 4
        elif delta < 0: bear += 4
    if pd.notna(imb):
        if imb >= 1.5: bull += 3; reasons_b.append("Bid-side imbalance")
        elif imb <= 0.67: bear += 3; reasons_s.append("Ask-side imbalance")
    direction = "BULLISH" if bull > bear else ("BEARISH" if bear > bull else "MIXED")
    score = max(bull, bear)
    atr = float(last.ATR14)
    entry = float(last.Close)
    if direction == "BULLISH":
        stop = entry - 1.5 * atr
        t1, t2 = entry + 1.5 * atr, entry + 2.5 * atr
    elif direction == "BEARISH":
        stop = entry + 1.5 * atr
        t1, t2 = entry - 1.5 * atr, entry - 2.5 * atr
    else: stop = t1 = t2 = None
    return {"symbol":symbol,"direction":direction,"score":int(score),"bull_score":bull,"bear_score":bear,
            "close":entry,"day_change_pct":round(float(px_chg),2),"volume_x":round(volx,2),
            "futures_oi_change_pct":None if oi_chg is None else round(float(oi_chg),2),
            "delivery_pct":None if pd.isna(delivery_pct) else round(float(delivery_pct),2),
            "monthly":month,"weekly":week,"daily":day,"rsi14":round(float(last.RSI14),1) if pd.notna(last.RSI14) else None,
            "atr14":round(atr,2),"entry_reference":round(entry,2),
            "stop_loss":None if stop is None else round(stop,2),"target_1":None if t1 is None else round(t1,2),
            "target_2":None if t2 is None else round(t2,2),
            "reasons":reasons_b if direction=="BULLISH" else reasons_s,
            "data_flags":{"futures_oi":"available" if oi_chg is not None else "missing",
                          "delivery":"available" if pd.notna(delivery_pct) else "missing",
                          "orderflow":"available" if pd.notna(cvd) or pd.notna(delta) else "missing"}}

def main():
    require_credentials()
    OUT.mkdir(exist_ok=True)
    today = date.today()
    start = (today - timedelta(days=LOOKBACK_DAYS*2)).isoformat()
    end = (today + timedelta(days=1)).isoformat()
    universe = get_master()
    if MAX_SYMBOLS > 0: universe = universe.head(MAX_SYMBOLS)
    delivery = load_optional(DELIVERY_FILE)
    orderflow = load_optional(ORDERFLOW_FILE)
    results, errors = [], []
    for row in universe.itertuples(index=False):
        symbol = str(row.symbol)
        try:
            eq = dhan_daily(str(row.equity_security_id), "EQUITY", start, end, False)
            fut = dhan_daily(str(row.future_security_id), "FUTSTK", start, end, True)
            if eq.empty: continue
            result = score_symbol(symbol, eq, fut, delivery, orderflow)
            if result and result["score"] >= MIN_SCORE and result["direction"] != "MIXED":
                results.append(result)
        except Exception as exc:
            errors.append({"symbol":symbol,"error":str(exc)})
            logging.warning("%s failed: %s", symbol, exc)
        time.sleep(0.12)
    results.sort(key=lambda r:(r["score"], abs(r["day_change_pct"])), reverse=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    payload = {"generated_at":datetime.now().astimezone().isoformat(),"data_provider":"DhanHQ API v2",
               "universe_count":len(universe),"qualified_count":len(results),"min_score":MIN_SCORE,
               "signals":results,"errors":errors,
               "limitations":["Scores are rule-based screening signals, not verified institutional trades.",
               "Delivery and order-flow fields are only scored when supplied in their CSV input files.",
               "Dhan historical OHLCV/OI is not a live order-book or footprint/CVD feed.",
               "Check live price, liquidity, spread, expiry and option Greeks before placing any options trade."]}
    (OUT/f"signals_{stamp}.json").write_text(json.dumps(payload,indent=2,default=str),encoding="utf-8")
    pd.DataFrame(results).to_csv(OUT/f"signals_{stamp}.csv",index=False)
    latest = OUT/"latest.json"; latest.write_text(json.dumps(payload,indent=2,default=str),encoding="utf-8")
    print(json.dumps({"universe":len(universe),"qualified":len(results),"errors":len(errors),"report":str(latest)},indent=2))
    for s in results[:20]:
        print(f'{s["symbol"]:16} {s["direction"]:8} score={s["score"]:3} close={s["close"]:.2f} vol={s["volume_x"]:.2f}x OI={s["futures_oi_change_pct"]}')

if __name__ == "__main__":
    try: main()
    except Exception as exc:
        logging.exception("Scanner stopped: %s", exc)
        sys.exit(1)
