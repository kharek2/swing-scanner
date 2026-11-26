#!/usr/bin/env python3
"""NSE stock-F&O end-of-day swing scanner using DhanHQ API v2."""
from __future__ import annotations
import io, os, re, sys, json, time, logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
import requests
import pandas as pd

BASE = "https://api.dhan.co/v2"
MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
TOKEN = os.getenv("DHAN_ACCESS_TOKEN", "").strip()
CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "").strip()
OUT = Path(os.getenv("OUTPUT_DIR", "reports"))
UNIVERSE_FILE = Path(os.getenv("UNIVERSE_FILE", "data/fno_universe.csv"))
DELIVERY_FILE = Path(os.getenv("DELIVERY_FILE", "data/nse_delivery.csv"))
ORDERFLOW_FILE = Path(os.getenv("ORDERFLOW_FILE", "data/orderflow.csv"))
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "260"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "55"))
MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS", "0"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0.35"))

SESSION = requests.Session()
SESSION.headers.update({"access-token": TOKEN, "Content-Type": "application/json", "Accept": "application/json"})
if CLIENT_ID:
    SESSION.headers["client-id"] = CLIENT_ID
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

def require_credentials():
    if not TOKEN:
        raise RuntimeError("DHAN_ACCESS_TOKEN is missing. Add an active Dhan Data API token in GitHub Actions secrets.")
    if LOOKBACK_DAYS < 210:
        logging.warning("LOOKBACK_DAYS=%s: 200-day moving average may be unavailable.", LOOKBACK_DAYS)

def api_post(path: str, payload: dict) -> dict:
    url = BASE + path
    last_error = None
    for attempt in range(6):
        try:
            response = SESSION.post(url, json=payload, timeout=(15, 60))
            if response.status_code == 429 or response.status_code >= 500:
                wait = min(30, 2 ** attempt)
                logging.warning("Dhan API HTTP %s; retry %s/6 in %ss", response.status_code, attempt + 1, wait)
                time.sleep(wait)
                continue
            if response.status_code in (401, 403):
                raise RuntimeError(f"Dhan API authentication/permission failed (HTTP {response.status_code}). Refresh the Data API token and verify the Dhan API subscription/permissions.")
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                raise RuntimeError(f"Unexpected Dhan API response type: {type(body).__name__}")
            if str(body.get("status", "")).lower() in ("failure", "failed", "error"):
                raise RuntimeError(f"Dhan API rejected request: {body.get('remarks') or body.get('message') or body}")
            return body
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
            wait = min(30, 2 ** attempt)
            logging.warning("Dhan API connection error; retry %s/6 in %ss: %s", attempt + 1, wait, exc)
            time.sleep(wait)
        except requests.HTTPError as exc:
            raise RuntimeError(f"Dhan API HTTP error: {exc}; response={response.text[:500]}") from exc
    raise RuntimeError(f"Dhan API request failed after retries: {last_error or 'rate limit/server error'}")

def normalize_symbol(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value).upper().strip())

def get_master() -> pd.DataFrame:
    if UNIVERSE_FILE.exists():
        cached = pd.read_csv(UNIVERSE_FILE, dtype=str).fillna("")
        required = {"symbol", "equity_security_id", "future_security_id"}
        if required.issubset(cached.columns) and not cached.empty:
            logging.info("Using cached F&O universe: %d symbols", len(cached))
            return cached.drop_duplicates("symbol")
    r = requests.get(MASTER_URL, timeout=90)
    r.raise_for_status()
    raw = pd.read_csv(io.BytesIO(r.content), low_memory=False, dtype=str).fillna("")
    raw.columns = [str(c).strip().upper() for c in raw.columns]
    required = ["SEM_EXM_EXCH_ID", "SEM_SEGMENT", "SEM_SMST_SECURITY_ID", "SEM_INSTRUMENT_NAME", "SEM_TRADING_SYMBOL", "SEM_EXPIRY_DATE"]
    missing = [c for c in required if c not in raw.columns]
    if missing:
        raise RuntimeError("Dhan scrip master schema changed; missing columns: " + ", ".join(missing) + ". Available: " + ", ".join(raw.columns[:35]))
    raw["_exchange"] = raw["SEM_EXM_EXCH_ID"].str.upper().str.strip()
    raw["_segment"] = raw["SEM_SEGMENT"].str.upper().str.strip()
    raw["_instrument"] = raw["SEM_INSTRUMENT_NAME"].str.upper().str.strip()
    raw["_expiry"] = pd.to_datetime(raw["SEM_EXPIRY_DATE"], errors="coerce", dayfirst=True)
    today = pd.Timestamp(date.today())
    eq = raw[(raw["_exchange"] == "NSE") & (raw["_segment"].isin(["E", "I"])) &
             (raw["_instrument"].isin(["EQUITY", "EQ"]))].copy()
    fut = raw[(raw["_exchange"] == "NSE") & (raw["_segment"] == "D") &
              (raw["_instrument"] == "FUTSTK") & raw["_expiry"].notna() &
              (raw["_expiry"] >= today)].copy()
    if eq.empty or fut.empty:
        raise RuntimeError(f"Could not find NSE cash/FUTSTK rows in Dhan master (cash={len(eq)}, futures={len(fut)}). Check current Dhan scrip-master format.")
    eq["_symbol"] = eq["SEM_TRADING_SYMBOL"].str.upper().str.strip()
    eq["_norm"] = eq["_symbol"].map(normalize_symbol)
    # Match the longest cash symbol that is a true prefix of the future contract symbol.
    eq_rows = sorted(eq[["_symbol", "_norm", "SEM_SMST_SECURITY_ID"]].drop_duplicates("_symbol").to_dict("records"),
                     key=lambda x: len(x["_norm"]), reverse=True)
    fut = fut.sort_values("_expiry")
    selected = {}
    for row in fut.to_dict("records"):
        candidates = [row.get("SEM_TRADING_SYMBOL", ""), row.get("SEM_CUSTOM_SYMBOL", "")]
        match = None
        for candidate in candidates:
            normalized = normalize_symbol(candidate)
            for e in eq_rows:
                root = e["_norm"]
                if normalized.startswith(root) and len(normalized) > len(root):
                    tail = normalized[len(root):]
                    if re.match(r"^(\d|FUT|[A-Z]{3})", tail):
                        match = e
                        break
            if match:
                break
        if not match:
            continue
        symbol = match["_symbol"]
        if symbol not in selected:
            selected[symbol] = {
                "symbol": symbol,
                "equity_security_id": match["SEM_SMST_SECURITY_ID"],
                "future_security_id": row["SEM_SMST_SECURITY_ID"],
                "future_expiry": row["_expiry"].strftime("%Y-%m-%d"),
            }
    out = pd.DataFrame(selected.values())
    if out.empty:
        sample = fut["SEM_TRADING_SYMBOL"].head(8).tolist()
        raise RuntimeError("Unable to map Dhan FUTSTK contracts to NSE cash symbols. Future symbol samples: " + repr(sample) + ". Dhan master may have changed its naming convention.")
    UNIVERSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(UNIVERSE_FILE, index=False)
    logging.info("Built NSE stock-F&O universe: %d underlyings", len(out))
    return out

def dhan_daily(security_id: str, instrument: str, from_date: str, to_date: str, oi: bool = False) -> pd.DataFrame:
    payload = {
        "securityId": str(security_id).strip(),
        "exchangeSegment": "NSE_EQ" if instrument == "EQUITY" else "NSE_FNO",
        "instrument": instrument,
        "expiryCode": 0,
        "oi": bool(oi),
        "fromDate": from_date,
        "toDate": to_date,
    }
    body = api_post("/charts/historical", payload)
    data = body.get("data", body)
    if not isinstance(data, dict) or not data.get("timestamp"):
        return pd.DataFrame()
    # Dhan's futures historical response uses 'oi'; tolerate 'open_interest' too.
    oi_values = data.get("oi", data.get("open_interest", data.get("openInterest")))
    frame_data = {}
    for key in ("open", "high", "low", "close", "volume"):
        if data.get(key) is not None:
            frame_data[key.title()] = data[key]
    if oi_values is not None:
        frame_data["Open_Interest"] = oi_values
    frame_data["timestamp"] = data["timestamp"]
    frame = pd.DataFrame(frame_data)
    frame["date"] = pd.to_datetime(frame.pop("timestamp"), unit="s", utc=True, errors="coerce").dt.tz_convert("Asia/Kolkata").dt.tz_localize(None).dt.normalize()
    frame = frame.dropna(subset=["date"]).set_index("date").sort_index()
    for col in frame.columns:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    if "Close" not in frame:
        return pd.DataFrame()
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
    x["VOL_X"] = x.Volume / x.VOL20.replace(0, pd.NA)
    return x

def trend_state(df: pd.DataFrame, rule: str) -> str:
    if len(df) < 30:
        return "NA"
    z = df.resample(rule).agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna()
    if len(z) < 3:
        return "NA"
    close = z.Close.iloc[-1]
    e1 = z.Close.ewm(span=9, adjust=False).mean().iloc[-1]
    e2 = z.Close.ewm(span=21, adjust=False).mean().iloc[-1]
    return "BULL" if close > e1 > e2 else ("BEAR" if close < e1 < e2 else "SIDEWAYS")

def load_optional(path: Path) -> pd.DataFrame:
    return pd.read_csv(path) if path.exists() else pd.DataFrame()

def optional_row(df: pd.DataFrame, symbol: str) -> dict:
    if df.empty or "symbol" not in df.columns:
        return {}
    rows = df[df.symbol.astype(str).str.upper().str.strip() == symbol.upper().strip()]
    return rows.iloc[-1].to_dict() if not rows.empty else {}

def score_symbol(symbol: str, eq: pd.DataFrame, fut: pd.DataFrame, delivery: pd.DataFrame, orderflow: pd.DataFrame) -> dict[str, Any] | None:
    if len(eq) < 60:
        return None
    x = add_indicators(eq)
    last, prev = x.iloc[-1], x.iloc[-2]
    if pd.isna(last.ATR14) or pd.isna(last.VOL_X):
        return None
    px_chg = (last.Close / prev.Close - 1) * 100 if prev.Close else 0
    oi_chg = None
    if not fut.empty and "Open_Interest" in fut.columns and len(fut) >= 2 and fut.Open_Interest.iloc[-2]:
        oi_chg = (fut.Open_Interest.iloc[-1] / fut.Open_Interest.iloc[-2] - 1) * 100
    drow, orow = optional_row(delivery, symbol), optional_row(orderflow, symbol)
    delivery_pct = pd.to_numeric(drow.get("delivery_pct"), errors="coerce")
    delivery_avg = pd.to_numeric(drow.get("delivery_pct_avg20"), errors="coerce")
    cvd = pd.to_numeric(orow.get("cvd"), errors="coerce")
    delta = pd.to_numeric(orow.get("delta"), errors="coerce")
    imb = pd.to_numeric(orow.get("imbalance_ratio"), errors="coerce")
    month, week = trend_state(eq, "ME"), trend_state(eq, "W-FRI")
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
    if pd.notna(delivery_pct) and pd.notna(delivery_avg) and delivery_pct > delivery_avg:
        if px_chg > 0: bull += 10; reasons_b.append(f"Delivery {delivery_pct:.1f}% above 20D average")
        elif px_chg < 0: bear += 10; reasons_s.append(f"Delivery {delivery_pct:.1f}% above 20D average on down day")
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
    atr, entry = float(last.ATR14), float(last.Close)
    if direction == "BULLISH": stop, t1, t2 = entry-1.5*atr, entry+1.5*atr, entry+2.5*atr
    elif direction == "BEARISH": stop, t1, t2 = entry+1.5*atr, entry-1.5*atr, entry-2.5*atr
    else: stop = t1 = t2 = None
    return {"symbol":symbol,"direction":direction,"score":int(score),"bull_score":bull,"bear_score":bear,
        "close":entry,"day_change_pct":round(float(px_chg),2),"volume_x":round(volx,2),
        "futures_oi_change_pct":None if oi_chg is None else round(float(oi_chg),2),
        "delivery_pct":None if pd.isna(delivery_pct) else round(float(delivery_pct),2),
        "monthly":month,"weekly":week,"daily":day,
        "rsi14":round(float(last.RSI14),1) if pd.notna(last.RSI14) else None,
        "atr14":round(atr,2),"entry_reference":round(entry,2),
        "stop_loss":None if stop is None else round(stop,2),"target_1":None if t1 is None else round(t1,2),
        "target_2":None if t2 is None else round(t2,2),
        "reasons":reasons_b if direction=="BULLISH" else reasons_s,
        "data_flags":{"futures_oi":"available" if oi_chg is not None else "missing",
                      "delivery":"available" if pd.notna(delivery_pct) else "missing",
                      "orderflow":"available" if pd.notna(cvd) or pd.notna(delta) else "missing"}}

def main():
    require_credentials()
    OUT.mkdir(parents=True, exist_ok=True)
    today = date.today()
    start = (today - timedelta(days=LOOKBACK_DAYS * 2)).isoformat()
    end = (today + timedelta(days=1)).isoformat()
    universe = get_master()
    if MAX_SYMBOLS > 0:
        universe = universe.head(MAX_SYMBOLS)
    delivery, orderflow = load_optional(DELIVERY_FILE), load_optional(ORDERFLOW_FILE)
    results, errors = [], []
    total = len(universe)
    for index, row in enumerate(universe.itertuples(index=False), 1):
        symbol = str(row.symbol)
        try:
            eq = dhan_daily(str(row.equity_security_id), "EQUITY", start, end)
            time.sleep(REQUEST_DELAY)
            fut = dhan_daily(str(row.future_security_id), "FUTSTK", start, end, True)
            time.sleep(REQUEST_DELAY)
            if eq.empty:
                raise RuntimeError("Dhan returned no cash-equity candles")
            if fut.empty:
                logging.warning("%s: no futures candles/OI returned", symbol)
            result = score_symbol(symbol, eq, fut, delivery, orderflow)
            if result and result["score"] >= MIN_SCORE and result["direction"] != "MIXED":
                results.append(result)
        except Exception as exc:
            errors.append({"symbol":symbol,"error":str(exc)})
            logging.exception("%s failed", symbol)
        if index % 10 == 0 or index == total:
            logging.info("Progress: %d/%d symbols; qualified=%d; errors=%d", index, total, len(results), len(errors))
    results.sort(key=lambda r:(r["score"], abs(r["day_change_pct"])), reverse=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    payload = {"generated_at":datetime.now().astimezone().isoformat(),"data_provider":"DhanHQ API v2",
        "universe_count":total,"qualified_count":len(results),"error_count":len(errors),"min_score":MIN_SCORE,
        "signals":results,"errors":errors,
        "limitations":["Rule-based scores are screening points, not calibrated probabilities or proof of institutional trades.",
        "Delivery and order-flow fields are only scored when supplied in CSV input files.",
        "Historical daily OHLCV/OI is not a live order-book, footprint or tick-by-tick CVD feed.",
        "Validate live price, liquidity, spread, expiry, IV and option Greeks before trading."]}
    (OUT/f"signals_{stamp}.json").write_text(json.dumps(payload,indent=2,default=str),encoding="utf-8")
    pd.DataFrame(results).to_csv(OUT/f"signals_{stamp}.csv",index=False)
    (OUT/"latest.json").write_text(json.dumps(payload,indent=2,default=str),encoding="utf-8")
    print(json.dumps({"universe":total,"qualified":len(results),"errors":len(errors),"report":str(OUT/"latest.json")},indent=2))
    if total and len(errors) == total:
        raise RuntimeError(f"All {total} symbols failed. See per-symbol errors above; report was still written.")
    for s in results[:20]:
        print(f'{s["symbol"]:16} {s["direction"]:8} score={s["score"]:3} close={s["close"]:.2f} vol={s["volume_x"]:.2f}x OI={s["futures_oi_change_pct"]}')

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logging.exception("Scanner stopped: %s", exc)
        sys.exit(1)
