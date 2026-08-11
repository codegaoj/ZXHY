from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import pandas as pd

from .formula_indicators import evaluate_formulas
from .models import SecuritySnapshot


@dataclass
class WatchSymbol:
    symbol: str
    name: str
    tags: List[str]


def _pick_column(df: pd.DataFrame, candidates: List[str]) -> str:
    for name in candidates:
        if name in df.columns:
            return name
    raise ValueError(f"缺少必要行情字段：{candidates}")


def normalize_daily_frame(raw: pd.DataFrame) -> pd.DataFrame:
    if raw.empty:
        raise ValueError("行情数据为空，无法生成快照。")

    date_col = _pick_column(raw, ["date", "日期", "trade_date"])
    close_col = _pick_column(raw, ["close", "收盘", "收盘价"])
    volume_col = _pick_column(raw, ["volume", "成交量", "vol"])
    high_col = _pick_column(raw, ["high", "最高", "最高价"])
    low_col = _pick_column(raw, ["low", "最低", "最低价"])

    df = raw.copy()
    df = df.rename(columns={
        date_col: "date",
        close_col: "close",
        volume_col: "volume",
        high_col: "high",
        low_col: "low",
    })
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for col in ["close", "volume", "high", "low"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["date", "close", "volume", "high", "low"])
    df = df.sort_values("date").reset_index(drop=True)
    if len(df) < 2:
        raise ValueError("有效行情不足 2 条，无法计算前收盘价。")
    return df


def calculate_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({"macd_dif": dif, "macd_dea": dea})


def build_snapshot(
    symbol: WatchSymbol,
    raw_daily: pd.DataFrame,
    active_market_value_pct: float,
    recent_active_market_values: Optional[List[float]] = None,
) -> SecuritySnapshot:
    df = normalize_daily_frame(raw_daily)
    formula_eval = evaluate_formulas(df)
    macd = calculate_macd(df["close"])
    df = pd.concat([df, macd], axis=1)
    latest = df.iloc[-1]
    prev = df.iloc[-2]
    recent = df.tail(20)

    close = float(latest["close"])
    # 知行短期趋势线：EMA(EMA(C,10),10)
    # 知行多空线：默认使用公式指标层当前参数 MA14/MA28/MA57/MA114 的均值；
    # 如后续补充 M1/M2/M3/M4 参数，只需在 formula_indicators.py 中调整 zxdkx。
    white_line = float(formula_eval.values.get("zxdq", df["close"].ewm(span=10, adjust=False).mean().ewm(span=10, adjust=False).mean().iloc[-1]))
    yellow_line = float(formula_eval.values.get("zxdkx", df["close"].tail(20).mean()))
    support_price = float(recent["low"].min())
    resistance_price = float(recent["high"].max())
    tags = list(dict.fromkeys([*symbol.tags, *formula_eval.tags]))

    return SecuritySnapshot(
        symbol=symbol.symbol,
        name=symbol.name,
        close=round(close, 4),
        prev_close=round(float(prev["close"]), 4),
        volume=round(float(latest["volume"]), 4),
        volume_ma5=round(float(df["volume"].tail(5).mean()), 4),
        active_market_value_pct=round(float(active_market_value_pct), 4),
        macd_dif=round(float(latest["macd_dif"]), 4),
        macd_dea=round(float(latest["macd_dea"]), 4),
        white_line=round(white_line, 4),
        yellow_line=round(yellow_line, 4),
        support_price=round(support_price, 4),
        resistance_price=round(resistance_price, 4),
        tags=tags,
        recent_active_market_values=list(recent_active_market_values) if recent_active_market_values else [],
    )
