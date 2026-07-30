"""Build M5 features for V16 and ASARC from live M1 candles.

Parity rules (must match research / bars_m5_features):
  - indicators: Wilder RSI/ATR, EMA set (indicators.py)
  - trend_score (regime_features.py)
  - ema20_slope = ema_20.diff(3)  (NOT diff(1))
  - asian high/low/mid distances (sessions.py)
  - atr_pct, volatility_regime → vol_code
  - H1 bias from M5→H1 ema50 (matches h1_price_above_ema50 intent)
  - session windows identical to config.py / sessions.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# V16 model inputs (unchanged)
FEATURES = [
    "rsi_14", "atr_14", "volume_ratio", "trend_score", "body_size",
    "upper_wick", "lower_wick", "distance_from_asian_high",
    "distance_from_asian_low", "hour",
]

# ASARC absolute-best (rich pack) — matches asarc_b_july_loop FEAT_PACKS["rich"]
ASARC_FEATURES = [
    "rsi_14", "atr_pct", "trend_score", "body_size", "upper_wick", "lower_wick",
    "dist_mid_atr", "stretch", "ema20_slope", "hour", "session_code",
    "h1_bias", "vol_code",
    "dist_high_atr", "dist_low_atr", "body_atr", "wick_imbalance",
]

SESSIONS = {
    "asia": (0, 0, 6, 59),
    "london": (7, 0, 11, 59),
    "newyork": (12, 0, 20, 59),
    "overlap": (12, 0, 16, 0),
}
SESSION_CODE = {"asia": 0, "london": 1, "overlap": 2, "newyork": 3, "off": 4}


def _in_window(h: int, m: int, sh: int, sm: int, eh: int, em: int) -> bool:
    cur = h * 60 + m
    return sh * 60 + sm <= cur <= eh * 60 + em


def classify_session(h: int, m: int) -> str:
    if _in_window(h, m, *SESSIONS["overlap"]):
        return "overlap"
    if _in_window(h, m, *SESSIONS["london"]):
        return "london"
    if _in_window(h, m, *SESSIONS["newyork"]):
        return "newyork"
    if _in_window(h, m, *SESSIONS["asia"]):
        return "asia"
    return "off"


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def m1_to_m5(m1: pd.DataFrame) -> pd.DataFrame:
    x = m1.copy()
    x["time"] = pd.to_datetime(x["time"], utc=True)
    x = x.set_index("time").sort_index()
    agg = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "tick_volume": "sum",
    }
    if "spread" in x.columns:
        agg["spread"] = "mean"
    return x.resample("5min").agg(agg).dropna(subset=["open", "close"]).reset_index()


def _add_h1_bias(df: pd.DataFrame) -> pd.Series:
    """H1 close vs H1 EMA50, forward-filled onto M5 — mirrors h1_price_above_ema50."""
    x = df.set_index("time").sort_index()
    h1 = x["close"].resample("1h").last().dropna().to_frame("close")
    if len(h1) < 60:
        return pd.Series(0.0, index=df.index)
    h1["ema_50"] = _ema(h1["close"], 50)
    h1["h1_bias"] = (h1["close"] > h1["ema_50"]).astype(float)
    # map back: each M5 gets last completed H1 bias
    mapped = h1["h1_bias"].reindex(x.index, method="ffill")
    return mapped.fillna(0).reset_index(drop=True)


def add_features(m5: pd.DataFrame) -> pd.DataFrame:
    """Full feature set for V16 + ASARC (single source of truth for live/train)."""
    df = m5.copy()
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.sort_values("time").reset_index(drop=True)

    for p in (9, 20, 50, 100, 200):
        df[f"ema_{p}"] = _ema(df["close"], p)
    df["rsi_14"] = _rsi(df["close"], 14)
    df["atr_14"] = _atr(df, 14)
    df["atr_pct"] = df["atr_14"] / df["close"].replace(0, np.nan) * 100

    vol_ma = df["tick_volume"].rolling(20).mean() if "tick_volume" in df.columns else pd.Series(np.nan, index=df.index)
    df["volume_ma_20"] = vol_ma
    df["volume_ratio"] = df.get("tick_volume", 0) / vol_ma.replace(0, np.nan)

    df["body_size"] = (df["close"] - df["open"]).abs()
    df["candle_range"] = df["high"] - df["low"]
    df["upper_wick"] = df["high"] - df[["open", "close"]].max(axis=1)
    df["lower_wick"] = df[["open", "close"]].min(axis=1) - df["low"]
    df["body_to_range_ratio"] = df["body_size"] / df["candle_range"].replace(0, np.nan)

    ema9_above_ema20 = df["ema_9"] > df["ema_20"]
    ema20_above_ema50 = df["ema_20"] > df["ema_50"]
    ema50_above_ema200 = df["ema_50"] > df["ema_200"]
    df["trend_score"] = (
        ema9_above_ema20.astype(int)
        + ema20_above_ema50.astype(int)
        + ema50_above_ema200.astype(int)
        + (df["close"] > df["ema_20"]).astype(int)
        + (df["close"] > df["ema_50"]).astype(int)
        - (df["close"] < df["ema_20"]).astype(int)
        - (df["close"] < df["ema_50"]).astype(int)
    )
    df["price_above_ema20"] = (df["close"] > df["ema_20"]).astype(int)
    df["price_above_ema50"] = (df["close"] > df["ema_50"]).astype(int)
    # CRITICAL: match regime_features.py (diff 3), not diff(1)
    df["ema20_slope"] = df["ema_20"].diff(3)
    df["ema50_slope"] = df["ema_50"].diff(3)

    t = df["time"]
    df["hour"] = t.dt.hour
    df["minute"] = t.dt.minute
    df["session"] = [classify_session(h, m) for h, m in zip(df["hour"], df["minute"])]
    df["session_code"] = df["session"].map(SESSION_CODE).fillna(4).astype(float)
    df["is_asia"] = df["session"] == "asia"
    df["date"] = t.dt.date

    asia = df[df["is_asia"]].groupby("date").agg(
        asian_high=("high", "max"),
        asian_low=("low", "min"),
    )
    df = df.merge(asia, left_on="date", right_index=True, how="left")
    df["asian_mid"] = (df["asian_high"] + df["asian_low"]) / 2.0
    df["distance_from_asian_high"] = df["close"] - df["asian_high"]
    df["distance_from_asian_low"] = df["close"] - df["asian_low"]
    df["distance_from_asian_mid"] = df["close"] - df["asian_mid"]
    atr = df["atr_14"].replace(0, np.nan)
    df["dist_mid_atr"] = df["distance_from_asian_mid"] / atr
    df["stretch"] = df["dist_mid_atr"].abs()
    df["dist_high_atr"] = df["distance_from_asian_high"] / atr
    df["dist_low_atr"] = df["distance_from_asian_low"] / atr
    df["body_atr"] = df["body_size"] / atr
    df["wick_imbalance"] = (df["lower_wick"] - df["upper_wick"]) / atr

    # ATR regime → vol_code (same buckets as regime_features)
    atr_pct = df["atr_pct"]
    q75 = atr_pct.rolling(100).quantile(0.75)
    q25 = atr_pct.rolling(100).quantile(0.25)
    df["volatility_regime"] = np.where(
        atr_pct > q75, "high", np.where(atr_pct < q25, "low", "normal")
    )
    df["vol_code"] = df["volatility_regime"].map({"low": 0, "normal": 1, "high": 2}).fillna(1).astype(float)

    df["h1_bias"] = _add_h1_bias(df)

    if "spread" in df.columns and "spread_pips" not in df.columns:
        # MetaAPI/broker spread often in points; keep raw + best-effort pips
        df["spread_pips"] = pd.to_numeric(df["spread"], errors="coerce")
    if "spread_pips" not in df.columns:
        df["spread_pips"] = 1.5

    return df


def latest_feature_row(m1: pd.DataFrame, feature_list: list[str] | None = None) -> pd.Series | None:
    """Latest completed M5 feature row. feature_list defaults to V16 FEATURES."""
    need = feature_list or FEATURES
    if m1 is None or len(m1) < 300:
        return None
    m5 = m1_to_m5(m1)
    if len(m5) < 220:
        return None
    now = pd.Timestamp.now(tz="UTC")
    last_t = pd.Timestamp(m5.iloc[-1]["time"])
    if last_t.tzinfo is None:
        last_t = last_t.tz_localize("UTC")
    if now < last_t + pd.Timedelta(minutes=5):
        m5 = m5.iloc[:-1]
    feat = add_features(m5)
    row = feat.iloc[-1]
    if any(pd.isna(row.get(c)) for c in need):
        return None
    return row


def latest_asarc_row(m1: pd.DataFrame) -> pd.Series | None:
    return latest_feature_row(m1, ASARC_FEATURES)
