from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

import pandas as pd


@dataclass
class FormulaEvaluation:
    tags: List[str] = field(default_factory=list)
    signals: Dict[str, bool] = field(default_factory=dict)
    values: Dict[str, float] = field(default_factory=dict)


def _sma_cn(series: pd.Series, n: int, m: int = 1) -> pd.Series:
    """通达信/同花顺风格 SMA：Y=(M*X+(N-M)*Y')/N。"""
    result = []
    prev = None
    for value in series.astype(float):
        if pd.isna(value):
            result.append(prev if prev is not None else 0.0)
            continue
        current = value if prev is None else (m * value + (n - m) * prev) / n
        result.append(current)
        prev = current
    return pd.Series(result, index=series.index)


def _cross_up(left: pd.Series, right: pd.Series) -> pd.Series:
    return (left > right) & (left.shift(1) <= right.shift(1))


def _safe_ratio(numerator: pd.Series, denominator: pd.Series, default: float = 0.0) -> pd.Series:
    denominator = denominator.replace(0, pd.NA)
    return (numerator / denominator).fillna(default)


def _bars_since_previous_true(series: pd.Series) -> int | None:
    if len(series) <= 1:
        return None
    previous = series.iloc[:-1]
    hit_positions = previous[previous].index
    if len(hit_positions) == 0:
        return None
    last_hit = hit_positions[-1]
    return int(series.index[-1] - last_hit)


def _resolve_open(df: pd.DataFrame, close: pd.Series) -> pd.Series:
    """获取开盘价：优先 open / 开盘 / 开盘价；缺失时用前收盘价近似，避免缺字段报错。"""
    for col in ("open", "开盘", "开盘价"):
        if col in df.columns:
            return df[col].astype(float)
    return close.shift(1).fillna(close)


def _last_float(series: pd.Series, default: float = -1.0) -> float:
    """安全取序列末值用于调试：空序列或 NaN 时返回 default。"""
    if len(series) == 0:
        return default
    value = series.iloc[-1]
    if value is None or pd.isna(value):
        return default
    return round(float(value), 4)


def evaluate_formulas(df: pd.DataFrame) -> FormulaEvaluation:
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)

    tags: List[str] = []
    signals: Dict[str, bool] = {}
    values: Dict[str, float] = {}

    ma3 = close.rolling(3, min_periods=1).mean()
    ma6 = close.rolling(6, min_periods=1).mean()
    ma12 = close.rolling(12, min_periods=1).mean()
    ma24 = close.rolling(24, min_periods=1).mean()
    ma60 = close.rolling(60, min_periods=1).mean()
    bbi = (ma3 + ma6 + ma12 + ma24) / 4

    zxdq = close.ewm(span=10, adjust=False).mean().ewm(span=10, adjust=False).mean()
    zxdkx = (
        close.rolling(14, min_periods=1).mean()
        + close.rolling(28, min_periods=1).mean()
        + close.rolling(57, min_periods=1).mean()
        + close.rolling(114, min_periods=1).mean()
    ) / 4
    trend_ok = (close > zxdkx) & (zxdq > zxdkx)

    rng = high.rolling(9, min_periods=1).max() - low.rolling(9, min_periods=1).min()
    rsv = _safe_ratio(close - low.rolling(9, min_periods=1).min(), rng, 0.5) * 100
    rsv = rsv.mask(rng == 0, 50)
    k = _sma_cn(rsv, 3, 1)
    d = _sma_cn(k, 3, 1)
    j = 3 * k - 2 * d

    b1 = (j < 15) & trend_ok
    b1_days = _bars_since_previous_true(b1)
    pct_change = close.pct_change() * 100
    no_upper = ((high - close) <= (close - low) * 0.5) | ((high - close) <= (high - low) * 0.3)
    b2 = (
        b1_days is not None
        and 0 < b1_days <= 5
        and bool(pct_change.iloc[-1] > 4)
        and bool(volume.iloc[-1] > volume.iloc[-2])
        and bool(j.iloc[-1] < 55)
        and bool(trend_ok.iloc[-1])
        and bool(no_upper.iloc[-1])
    )

    short = _safe_ratio(close - low.rolling(3, min_periods=1).min(), close.rolling(3, min_periods=1).max() - low.rolling(3, min_periods=1).min()) * 100
    mid = _safe_ratio(close - low.rolling(10, min_periods=1).min(), close.rolling(10, min_periods=1).max() - low.rolling(10, min_periods=1).min()) * 100
    mid_long = _safe_ratio(close - low.rolling(21, min_periods=1).min(), close.rolling(21, min_periods=1).max() - low.rolling(21, min_periods=1).min()) * 100
    long = _safe_ratio(close - low.rolling(31, min_periods=1).min(), close.rolling(31, min_periods=1).max() - low.rolling(31, min_periods=1).min()) * 100

    four_zero = (short <= 6) & (mid <= 6) & (mid_long <= 6) & (long <= 6)
    white_under_20 = (short <= 20) & (long >= 70)
    white_cross_red = _cross_up(short, long) & (long < 20)
    white_cross_yellow = _cross_up(short, mid) & (mid < 30)
    single_needle = four_zero | white_under_20 | white_cross_red | white_cross_yellow

    var1a = _safe_ratio(high.rolling(4, min_periods=1).max() - close, high.rolling(4, min_periods=1).max() - low.rolling(4, min_periods=1).min()) * 100 - 90
    var2a = _sma_cn(var1a, 4, 1) + 100
    var3a = _safe_ratio(close - low.rolling(4, min_periods=1).min(), high.rolling(4, min_periods=1).max() - low.rolling(4, min_periods=1).min()) * 100
    var4a = _sma_cn(var3a, 6, 1)
    var5a = _sma_cn(var4a, 6, 1) + 100
    var6a = var5a - var2a
    brick = (var6a - 4).where(var6a > 4, 0)
    aa = brick.shift(1) < brick
    cc = (~aa.shift(1).fillna(False)) & aa
    brick_reversal = cc

    signals["B1买点"] = bool(b1.iloc[-1])
    signals["B2买点"] = bool(b2)
    signals["单针下20"] = bool(single_needle.iloc[-1])
    signals["砖型图反红"] = bool(brick_reversal.iloc[-1])
    signals["知行趋势达标"] = bool(trend_ok.iloc[-1])
    signals["BBI_MA60多头"] = bool(close.iloc[-1] > bbi.iloc[-1] and close.iloc[-1] > ma60.iloc[-1])

    # ---- 实战信号标签（牛市增强）----
    open_price = _resolve_open(df, close)

    # 公共量能变量
    vol_avg_5 = volume.rolling(5, min_periods=1).mean().shift(1)  # 前5日平均成交量（昨日视角）
    vol_ratio_to_prev = volume / volume.shift(1)  # 今日量 / 昨日量
    # 过去5日（截至昨日）成交量萎缩（< 前一日*0.9）的天数
    vol_shrink_count = (vol_ratio_to_prev < 0.9).rolling(5, min_periods=1).sum().shift(1)

    # 1. 滴滴战法：缩量回调后放量反弹
    didi = (
        (vol_shrink_count >= 3)  # 过去5日至少3天缩量
        & (volume > vol_avg_5 * 1.2)  # 今日放量
        & (pct_change > 0)  # 今日上涨
        & trend_ok  # 知行趋势达标
    )

    # 2. 关键K线-放量长阳
    body_pct = _safe_ratio(close - open_price, open_price) * 100  # 实体涨幅(%)
    upper_shadow_ratio = _safe_ratio(high - close, high - low)  # 上影线占比
    key_long_yang = (
        (body_pct > 3)  # 实体涨幅 > 3%
        & (volume > vol_avg_5 * 1.5)  # 量比前5日均量放大1.5倍
        & (upper_shadow_ratio < 0.3)  # 上影线短
    )

    # 3. 关键K线-锤头线
    body_len = (close - open_price).abs()
    candle_min = pd.concat([open_price, close], axis=1).min(axis=1)
    candle_max = pd.concat([open_price, close], axis=1).max(axis=1)
    lower_shadow = candle_min - low
    upper_shadow = high - candle_max
    low_5_min = low.rolling(5, min_periods=1).min()
    key_hammer = (
        (lower_shadow > body_len * 2)  # 下影线 > 实体2倍
        & (upper_shadow < body_len * 0.5)  # 上影线 < 实体0.5倍
        & (low <= low_5_min)  # 今日最低创近5日新低
    )

    # 4. 回踩白线反弹
    near_white_prev = _safe_ratio((low.shift(1) - zxdq.shift(1)).abs(), zxdq.shift(1)) < 0.02
    near_white_prev2 = _safe_ratio((low.shift(2) - zxdq.shift(2)).abs(), zxdq.shift(2)) < 0.02
    pullback_white = (
        (near_white_prev | near_white_prev2)  # 昨日或前日低点回踩白线
        & (close > zxdq)  # 今日站上白线
        & (pct_change > 0)  # 今日上涨
        & trend_ok  # 知行趋势达标
    )

    # 5. 突破压力位（近20日最高价作为压力位）
    resistance_20 = high.rolling(20, min_periods=1).max().shift(1)  # 昨日视角的20日最高
    breakout_resistance = (
        (close.shift(1) < resistance_20)  # 昨日收盘 < 压力位
        & (close > resistance_20)  # 今日收盘 > 压力位
        & (volume > vol_avg_5 * 1.2)  # 今日放量
    )

    signals["滴滴战法"] = bool(didi.iloc[-1])
    signals["放量长阳"] = bool(key_long_yang.iloc[-1])
    signals["锤头线"] = bool(key_hammer.iloc[-1])
    signals["回踩白线"] = bool(pullback_white.iloc[-1])
    signals["突破压力"] = bool(breakout_resistance.iloc[-1])

    if signals["B1买点"] or signals["B2买点"]:
        tags.append("B1/B2买点")
    if signals["单针下20"]:
        tags.append("单针战法")
    if signals["砖型图反红"]:
        tags.append("砖形图")
    if signals["知行趋势达标"]:
        tags.append("双线/白黄线")
    if signals["BBI_MA60多头"]:
        tags.append("BBI/MA60")

    if signals["滴滴战法"]:
        tags.append("滴滴战法")
    if signals["放量长阳"]:
        tags.append("放量长阳")
    if signals["锤头线"]:
        tags.append("锤头线")
    if signals["回踩白线"]:
        tags.append("回踩白线")
    if signals["突破压力"]:
        tags.append("突破压力")

    values.update({
        "j": round(float(j.iloc[-1]), 4),
        "zxdq": round(float(zxdq.iloc[-1]), 4),
        "zxdkx": round(float(zxdkx.iloc[-1]), 4),
        "bbi": round(float(bbi.iloc[-1]), 4),
        "ma60": round(float(ma60.iloc[-1]), 4),
        "single_short": round(float(short.iloc[-1]), 4),
        "single_mid": round(float(mid.iloc[-1]), 4),
        "single_mid_long": round(float(mid_long.iloc[-1]), 4),
        "single_long": round(float(long.iloc[-1]), 4),
        "brick": round(float(brick.iloc[-1]), 4),
        "b1_days": float(b1_days) if b1_days is not None else -1.0,
        "vol_avg_5": _last_float(vol_avg_5),
        "vol_shrink_count": _last_float(vol_shrink_count),
        "body_pct": _last_float(body_pct),
        "upper_shadow_ratio": _last_float(upper_shadow_ratio),
        "lower_shadow": _last_float(lower_shadow),
        "upper_shadow": _last_float(upper_shadow),
        "resistance_20": _last_float(resistance_20),
    })

    return FormulaEvaluation(tags=tags, signals=signals, values=values)
