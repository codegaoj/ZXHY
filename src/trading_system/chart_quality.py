"""K 线形态质量评分模块。

基于交易实战经验，对最近一根 K 线所处的形态进行 0-100 分的质量评分。

评分逻辑：
    最终分数 = 50 + 好形态加分(上限70) - 坏形态减分(下限40)，并 clamp 到 [0, 100]
    grade: >=80 优秀 / 60-79 良好 / 40-59 一般 / <40 较差
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .formula_indicators import _resolve_open, _safe_ratio


@dataclass
class ChartQualityResult:
    score: int  # 0-100
    grade: str  # 优秀(>=80)/良好(60-79)/一般(40-59)/较差(<40)
    good_points: list[str]  # 命中的好形态描述
    bad_points: list[str]  # 命中的坏形态描述


# 放量倍数阈值
_VOL_SURGE_RATIO = 1.2  # 通用"今日放量"阈值
_VOL_STRONG_RATIO = 1.5  # 放量滞涨/高位放量 强放量阈值
_VOL_HUGE_RATIO = 2.0  # 巨量阈值

# 好形态加分上限 / 坏形态减分下限
_GOOD_CAP = 70
_BAD_CAP = 40
_BASE_SCORE = 50


def _grade_of(score: int) -> str:
    if score >= 80:
        return "优秀"
    if score >= 60:
        return "良好"
    if score >= 40:
        return "一般"
    return "较差"


def _last(series: pd.Series, default: float = 0.0) -> float:
    """安全取序列末值：空序列或 NaN 时返回 default。"""
    if series is None or len(series) == 0:
        return default
    value = series.iloc[-1]
    if value is None or pd.isna(value):
        return default
    return float(value)


def evaluate_chart_quality(df) -> ChartQualityResult:
    """
    输入：pandas DataFrame，包含 date, open, close, high, low, volume 列
    输出：ChartQualityResult
    """
    # ---- 数据不足保护 ----
    if df is None or len(df) < 20:
        return ChartQualityResult(score=50, grade="一般", good_points=[], bad_points=[])

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)
    open_price = _resolve_open(df, close)

    # ---- 基础指标（向量化计算）----
    ma5 = close.rolling(5, min_periods=1).mean()
    ma10 = close.rolling(10, min_periods=1).mean()
    ma20 = close.rolling(20, min_periods=1).mean()

    pct_change = close.pct_change()
    # 前5日均量（昨日视角，不含今日），用于判断"放量"
    vol_ma5_prev = volume.rolling(5, min_periods=1).mean().shift(1)

    # 实体 / 影线
    body = (close - open_price).abs()
    candle_min = pd.concat([open_price, close], axis=1).min(axis=1)
    candle_max = pd.concat([open_price, close], axis=1).max(axis=1)
    upper_shadow = high - candle_max
    lower_shadow = candle_min - low
    full_range = high - low

    # ---- 末值快照 ----
    c = _last(close)
    o = _last(open_price)
    h = _last(high)
    l = _last(low)
    v = _last(volume)
    ma5_v = _last(ma5)
    ma10_v = _last(ma10)
    ma20_v = _last(ma20)
    today_pct = _last(pct_change, 0.0)
    vol_ma5_prev_v = _last(vol_ma5_prev, v)
    body_v = _last(body)
    upper_shadow_v = _last(upper_shadow)
    lower_shadow_v = _last(lower_shadow)
    range_v = max(h - l, 0.0)

    is_yang = c > o
    is_yin = c < o

    # 今日放量（量比前5日均量放大）
    today_vol_surge = bool(vol_ma5_prev_v > 0 and v > vol_ma5_prev_v * _VOL_SURGE_RATIO)

    good_score = 0
    good_points: list[str] = []
    bad_score = 0
    bad_points: list[str] = []

    # ============================================================
    # 好形态（加分项，总分上限 70）
    # ============================================================

    # 1. 趋势一致性（10分）：MA5 > MA10 > MA20 多头排列
    if ma5_v > ma10_v > ma20_v:
        good_score += 10
        good_points.append(
            f"趋势一致性(10)：MA5({ma5_v:.2f})>MA10({ma10_v:.2f})>MA20({ma20_v:.2f})多头排列"
        )

    # 2. 量价配合（10分）：最近10日上涨日均量 > 下跌日均量，且今日放量
    last10_close = close.iloc[-10:]
    last10_open = open_price.iloc[-10:]
    last10_vol = volume.iloc[-10:]
    up_mask = last10_close > last10_open
    down_mask = last10_close < last10_open
    avg_vol_up = float(last10_vol[up_mask].mean()) if up_mask.any() else 0.0
    avg_vol_down = float(last10_vol[down_mask].mean()) if down_mask.any() else 0.0
    if avg_vol_up > avg_vol_down and today_vol_surge:
        good_score += 10
        good_points.append(
            f"量价配合(10)：近10日上涨日均量({avg_vol_up:.0f})>下跌日均量({avg_vol_down:.0f})，今日放量"
        )

    # 3. K线实体质量（8分）：最近3日平均实体占比 > 50%
    body_ratio = _safe_ratio(body, full_range, 0.0)
    avg_body_ratio_3 = float(body_ratio.iloc[-3:].mean())
    if avg_body_ratio_3 > 0.5:
        good_score += 8
        good_points.append(
            f"K线实体质量(8)：近3日平均实体占比{avg_body_ratio_3 * 100:.1f}%>50%"
        )

    # 4. 位置安全（8分）：收盘价距20日均线偏离度 < 15%
    deviation = abs(c - ma20_v) / ma20_v if ma20_v > 0 else 1.0
    if deviation < 0.15:
        good_score += 8
        good_points.append(f"位置安全(8)：距MA20偏离度{deviation * 100:.1f}%<15%")

    # 5. 上影线控制（8分）：今日上影线 < 实体的50%
    if body_v > 0 and upper_shadow_v < body_v * 0.5:
        good_score += 8
        good_points.append(
            f"上影线控制(8)：上影线({upper_shadow_v:.2f})<实体50%({body_v * 0.5:.2f})"
        )

    # 6. 连阳节奏（8分）：最近5日中阳线 >= 3根
    last5_yang = int((close.iloc[-5:] > open_price.iloc[-5:]).sum())
    if last5_yang >= 3:
        good_score += 8
        good_points.append(f"连阳节奏(8)：近5日阳线{last5_yang}根>=3")

    # 7. 缩量回调后放量（8分）：前3日缩量 + 今日放量
    prev3_vol_avg = float(volume.iloc[-4:-1].mean())  # 今日之前3日均量
    shrink = vol_ma5_prev_v > 0 and prev3_vol_avg < vol_ma5_prev_v * 0.9
    if shrink and today_vol_surge:
        good_score += 8
        good_points.append(
            f"缩量回调后放量(8)：前3日均量({prev3_vol_avg:.0f})缩量，今日放量"
        )

    # 8. 站上关键均线（5分）：收盘 > MA5 > MA10
    if c > ma5_v > ma10_v:
        good_score += 5
        good_points.append(
            f"站上关键均线(5)：收盘({c:.2f})>MA5({ma5_v:.2f})>MA10({ma10_v:.2f})"
        )

    # 9. 小步攀升（5分）：最近5日涨幅分布在0-5%之间（非暴涨暴跌）
    last5_pct = pct_change.iloc[-5:].dropna()
    if len(last5_pct) == 5 and (last5_pct >= 0).all() and (last5_pct <= 0.05).all():
        good_score += 5
        good_points.append("小步攀升(5)：近5日单日涨幅均在0-5%之间")

    # 10. 下影线支撑（5分）：今日有下影线，下影线长度 > 实体的30%
    if lower_shadow_v > 0 and body_v > 0 and lower_shadow_v > body_v * 0.3:
        good_score += 5
        good_points.append(
            f"下影线支撑(5)：下影线({lower_shadow_v:.2f})>实体30%({body_v * 0.3:.2f})"
        )

    good_score = min(good_score, _GOOD_CAP)

    # ============================================================
    # 坏形态（减分项，总减分下限 40）
    # ============================================================

    # 1. 长上影线（-10分）：今日上影线 > 实体的1.5倍（十字星用全振幅衡量）
    if range_v > 0:
        if body_v > 0:
            long_upper = upper_shadow_v > body_v * 1.5
        else:
            # 十字星：上影线占全振幅>40%视为长上影
            long_upper = upper_shadow_v > range_v * 0.4
    else:
        long_upper = False
    if long_upper:
        bad_score += 10
        bad_points.append(
            f"长上影线(-10)：上影线({upper_shadow_v:.2f})>实体1.5倍({body_v * 1.5:.2f})"
        )

    # 2. 放量滞涨（-8分）：今日成交量 > 5日均量1.5倍，但涨幅 < 1%
    if vol_ma5_prev_v > 0 and v > vol_ma5_prev_v * _VOL_STRONG_RATIO and today_pct < 0.01:
        bad_score += 8
        bad_points.append(
            f"放量滞涨(-8)：量比5日均量1.5倍但涨幅{today_pct * 100:.2f}%<1%"
        )

    # 3. 连续阴线（-8分）：最近3日中阴线 >= 2根
    last3_yin = int((close.iloc[-3:] < open_price.iloc[-3:]).sum())
    if last3_yin >= 2:
        bad_score += 8
        bad_points.append(f"连续阴线(-8)：近3日阴线{last3_yin}根>=2")

    # 4. 高位放量（-8分）：收盘价 > 20日均线1.15倍 且今日放量
    if ma20_v > 0 and c > ma20_v * 1.15 and today_vol_surge:
        bad_score += 8
        bad_points.append(
            f"高位放量(-8)：收盘距MA20超15%(偏离{(c / ma20_v - 1) * 100:.1f}%)且今日放量"
        )

    # 5. 跌破5日均线（-5分）：今日收盘 < 5日均线
    if c < ma5_v:
        bad_score += 5
        bad_points.append(f"跌破5日均线(-5)：收盘({c:.2f})<MA5({ma5_v:.2f})")

    # 6. 暴涨风险（-5分）：今日涨幅 > 8%
    if today_pct > 0.08:
        bad_score += 5
        bad_points.append(f"暴涨风险(-5)：今日涨幅{today_pct * 100:.2f}%>8%")

    # 7. 巨量阴线（-5分）：今日收阴且成交量 > 5日均量2倍
    if is_yin and vol_ma5_prev_v > 0 and v > vol_ma5_prev_v * _VOL_HUGE_RATIO:
        bad_score += 5
        bad_points.append("巨量阴线(-5)：收阴且量比5日均量2倍")

    # 8. 缺口过大（-5分）：今日开盘缺口 > 3%
    if len(close) >= 2:
        prev_close = float(close.iloc[-2])
        gap = abs(o - prev_close) / prev_close if prev_close > 0 else 0.0
        if gap > 0.03:
            bad_score += 5
            bad_points.append(f"缺口过大(-5)：开盘缺口{gap * 100:.2f}%>3%")

    bad_score = min(bad_score, _BAD_CAP)

    # ============================================================
    # 综合评分
    # ============================================================
    raw_score = _BASE_SCORE + good_score - bad_score
    score = int(max(0, min(100, raw_score)))
    grade = _grade_of(score)

    return ChartQualityResult(
        score=score,
        grade=grade,
        good_points=good_points,
        bad_points=bad_points,
    )
