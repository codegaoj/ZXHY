from __future__ import annotations

import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from .storage import Storage, DB_PATH
from .data_provider import AkShareProvider
from .data_normalizer import calculate_macd

PROJECT_ROOT = Path(r"g:\BaiduNetdiskDownload\z直播文字\trading-system-core")


class PortfolioEngine:
    def __init__(self):
        self.provider = AkShareProvider(adjust="qfq", retries=1, pause_seconds=0.4)
        self._cache: dict[str, tuple[float, dict]] = {}

    def _fetch_indicators(self, symbol: str) -> Optional[dict]:
        now = time.time()
        cached = self._cache.get(symbol)
        if cached and (now - cached[0]) < 300:
            return cached[1]
        end = date.today()
        start = end - timedelta(days=180)
        try:
            raw = self.provider.fetch_daily(
                symbol, start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
            )
            if raw is None or raw.empty:
                return None
            df = raw.copy()
            if "日期" in df.columns:
                df = df.rename(columns={
                    "日期": "date", "开盘": "open", "收盘": "close",
                    "最高": "high", "最低": "low", "成交量": "volume",
                })
            for col in ("date", "open", "close", "high", "low", "volume"):
                if col not in df.columns:
                    return None
            df["close"] = pd.to_numeric(df["close"], errors="coerce")
            df["high"] = pd.to_numeric(df["high"], errors="coerce")
            df["low"] = pd.to_numeric(df["low"], errors="coerce")
            df = df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
            if len(df) < 30:
                return None
            close = df["close"].astype(float)
            high = df["high"].astype(float)
            low = df["low"].astype(float)
            white_line = float(close.ewm(span=10, adjust=False).mean().ewm(span=10, adjust=False).mean().iloc[-1])
            yellow_line = float((
                close.rolling(14, min_periods=1).mean()
                + close.rolling(28, min_periods=1).mean()
                + close.rolling(57, min_periods=1).mean()
                + close.rolling(114, min_periods=1).mean()
            ).iloc[-1] / 4)
            macd = calculate_macd(close)
            macd_dif = float(macd["macd_dif"].iloc[-1])
            macd_dea = float(macd["macd_dea"].iloc[-1])
            recent20 = df.tail(20)
            support = float(recent20["low"].min())
            resistance = float(recent20["high"].max())
            latest_close = float(close.iloc[-1])
            result = {
                "close": round(latest_close, 4),
                "white_line": round(white_line, 4),
                "yellow_line": round(yellow_line, 4),
                "support": round(support, 4),
                "resistance": round(resistance, 4),
                "macd_dif": round(macd_dif, 4),
                "macd_dea": round(macd_dea, 4),
                "prev_close": round(float(close.iloc[-2]), 4) if len(close) >= 2 else latest_close,
            }
            self._cache[symbol] = (now, result)
            return result
        except Exception:
            return None

    def check_holdings(self) -> List[Dict[str, Any]]:
        with Storage() as db:
            holdings = db.get_open_holdings()
            if not holdings:
                return []
            results = []
            for h in holdings:
                ind = self._fetch_indicators(h["symbol"])
                if not ind:
                    results.append({
                        "id": h["id"],
                        "symbol": h["symbol"],
                        "name": h["name"],
                        "buy_date": h["buy_date"],
                        "buy_price": h["buy_price"],
                        "shares": h["shares"],
                        "cost_price": h.get("cost_price") or h["buy_price"],
                        "current_price": None,
                        "signals": [],
                        "pnl": None,
                        "pnl_pct": None,
                        "market_value": None,
                        "error": "行情拉取失败",
                    })
                    continue
                current = ind["close"]
                market_value = current * h["shares"]
                cost = (h.get("cost_price") or h["buy_price"]) * h["shares"]
                pnl = market_value - cost
                pnl_pct = (pnl / cost * 100) if cost else 0.0
                signals = self._detect_signals(h, ind)
                rr = 0.0
                downside = current - ind["support"]
                if downside > 0:
                    rr = (ind["resistance"] - current) / downside
                results.append({
                    "id": h["id"],
                    "symbol": h["symbol"],
                    "name": h["name"],
                    "buy_date": h["buy_date"],
                    "buy_price": h["buy_price"],
                    "shares": h["shares"],
                    "cost_price": h.get("cost_price") or h["buy_price"],
                    "current_price": current,
                    "white_line": ind["white_line"],
                    "yellow_line": ind["yellow_line"],
                    "support": ind["support"],
                    "resistance": ind["resistance"],
                    "macd_dif": ind["macd_dif"],
                    "macd_dea": ind["macd_dea"],
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "market_value": round(market_value, 2),
                    "rr": round(rr, 2),
                    "signals": signals,
                    "error": None,
                })
            return results

    def _detect_signals(self, holding: dict, ind: dict) -> List[Dict[str, str]]:
        signals = []
        close = ind["close"]
        buy_price = holding["buy_price"]
        if close < ind["yellow_line"]:
            signals.append({
                "action": "清仓/回避",
                "priority": 1,
                "rule": "破黄线",
                "reason": f"收盘价 {close} 跌破黄线 {ind['yellow_line']}，主力成本线失守",
            })
        if close < ind["white_line"]:
            signals.append({
                "action": "减仓",
                "priority": 2,
                "rule": "破白线",
                "reason": f"收盘价 {close} 跌破白线 {ind['white_line']}，短期趋势破坏",
            })
        if ind["macd_dif"] < 0 and ind["macd_dif"] < ind["macd_dea"]:
            signals.append({
                "action": "警示",
                "priority": 3,
                "rule": "MACD否决",
                "reason": f"MACD DIF {ind['macd_dif']} < DEA {ind['macd_dea']}，空头动能增强",
            })
        stop_loss_price = buy_price * 0.95
        if close < stop_loss_price:
            signals.append({
                "action": "止损/退出",
                "priority": 1,
                "rule": "止损",
                "reason": f"收盘价 {close} 低于买入价 5% 止损线 {stop_loss_price:.2f}",
            })
        if close >= ind["resistance"]:
            signals.append({
                "action": "止盈提示",
                "priority": 4,
                "rule": "止盈",
                "reason": f"收盘价 {close} 触及 20 日最高 {ind['resistance']}，注意止盈",
            })
        signals.sort(key=lambda s: s["priority"])
        return signals
