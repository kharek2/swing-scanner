# NSE F&O Institutional-Style Swing Scanner

This is a research/screening tool, not an execution bot and not proof of institutional activity.

## Current implementation

- Builds an NSE stock-F&O universe from the Dhan instrument master (nearest unexpired FUTSTK contract matched to cash equity).
- Pulls daily cash OHLCV and futures OHLCV/open-interest history from DhanHQ API v2.
- Calculates monthly, weekly and daily EMA trend structure, 50/200 DMA, RSI, ATR, relative volume and 20-day breakout context.
- Scores bullish/bearish alignment and price + futures OI combinations (long/short build-up and unwinding candidates).
- Produces JSON and CSV reports with entry reference, ATR-based invalidation/stop and illustrative targets.
- Runs after market close on weekdays through GitHub Actions; report is downloadable from the workflow run's Artifacts.

## Important data limitations (do not ignore)

1. **Delivery is not automatically fetched from NSE in this first version.** To include delivery participation, supply `data/nse_delivery.csv` with columns `symbol,delivery_pct,delivery_pct_avg20`. Values must be percentage points (e.g. 48.5), not fractions. The template is in `data/delivery_template.csv`.
2. **Order flow is not automatically available from daily candles.** CVD, bid/ask delta and imbalance require a timestamped trade/depth feed and a defined aggregation interval. If your GoCharting/Dhan feed exports daily symbol aggregates, supply `data/orderflow.csv` with `symbol,cvd,delta,imbalance_ratio`. Template: `data/orderflow_template.csv`. Never treat these as official NSE institutional participant identifiers.
3. This version does **not** yet ingest NSE UDiFF/Bhavcopy files, compute options-chain Greeks, or provide tick-by-tick footprint/absorption. It is therefore an institutional-style multi-factor scanner, not a complete institutional order-flow system.
4. Dhan historical daily futures OI is used as a futures OI proxy. It is not participant-wise FII/DII data and does not reveal who traded.
5. The scanner is end-of-day, not real-time. Do not use its closing-price reference as an executable option entry. Validate current price, option liquidity, bid/ask spread, expiry, IV and Greeks in your broker terminal.

## Setup in GitHub

1. Open repository Settings → Secrets and variables → Actions.
2. Add repository secret `DHAN_ACCESS_TOKEN` using your active Dhan Data API access token. Never paste it into source code or chat.
3. Open Actions → Institutional Swing Scanner → Run workflow for a manual test, or wait for the weekday schedule (12:30 UTC / 18:00 IST).
4. Open the completed run and download the `institutional-swing-report` artifact.

## Run locally (Mac)

Python 3.10+ recommended (3.11 tested in CI):
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DHAN_ACCESS_TOKEN="YOUR_DHAN_ACCESS_TOKEN"
python institutional_scanner.py
```

## Score interpretation

Scores are transparent rule-based points, not calibrated probabilities. Missing delivery/order-flow inputs are marked missing and do not earn points. A bullish/bearish score is a watchlist filter only. Backtest and paper-trade before risking capital.

## Next engineering stages

- NSE UDiFF CM + FO bhavcopy ingestion and delivery percentage validation.
- Dhan live WebSocket depth/trade adapter for CVD, delta, bid/ask imbalance and absorption proxies.
- Options-chain selection layer: ATM/ITM strike, expiry, IV, Greeks, spread and liquidity.
- Walk-forward backtesting, transaction-cost model, slippage and signal quality metrics.
- Dashboard and alert delivery (email/Telegram) with rate-limits and deduplication.
