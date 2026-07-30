"""MetaAPI client for Exness MT5 — candles, price, positions, market orders."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

HERE = Path(__file__).resolve().parent
DEFAULT_CFG = HERE / "config" / "metaapi.json"
PIP_SIZE = 0.10  # XAUUSD


class MetaApiClient:
    def __init__(self, cfg_path: Path | str | None = None):
        path = Path(cfg_path) if cfg_path else DEFAULT_CFG
        if not path.exists():
            raise FileNotFoundError(f"MetaAPI config not found: {path}")
        self.cfg = json.loads(path.read_text())
        self.token = self.cfg["token"].strip()
        self.account_id = self.cfg["account_id"]
        self.region = self.cfg.get("region", "london")
        self.symbol = self.cfg.get("resolved_symbol", "XAUUSDm")
        self.headers = {
            "auth-token": self.token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self.market_host = self._pick_market_host()
        self.trade_host = f"https://mt-client-api-v1.{self.region}.agiliumtrade.ai"

    def _pick_market_host(self) -> str:
        hosts = [
            self.cfg.get("market_data_host", ""),
            f"https://mt-market-data-client-api-v1.{self.region}.agiliumtrade.ai",
            "https://mt-market-data-client-api-v1.london.agiliumtrade.ai",
            "https://mt-market-data-client-api-v1.new-york.agiliumtrade.ai",
        ]
        for h in hosts:
            if not h:
                continue
            url = (
                f"{h}/users/current/accounts/{self.account_id}"
                f"/historical-market-data/symbols/{self.symbol}/timeframes/1m/candles"
            )
            try:
                r = requests.get(url, headers=self.headers, params={"limit": 1}, timeout=20)
                if r.status_code == 200:
                    return h
            except requests.RequestException:
                continue
        raise RuntimeError("MetaAPI market-data probe failed — check token / account / region")

    def fetch_m1(self, bars: int = 2000) -> pd.DataFrame:
        rows: list[dict] = []
        cursor = datetime.now(timezone.utc)
        remaining = bars
        while remaining > 0:
            limit = min(1000, remaining)
            url = (
                f"{self.market_host}/users/current/accounts/{self.account_id}"
                f"/historical-market-data/symbols/{self.symbol}/timeframes/1m/candles"
            )
            r = requests.get(
                url,
                headers=self.headers,
                params={"startTime": cursor.isoformat().replace("+00:00", "Z"), "limit": limit},
                timeout=60,
            )
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            rows.extend(batch)
            oldest = min(batch, key=lambda x: x["time"])
            cursor = datetime.fromisoformat(oldest["time"].replace("Z", "+00:00")) - timedelta(seconds=1)
            remaining -= len(batch)
            time.sleep(0.15)
            if len(batch) < limit:
                break

        df = pd.DataFrame(rows)
        if df.empty:
            return df
        df["time"] = pd.to_datetime(df["time"], utc=True)
        if "tickVolume" in df.columns:
            df = df.rename(columns={"tickVolume": "tick_volume"})
        if "tick_volume" not in df.columns:
            df["tick_volume"] = 0
        return (
            df[["time", "open", "high", "low", "close", "tick_volume"]]
            .sort_values("time")
            .drop_duplicates("time")
            .reset_index(drop=True)
        )

    def current_price(self) -> dict[str, float]:
        url = (
            f"{self.trade_host}/users/current/accounts/{self.account_id}"
            f"/symbols/{self.symbol}/current-price"
        )
        r = requests.get(url, headers=self.headers, timeout=20)
        r.raise_for_status()
        d = r.json()
        return {"bid": float(d["bid"]), "ask": float(d["ask"])}

    def positions(self) -> list[dict]:
        url = f"{self.trade_host}/users/current/accounts/{self.account_id}/positions"
        r = requests.get(url, headers=self.headers, timeout=20)
        if r.status_code != 200:
            return []
        data = r.json()
        if not isinstance(data, list):
            return []
        return [p for p in data if str(p.get("symbol", "")).startswith("XAU")]

    def open_market(
        self,
        direction: str,
        volume: float,
        sl: float,
        tp: float,
        comment: str = "V16_ML",
    ) -> dict[str, Any]:
        action = "ORDER_TYPE_BUY" if direction == "BUY" else "ORDER_TYPE_SELL"
        body = {
            "actionType": action,
            "symbol": self.symbol,
            "volume": float(volume),
            "stopLoss": float(round(sl, 2)),
            "takeProfit": float(round(tp, 2)),
            "comment": comment[:30],
        }
        url = f"{self.trade_host}/users/current/accounts/{self.account_id}/trade"
        r = requests.post(url, headers=self.headers, json=body, timeout=30)
        try:
            payload = r.json()
        except Exception:
            payload = {"raw": r.text}
        return {
            "http_status": r.status_code,
            "ok": self._trade_ok(r.status_code, payload),
            "request": body,
            "response": payload,
        }

    @staticmethod
    def _trade_ok(http_status: int, payload: Any) -> bool:
        """HTTP 200 alone is not enough — INVALID_STOPS also returns 200."""
        if http_status >= 300 or not isinstance(payload, dict):
            return False
        code = payload.get("numericCode")
        s_code = str(payload.get("stringCode") or "")
        return code == 10009 or s_code == "TRADE_RETCODE_DONE"

    def close_position(self, position_id: str, volume: float | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"actionType": "POSITION_CLOSE_ID", "positionId": str(position_id)}
        if volume is not None:
            body["volume"] = float(volume)
        url = f"{self.trade_host}/users/current/accounts/{self.account_id}/trade"
        r = requests.post(url, headers=self.headers, json=body, timeout=30)
        try:
            payload = r.json()
        except Exception:
            payload = {"raw": r.text}
        return {
            "http_status": r.status_code,
            "ok": self._trade_ok(r.status_code, payload),
            "response": payload,
        }

    def our_v16_positions(self) -> list[dict]:
        """Only positions opened by this bot (comment starts with V16_)."""
        return [p for p in self.positions() if str(p.get("comment", "")).startswith("V16_")]
