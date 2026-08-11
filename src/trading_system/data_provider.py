from __future__ import annotations

import csv
import time
from abc import ABC, abstractmethod
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List

import pandas as pd

from .data_normalizer import WatchSymbol


class MarketDataProvider(ABC):
    @abstractmethod
    def fetch_daily(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        """获取单只股票的日线行情。"""

    @abstractmethod
    def fetch_active_market_value_pct(self) -> float:
        """获取大盘活跃市值或市场热度代理值。"""


class AkShareProvider(MarketDataProvider):
    def __init__(self, adjust: str = "qfq", retries: int = 3, pause_seconds: float = 0.8):
        try:
            import akshare as ak  # type: ignore
        except ImportError as exc:
            raise RuntimeError("未安装 akshare，请先运行：pip install akshare") from exc
        self.ak = ak
        self.adjust = adjust
        self.retries = retries
        self.pause_seconds = pause_seconds

    def fetch_daily(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        pure_symbol = normalize_symbol(symbol, style="plain")
        prefixed_symbol = normalize_symbol(symbol, style="prefixed")
        last_error = None
        for attempt in range(1, self.retries + 1):
            try:
                data = self._fetch_daily_once(pure_symbol, prefixed_symbol, start_date, end_date)
                if data is None or data.empty:
                    raise ValueError(f"AkShare 返回空行情：{symbol}")
                return data
            except Exception as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(self.pause_seconds * attempt)
        raise RuntimeError(f"AkShare 拉取失败：{symbol}，已重试 {self.retries} 次。最后错误：{last_error}") from last_error

    def _fetch_daily_once(self, pure_symbol: str, prefixed_symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        errors = []
        try:
            return self.ak.stock_zh_a_hist(
                symbol=pure_symbol,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust=self.adjust,
                timeout=15,
            )
        except Exception as exc:
            errors.append(f"stock_zh_a_hist: {exc}")

        try:
            return self.ak.stock_zh_a_hist_tx(
                symbol=prefixed_symbol,
                start_date=start_date,
                end_date=end_date,
                adjust=self.adjust,
                timeout=15,
            )
        except Exception as exc:
            errors.append(f"stock_zh_a_hist_tx: {exc}")

        try:
            return self.ak.stock_zh_a_daily(
                symbol=prefixed_symbol,
                start_date=start_date,
                end_date=end_date,
                adjust=self.adjust,
            )
        except Exception as exc:
            errors.append(f"stock_zh_a_daily: {exc}")

        raise RuntimeError("；".join(errors))

    def fetch_active_market_value_pct(self) -> float:
        try:
            spot = self.ak.stock_zh_a_spot_em()
        except Exception:
            return 0.0
        pct_col = "涨跌幅" if "涨跌幅" in spot.columns else None
        amount_col = "成交额" if "成交额" in spot.columns else None
        if not pct_col or not amount_col:
            return 0.0
        data = spot[[pct_col, amount_col]].copy()
        data[pct_col] = pd.to_numeric(data[pct_col], errors="coerce")
        data[amount_col] = pd.to_numeric(data[amount_col], errors="coerce")
        data = data.dropna()
        if data.empty or data[amount_col].sum() == 0:
            return 0.0
        active_amount = data.loc[data[pct_col] > 0, amount_col].sum()
        total_amount = data[amount_col].sum()
        return float(active_amount / total_amount * 10 - 5)


class CsvProvider(MarketDataProvider):
    def __init__(self, demo_snapshot_path: Path):
        self.demo_snapshot_path = demo_snapshot_path
        self._rows = self._load_rows()

    def _load_rows(self) -> Dict[str, dict]:
        rows: Dict[str, dict] = {}
        with self.demo_snapshot_path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                rows[str(row["symbol"]).strip()] = row
        return rows

    def fetch_daily(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        row = self._rows.get(symbol)
        if not row:
            raise ValueError(f"CSV 演示源中找不到股票：{symbol}")
        close = float(row["close"])
        prev_close = float(row["prev_close"])
        volume = float(row["volume"])
        today = date.today()
        records = []
        for i in range(30):
            current_date = today - timedelta(days=29 - i)
            drift = (i - 28) * 0.003
            price = prev_close * (1 + drift) if i < 29 else close
            records.append({
                "date": current_date.isoformat(),
                "close": round(price, 4),
                "high": round(price * 1.025, 4),
                "low": round(price * 0.975, 4),
                "volume": round(volume * (0.8 + i / 100), 4),
            })
        records[-2]["close"] = prev_close
        records[-1]["close"] = close
        return pd.DataFrame(records)

    def fetch_active_market_value_pct(self) -> float:
        values = [float(row["active_market_value_pct"]) for row in self._rows.values()]
        return sum(values) / len(values) if values else 0.0


def normalize_symbol(symbol: str, style: str = "plain") -> str:
    value = str(symbol).strip()
    if "." in value:
        code, suffix = value.split(".", 1)
        if style in {"akshare", "prefixed"}:
            return f"{suffix.lower()}{code}"
        return code
    if value.lower().startswith(("sh", "sz", "bj")) and len(value) >= 8:
        return value if style in {"akshare", "prefixed"} else value[2:]
    if style in {"akshare", "prefixed"}:
        if value.startswith("6"):
            return f"sh{value}"
        if value.startswith(("0", "3")):
            return f"sz{value}"
        if value.startswith(("4", "8", "9")):
            return f"bj{value}"
    return value


def parse_tags(raw: str) -> List[str]:
    normalized = str(raw).replace("；", ";").replace("，", ";").replace(",", ";").replace("|", ";")
    return [item.strip() for item in normalized.split(";") if item.strip()]


def load_watchlist(path: Path) -> List[WatchSymbol]:
    if not path.exists():
        raise FileNotFoundError(f"自选股池不存在：{path}")
    items: List[WatchSymbol] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            items.append(WatchSymbol(
                symbol=str(row["symbol"]).strip(),
                name=str(row.get("name", row["symbol"])).strip(),
                tags=parse_tags(row.get("tags", "")),
            ))
    if not items:
        raise ValueError("自选股池为空，请至少配置一只股票。")
    return items


def create_provider(config: dict, project_root: Path) -> MarketDataProvider:
    source = str(config.get("source", "csv")).lower()
    if source == "akshare":
        return AkShareProvider(
            adjust=str(config.get("adjust", "qfq")),
            retries=int(config.get("akshare_retries", 3)),
            pause_seconds=float(config.get("akshare_pause_seconds", 0.8)),
        )
    if source == "csv":
        demo_path = project_root / str(config.get("demo_snapshot_path", "data/demo_market_snapshot.csv"))
        return CsvProvider(demo_path)
    raise ValueError(f"暂不支持的数据源：{source}")


def default_date_range(days: int = 180) -> tuple[str, str]:
    end = date.today()
    start = end - timedelta(days=days)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
