import importlib.util
from pathlib import Path
import pandas as pd

SPEC = importlib.util.spec_from_file_location("scanner", Path(__file__).parents[1] / "institutional_scanner.py")
scanner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scanner)

def test_normalize_symbol_removes_contract_separators():
    assert scanner.normalize_symbol("RELIANCE 30 DEC 2026 FUT") == "RELIANCE30DEC2026FUT"

def test_historical_parser_accepts_dhan_oi_key(monkeypatch):
    payload = {
        "data": {
            "timestamp": [1795651200, 1795737600],
            "open": [100, 101], "high": [102, 103], "low": [99, 100],
            "close": [101, 102], "volume": [1000, 1200], "oi": [50000, 52000]
        }
    }
    monkeypatch.setattr(scanner, "api_post", lambda path, body: payload)
    df = scanner.dhan_daily("12345", "FUTSTK", "2026-11-01", "2026-11-26", True)
    assert "Open_Interest" in df.columns
    assert df["Open_Interest"].tolist() == [50000, 52000]

def test_historical_parser_accepts_legacy_open_interest(monkeypatch):
    payload = {
        "data": {
            "timestamp": [1795651200, 1795737600],
            "open": [100, 101], "high": [102, 103], "low": [99, 100],
            "close": [101, 102], "volume": [1000, 1200], "open_interest": [50000, 52000]
        }
    }
    monkeypatch.setattr(scanner, "api_post", lambda path, body: payload)
    df = scanner.dhan_daily("12345", "FUTSTK", "2026-11-01", "2026-11-26", True)
    assert df["Open_Interest"].tolist() == [50000, 52000]

def test_score_returns_atr_levels_and_flags():
    idx = pd.date_range("2025-01-01", periods=260, freq="B")
    close = pd.Series(range(100, 360), index=idx, dtype=float)
    eq = pd.DataFrame({
        "Open": close - 0.5, "High": close + 1, "Low": close - 1,
        "Close": close, "Volume": 100000
    }, index=idx)
    result = scanner.score_symbol("TEST", eq, pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
    assert result is not None
    assert result["direction"] in ("BULLISH", "BEARISH", "MIXED")
    assert result["atr14"] > 0
    assert result["data_flags"]["futures_oi"] == "missing"
