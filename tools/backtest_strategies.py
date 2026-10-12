#!/usr/bin/env python3
"""独立战法回测工具

从历史行情出发，用每个战法的公式独立扫描近一年的全市场，
统计各战法在不同市场环境下的胜率、收益和盈亏比。

用法:
    python tools/backtest_strategies.py            # 默认回测近2年，500只股票
    python tools/backtest_strategies.py --all      # 全市场3520只（耗时很长）
    python tools/backtest_strategies.py --top 800  # 指定股票数
"""
import sys
import os
import csv
import json
import time
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict
from typing import List, Dict, Tuple, Optional

import pandas as pd
import numpy as np

# 路径设置
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from trading_system.formula_indicators import evaluate_formulas, _sma_cn, _safe_ratio, _cross_up

# =====================================================================
# 配置
# =====================================================================
BACKTEST_DAYS = 486        # 近2年交易日
WARMUP_DAYS = 120          # 指标预热期（不参与回测）
SLIPPAGE = 0.003           # 滑点 0.3%
STOP_LOSS_PCT = 0.05       # 止损 5%
TAKE_PROFIT_PCT = 0.10     # 止盈 10%（无resistance时）
MAX_HOLD_DAYS = 10         # 最大持有天数
DEFAULT_TOP_N = 500        # 默认回测股票数
BATCH_SLEEP = 0.15         # AkShare请求间隔
USE_RISK_ENGINE = True     # 启用风控引擎卖出逻辑（破白线减仓/破黄线清仓/滴滴战法）

STRATEGIES = [
    "B1买点", "B2买点", "单针下20", "砖型图反红",
    "回踩白线", "突破压力", "放量长阳", "锤头线", "缩量反弹"
]

REGIMES = ["多头", "震荡偏强", "震荡中性", "震荡偏弱", "空头"]

# =====================================================================
# 数据拉取
# =====================================================================
def load_watchlist(top_n: int = DEFAULT_TOP_N) -> List[Dict]:
    """加载股票池，取前top_n只。"""
    csv_path = PROJECT_ROOT / "data" / "watchlist_all_a_ex_300_688.csv"
    rows = []
    with open(csv_path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            symbol = r.get("symbol", "").strip().lstrip("\ufeff")
            name = r.get("name", "").strip()
            if symbol and name:
                # 排除北交所(920/8开头)、退市等无法拉取的股票
                if symbol.startswith("920") or symbol.startswith("8"):
                    continue
                rows.append({"symbol": symbol, "name": name})
    return rows[:top_n]


def fetch_daily(symbol: str, total_days: int) -> Optional[pd.DataFrame]:
    """拉取足够长度的日线数据（使用新浪源）。"""
    import akshare as ak
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=int(total_days * 1.5) + 180)).strftime("%Y%m%d")

    # 新浪源需要 sh/sz 前缀
    if symbol.startswith("6"):
        sina_symbol = f"sh{symbol}"
    else:
        sina_symbol = f"sz{symbol}"

    for attempt in range(2):
        try:
            df = ak.stock_zh_a_daily(
                symbol=sina_symbol,
                start_date=start_date,
                end_date=end_date,
                adjust="qfq"
            )
            if df is None or df.empty:
                return None
            for c in ["open", "close", "high", "low", "volume"]:
                if c in df.columns:
                    df[c] = pd.to_numeric(df[c], errors="coerce")
            df["date"] = pd.to_datetime(df["date"])
            df = df.dropna(subset=["close", "high", "low"]).sort_values("date").reset_index(drop=True)
            return df
        except Exception:
            if attempt == 0:
                time.sleep(1)
            else:
                return None
    return None


def fetch_index_data() -> Optional[pd.DataFrame]:
    """拉取沪深300指数日线，用于判断市场环境。"""
    import akshare as ak
    try:
        df = ak.stock_zh_index_daily(symbol="sh000300")
        if df is None or df.empty:
            return None
        col_map = {"date": "date", "open": "open", "close": "close",
                   "high": "high", "low": "low"}
        df = df.rename(columns=col_map)
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        return df
    except Exception:
        return None


# =====================================================================
# 指标计算（向量化，输出每日Series）
# =====================================================================
def compute_indicators(df: pd.DataFrame) -> Dict[str, pd.Series]:
    """计算所有战法需要的指标，返回每日Series。"""
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)
    open_price = df["open"].astype(float) if "open" in df.columns else close.shift(1).fillna(close)

    # 均线
    ma3 = close.rolling(3, min_periods=1).mean()
    ma6 = close.rolling(6, min_periods=1).mean()
    ma12 = close.rolling(12, min_periods=1).mean()
    ma24 = close.rolling(24, min_periods=1).mean()
    ma60 = close.rolling(60, min_periods=1).mean()
    bbi = (ma3 + ma6 + ma12 + ma24) / 4

    # 知行线
    zxdq = close.ewm(span=10, adjust=False).mean().ewm(span=10, adjust=False).mean()
    zxdkx = (
        close.rolling(14, min_periods=1).mean()
        + close.rolling(28, min_periods=1).mean()
        + close.rolling(57, min_periods=1).mean()
        + close.rolling(114, min_periods=1).mean()
    ) / 4
    trend_ok = (close > zxdkx) & (zxdq > zxdkx)

    # KDJ
    rng = high.rolling(9, min_periods=1).max() - low.rolling(9, min_periods=1).min()
    rsv = _safe_ratio(close - low.rolling(9, min_periods=1).min(), rng, 0.5) * 100
    rsv = rsv.mask(rng == 0, 50)
    k = _sma_cn(rsv, 3, 1)
    d = _sma_cn(k, 3, 1)
    j = 3 * k - 2 * d

    # 砖形图
    var1a = _safe_ratio(high.rolling(4, min_periods=1).max() - close,
                        high.rolling(4, min_periods=1).max() - low.rolling(4, min_periods=1).min()) * 100 - 90
    var2a = _sma_cn(var1a, 4, 1) + 100
    var3a = _safe_ratio(close - low.rolling(4, min_periods=1).min(),
                        high.rolling(4, min_periods=1).max() - low.rolling(4, min_periods=1).min()) * 100
    var4a = _sma_cn(var3a, 6, 1)
    var5a = _sma_cn(var4a, 6, 1) + 100
    var6a = var5a - var2a
    brick = (var6a - 4).where(var6a > 4, 0)
    brick_prev = brick.shift(1)
    brick_reversal = (brick_prev < brick) & (brick_prev.shift(1, fill_value=0) >= brick_prev)

    # 单针战法
    short = _safe_ratio(close - low.rolling(3, min_periods=1).min(),
                         close.rolling(3, min_periods=1).max() - low.rolling(3, min_periods=1).min()) * 100
    mid = _safe_ratio(close - low.rolling(10, min_periods=1).min(),
                       close.rolling(10, min_periods=1).max() - low.rolling(10, min_periods=1).min()) * 100
    mid_long = _safe_ratio(close - low.rolling(21, min_periods=1).min(),
                            close.rolling(21, min_periods=1).max() - low.rolling(21, min_periods=1).min()) * 100
    long_ = _safe_ratio(close - low.rolling(31, min_periods=1).min(),
                         close.rolling(31, min_periods=1).max() - low.rolling(31, min_periods=1).min()) * 100
    four_zero = (short <= 6) & (mid <= 6) & (mid_long <= 6) & (long_ <= 6)
    white_under_20 = (short <= 20) & (long_ >= 70)
    white_cross_red = _cross_up(short, long_) & (long_ < 20)
    white_cross_yellow = _cross_up(short, mid) & (mid < 30)
    single_needle = four_zero | white_under_20 | white_cross_red | white_cross_yellow

    # B1/B2
    b1 = (j < 15) & trend_ok
    # B1出现后的天数
    b1_shifted = b1.shift(1, fill_value=False)
    for i in range(2, 6):
        b1_shifted = b1_shifted | b1.shift(i, fill_value=False)
    pct_change = close.pct_change() * 100
    no_upper = ((high - close) <= (close - low) * 0.5) | ((high - close) <= (high - low) * 0.3)
    vol_increase = volume > volume.shift(1)
    b2 = b1_shifted & (pct_change > 4) & vol_increase & (j < 55) & trend_ok & no_upper

    # 量价指标
    vol_avg_5 = volume.rolling(5, min_periods=1).mean().shift(1)
    vol_ratio_to_prev = volume / volume.shift(1)
    vol_shrink_count = (vol_ratio_to_prev < 0.9).rolling(5, min_periods=1).sum().shift(1)

    # 缩量反弹
    shrink_rebound = (vol_shrink_count >= 3) & (volume > vol_avg_5 * 1.2) & (pct_change > 0) & trend_ok

    # 放量长阳
    body_pct = _safe_ratio(close - open_price, open_price) * 100
    upper_shadow_ratio = _safe_ratio(high - close, high - low)
    key_long_yang = (body_pct > 3) & (volume > vol_avg_5 * 1.5) & (upper_shadow_ratio < 0.3)

    # 锤头线
    body_len = (close - open_price).abs()
    candle_min = pd.concat([open_price, close], axis=1).min(axis=1)
    candle_max = pd.concat([open_price, close], axis=1).max(axis=1)
    lower_shadow = candle_min - low
    upper_shadow_k = high - candle_max
    low_5_min = low.rolling(5, min_periods=1).min()
    key_hammer = (lower_shadow > body_len * 2) & (upper_shadow_k < body_len * 0.5) & (low <= low_5_min)

    # 回踩白线
    near_white_prev = _safe_ratio((low.shift(1) - zxdq.shift(1)).abs(), zxdq.shift(1)) < 0.02
    near_white_prev2 = _safe_ratio((low.shift(2) - zxdq.shift(2)).abs(), zxdq.shift(2)) < 0.02
    pullback_white = (near_white_prev | near_white_prev2) & (close > zxdq) & (pct_change > 0) & trend_ok

    # 突破压力
    resistance_20 = high.rolling(20, min_periods=1).max().shift(1)
    breakout_resistance = (close.shift(1) < resistance_20) & (close > resistance_20) & (volume > vol_avg_5 * 1.2)

    return {
        "B1买点": b1,
        "B2买点": b2,
        "单针下20": single_needle,
        "砖型图反红": brick_reversal,
        "回踩白线": pullback_white,
        "突破压力": breakout_resistance,
        "放量长阳": key_long_yang,
        "锤头线": key_hammer,
        "缩量反弹": shrink_rebound,
        # 辅助数据
        "_resistance_20": resistance_20,
        "_close": close,
        "_open": open_price,
        "_high": high,
        "_low": low,
        "_zxdq": zxdq,
        "_zxdkx": zxdkx,
        "_prev_low": low.shift(1),
    }


# =====================================================================
# 市场环境判定
# =====================================================================
def classify_regime(hs300_df: pd.DataFrame) -> Dict[str, str]:
    """用沪深300近20日涨跌幅判定市场环境，返回 {date_str: regime}。"""
    close = hs300_df["close"].astype(float)
    ret_20d = (close / close.shift(20) - 1) * 100
    regime_map = {}
    for i in range(len(hs300_df)):
        r = ret_20d.iloc[i]
        date_str = hs300_df["date"].iloc[i].strftime("%Y-%m-%d")
        if pd.isna(r):
            regime_map[date_str] = "未知"
        elif r > 5:
            regime_map[date_str] = "多头"
        elif r > 2:
            regime_map[date_str] = "震荡偏强"
        elif r > -2:
            regime_map[date_str] = "震荡中性"
        elif r > -5:
            regime_map[date_str] = "震荡偏弱"
        else:
            regime_map[date_str] = "空头"
    return regime_map


# =====================================================================
# 回测核心
# =====================================================================
def backtest_stock(
    symbol: str,
    name: str,
    df: pd.DataFrame,
    regime_map: Dict[str, str],
    warmup: int,
) -> List[Dict]:
    """对单只股票回测所有战法，返回交易记录列表。"""
    if len(df) < warmup + 20:
        return []

    indicators = compute_indicators(df)
    close = indicators["_close"]
    open_price = indicators["_open"]
    high = indicators["_high"]
    low = indicators["_low"]
    resistance_20 = indicators["_resistance_20"]
    zxdq = indicators["_zxdq"]
    zxdkx = indicators["_zxdkx"]
    prev_low = indicators["_prev_low"]

    trades = []
    signal_names = [s for s in STRATEGIES if s in indicators]

    for i in range(warmup, len(df) - 1):
        date_str = df["date"].iloc[i].strftime("%Y-%m-%d")
        regime = regime_map.get(date_str, "未知")

        # 次日开盘价作为买入价
        if i + 1 >= len(df):
            continue
        buy_price = open_price.iloc[i + 1]
        if pd.isna(buy_price) or buy_price <= 0:
            continue
        buy_price *= (1 + SLIPPAGE)

        # 止损/止盈线
        stop_loss = buy_price * (1 - STOP_LOSS_PCT)
        # 用resistance_20作为止盈目标，否则用固定10%
        res = resistance_20.iloc[i]
        take_profit = float(res) if (not pd.isna(res) and res > buy_price) else buy_price * (1 + TAKE_PROFIT_PCT)

        # 遍历持有期内每日，判断止损/止盈/风控信号
        position_pct = 1.0
        realized_ret = 0.0
        exit_price = None
        exit_reason = None
        exit_day = None
        reduced = False

        for j in range(i + 2, min(i + 2 + MAX_HOLD_DAYS, len(df))):
            day_low = low.iloc[j]
            day_high = high.iloc[j]
            day_close = close.iloc[j]

            # 1. 初始止损（盘中）
            if day_low <= stop_loss:
                exit_price = stop_loss
                exit_reason = "止损"
                exit_day = j
                break

            # 2. 止盈（盘中）
            if day_high >= take_profit:
                exit_price = take_profit
                exit_reason = "止盈"
                exit_day = j
                break

            if USE_RISK_ENGINE:
                day_white = zxdq.iloc[j]
                day_yellow = zxdkx.iloc[j]
                day_prev_low = prev_low.iloc[j]

                # 3. 破黄线 → 清仓
                if not pd.isna(day_yellow) and day_close < day_yellow:
                    exit_price = day_close
                    exit_reason = "破黄线"
                    exit_day = j
                    break

                # 4. 滴滴战法：跌破前低
                if (not pd.isna(day_prev_low) and day_prev_low > 0
                        and day_close < day_prev_low):
                    if not pd.isna(day_white) and day_close < day_white:
                        exit_price = day_close
                        exit_reason = "滴滴止损"
                        exit_day = j
                        break
                    elif not reduced:
                        realized_ret += position_pct * 0.5 * (day_close / buy_price - 1) * 100
                        position_pct *= 0.5
                        reduced = True

                # 5. 破白线 → 减仓
                if (not pd.isna(day_white) and day_close < day_white
                        and not reduced):
                    realized_ret += position_pct * 0.5 * (day_close / buy_price - 1) * 100
                    position_pct *= 0.5
                    reduced = True
        else:
            # 到期平仓
            exit_day = min(i + 1 + MAX_HOLD_DAYS, len(df) - 1)
            exit_price = close.iloc[exit_day]
            exit_reason = "到期平仓"

        if exit_price is None:
            continue

        if reduced:
            remaining_ret = (exit_price / buy_price - 1) * 100
            ret = realized_ret + position_pct * remaining_ret
        else:
            ret = (exit_price / buy_price - 1) * 100
        is_win = ret > 0

        # 记录每个战法的交易（独立统计）
        for strat in signal_names:
            signal = indicators[strat]
            if i < len(signal) and bool(signal.iloc[i]):
                trades.append({
                    "symbol": symbol,
                    "name": name,
                    "date": date_str,
                    "regime": regime,
                    "strategy": strat,
                    "buy_price": round(buy_price, 2),
                    "exit_price": round(float(exit_price), 2),
                    "exit_reason": exit_reason,
                    "hold_days": exit_day - i - 1,
                    "return_pct": round(ret, 2),
                    "is_win": is_win,
                    "partial_sell": reduced,
                })

    return trades


# =====================================================================
# 统计汇总
# =====================================================================
def aggregate_results(trades: List[Dict]) -> Dict:
    """按战法×市场环境汇总统计。"""
    df = pd.DataFrame(trades)
    if df.empty:
        return {}

    results = {}

    # 1. 各战法总体表现
    results["overall"] = {}
    for strat in STRATEGIES:
        subset = df[df["strategy"] == strat]
        if subset.empty:
            results["overall"][strat] = None
            continue
        wins = subset[subset["is_win"]]
        losses = subset[~subset["is_win"]]
        avg_win = wins["return_pct"].mean() if len(wins) > 0 else 0
        avg_loss = losses["return_pct"].mean() if len(losses) > 0 else 0
        results["overall"][strat] = {
            "count": len(subset),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(subset) * 100, 1),
            "avg_return": round(subset["return_pct"].mean(), 2),
            "median_return": round(subset["return_pct"].median(), 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(abs(avg_win / avg_loss), 2) if avg_loss != 0 else 999.0,
            "max_win": round(subset["return_pct"].max(), 2),
            "max_loss": round(subset["return_pct"].min(), 2),
        }

    # 2. 战法×市场环境矩阵
    results["matrix"] = {}
    for strat in STRATEGIES:
        results["matrix"][strat] = {}
        for regime in REGIMES + ["未知"]:
            subset = df[(df["strategy"] == strat) & (df["regime"] == regime)]
            if subset.empty:
                results["matrix"][strat][regime] = None
                continue
            wins = subset[subset["is_win"]]
            results["matrix"][strat][regime] = {
                "count": len(subset),
                "win_rate": round(len(wins) / len(subset) * 100, 1),
                "avg_return": round(subset["return_pct"].mean(), 2),
            }

    # 3. 市场环境分布
    results["regime_dist"] = {}
    for regime in REGIMES + ["未知"]:
        subset = df[df["regime"] == regime]
        results["regime_dist"][regime] = len(subset)

    # 4. 出场原因分布
    results["exit_reasons"] = {}
    for reason in df["exit_reason"].unique():
        subset = df[df["exit_reason"] == reason]
        results["exit_reasons"][reason] = {
            "count": len(subset),
            "win_rate": round(len(subset[subset["is_win"]]) / len(subset) * 100, 1),
            "avg_return": round(subset["return_pct"].mean(), 2),
        }

    # 5. 减仓统计
    results["partial_sell_count"] = int(df["partial_sell"].sum()) if "partial_sell" in df.columns else 0

    # 6. 总交易数
    results["total_trades"] = len(df)
    results["total_symbols"] = df["symbol"].nunique()

    return results


# =====================================================================
# HTML报告生成
# =====================================================================
def generate_html_report(results: Dict, trades: List[Dict], duration: float) -> str:
    """生成HTML回测报告。"""
    overall = results.get("overall", {})
    matrix = results.get("matrix", {})
    regime_dist = results.get("regime_dist", {})
    exit_reasons = results.get("exit_reasons", {})
    partial_sell_count = results.get("partial_sell_count", 0)
    total_trades = results.get("total_trades", 0)
    total_symbols = results.get("total_symbols", 0)

    # 总览表
    overall_rows = ""
    for strat in STRATEGIES:
        d = overall.get(strat)
        if d is None:
            overall_rows += f"<tr><td>{strat}</td><td colspan='9' style='color:#999'>无信号</td></tr>"
            continue
        # 胜率颜色
        wr_color = "#27ae60" if d["win_rate"] >= 50 else "#e74c3c" if d["win_rate"] < 35 else "#f39c12"
        overall_rows += f"""
        <tr>
            <td><strong>{strat}</strong></td>
            <td>{d['count']}</td>
            <td style="color:{wr_color};font-weight:bold">{d['win_rate']}%</td>
            <td style="color:{'#27ae60' if d['avg_return'] >= 0 else '#e74c3c'}">{d['avg_return']:+.2f}%</td>
            <td>{d['median_return']:+.2f}%</td>
            <td style="color:#27ae60">{d['avg_win']:+.2f}%</td>
            <td style="color:#e74c3c">{d['avg_loss']:+.2f}%</td>
            <td>{d['profit_factor']}</td>
            <td style="color:#27ae60">{d['max_win']:+.2f}%</td>
            <td style="color:#e74c3c">{d['max_loss']:+.2f}%</td>
        </tr>"""

    # 热力矩阵
    matrix_rows = ""
    for strat in STRATEGIES:
        cells = ""
        for regime in REGIMES + ["未知"]:
            d = matrix.get(strat, {}).get(regime)
            if d is None:
                cells += "<td style='background:#f5f5f5;color:#999'>-</td>"
            else:
                wr = d["win_rate"]
                # 胜率热力色
                if wr >= 60:
                    bg = "#1a961a"
                    fg = "#fff"
                elif wr >= 45:
                    bg = "#a6d96a"
                    fg = "#333"
                elif wr >= 35:
                    bg = "#fdae61"
                    fg = "#333"
                else:
                    bg = "#d7191c"
                    fg = "#fff"
                cells += f"""<td style="background:{bg};color:{fg}" title="样本{d['count']}">
                    <div style="font-weight:bold">{wr}%</div>
                    <div style="font-size:11px">{d['avg_return']:+.1f}%</div>
                    <div style="font-size:10px;opacity:0.8">n={d['count']}</div>
                </td>"""
        matrix_rows += f"<tr><td><strong>{strat}</strong></td>{cells}</tr>"

    # 市场环境分布
    regime_cells = ""
    for regime in REGIMES + ["未知"]:
        cnt = regime_dist.get(regime, 0)
        pct = round(cnt / total_trades * 100, 1) if total_trades > 0 else 0
        regime_cells += (
            f"<div style='display:inline-block;margin:8px;padding:10px 16px;border-radius:8px;background:#f0f0f0'>"
            f"<div style='font-size:13px;color:#666'>{regime}</div>"
            f"<div style='font-size:20px;font-weight:bold'>{cnt}</div>"
            f"<div style='font-size:11px;color:#999'>{pct}%</div>"
            f"</div>"
        )

    # 交易明细表（前200条）
    detail_rows = ""
    df_trades = pd.DataFrame(trades)
    if not df_trades.empty:
        df_trades = df_trades.sort_values("date", ascending=False).head(200)
        for _, r in df_trades.iterrows():
            ret_color = "#27ae60" if r["return_pct"] >= 0 else "#e74c3c"
            detail_rows += f"""<tr>
                <td>{r['date']}</td>
                <td>{r['symbol']}</td>
                <td>{r['name']}</td>
                <td><strong>{r['strategy']}</strong></td>
                <td>{r['regime']}</td>
                <td>{r['buy_price']:.2f}</td>
                <td>{r['exit_price']:.2f}</td>
                <td>{r['exit_reason']}</td>
                <td>{r['hold_days']}天</td>
                <td style="color:{ret_color};font-weight:bold">{r['return_pct']:+.2f}%</td>
                <td>{'胜' if r['is_win'] else '负'}</td>
            </tr>"""

    # 出场原因分布
    exit_reason_rows = ""
    for reason, d in sorted(exit_reasons.items(), key=lambda x: -x[1]["count"]):
        pct = round(d["count"] / total_trades * 100, 1) if total_trades > 0 else 0
        wr_color = "#27ae60" if d["win_rate"] >= 50 else "#e74c3c"
        ret_color = "#27ae60" if d["avg_return"] >= 0 else "#e74c3c"
        exit_reason_rows += f"""<tr>
            <td><strong>{reason}</strong></td>
            <td>{d['count']}</td><td>{pct}%</td>
            <td style="color:{wr_color};font-weight:bold">{d['win_rate']}%</td>
            <td style="color:{ret_color}">{d['avg_return']:+.2f}%</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>战法回测报告</title>
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: -apple-system, "Microsoft YaHei", sans-serif; background: #f5f6fa; color: #333; line-height: 1.6; }}
    .container {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
    h1 {{ text-align: center; color: #2c3e50; margin: 20px 0; font-size: 26px; }}
    h2 {{ color: #2c3e50; margin: 30px 0 12px; font-size: 20px; border-bottom: 2px solid #3498db; padding-bottom: 6px; }}
    .summary-cards {{ display: flex; flex-wrap: wrap; gap: 12px; margin: 16px 0; }}
    .card {{ background: #fff; border-radius: 10px; padding: 16px 24px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); min-width: 140px; }}
    .card .label {{ font-size: 13px; color: #888; }}
    .card .value {{ font-size: 24px; font-weight: bold; color: #2c3e50; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
    th {{ background: #2c3e50; color: #fff; padding: 10px 8px; text-align: center; font-size: 13px; }}
    td {{ padding: 8px 8px; text-align: center; font-size: 13px; border-bottom: 1px solid #eee; }}
    tr:hover {{ background: #f8f9fa; }}
    .note {{ font-size: 12px; color: #999; margin: 8px 0; }}
    .detail-table {{ max-height: 600px; overflow-y: auto; }}
    .detail-table th {{ position: sticky; top: 0; z-index: 10; }}
</style>
</head>
<body>
<div class="container">
    <h1>战法独立回测报告</h1>
    <div class="note" style="text-align:center">回测时间: {datetime.now().strftime('%Y-%m-%d %H:%M')} | 耗时: {duration:.1f}秒</div>

    <div class="summary-cards">
        <div class="card"><div class="label">回测股票数</div><div class="value">{total_symbols}</div></div>
        <div class="card"><div class="label">总交易笔数</div><div class="value">{total_trades}</div></div>
        <div class="card"><div class="label">战法数</div><div class="value">{len(STRATEGIES)}</div></div>
        <div class="card"><div class="label">回测周期</div><div class="value">{BACKTEST_DAYS}天</div></div>
        <div class="card"><div class="label">持有天数</div><div class="value">{MAX_HOLD_DAYS}天</div></div>
    </div>

    <h2>一、各战法总体表现</h2>
    <table>
        <thead>
            <tr>
                <th>战法</th>
                <th>样本数</th>
                <th>胜率</th>
                <th>平均收益</th>
                <th>中位数收益</th>
                <th>平均盈利</th>
                <th>平均亏损</th>
                <th>盈亏比</th>
                <th>最大盈利</th>
                <th>最大亏损</th>
            </tr>
        </thead>
        <tbody>{overall_rows}</tbody>
    </table>

    <h2>二、战法 × 市场环境 胜率矩阵</h2>
    <p class="note">单元格颜色: 绿色=高胜率, 红色=低胜率; 下方为平均收益和样本数</p>
    <table>
        <thead>
            <tr>
                <th>战法</th>
                {''.join(f'<th>{r}</th>' for r in REGIMES + ['未知'])}
            </tr>
        </thead>
        <tbody>{matrix_rows}</tbody>
    </table>

    <h2>三、市场环境分布</h2>
    <div>{regime_cells}</div>

    <h2>四、交易明细（近200笔）</h2>
    <table class="detail-table">
        <thead>
            <tr>
                <th>日期</th><th>代码</th><th>名称</th><th>战法</th><th>市场环境</th>
                <th>买入价</th><th>卖出价</th><th>退出原因</th><th>持有天数</th>
                <th>收益</th><th>胜负</th>
            </tr>
        </thead>
        <tbody>{detail_rows}</tbody>
    </table>

    <h2>四、出场原因分布</h2>
    <p class="note">风控引擎卖出: 破黄线=清仓, 破白线=减仓50%, 滴滴止损=跌破前低+白线下方</p>
    <table>
        <thead><tr><th>出场原因</th><th>样本数</th><th>占比</th><th>胜率</th><th>平均收益</th></tr></thead>
        <tbody>{exit_reason_rows}</tbody>
    </table>
    <p class="note">减仓交易数: {partial_sell_count} (占比 {round(partial_sell_count/total_trades*100,1) if total_trades > 0 else 0}%)</p>
</div>
</body>
</html>"""
    return html


# =====================================================================
# 主函数
# =====================================================================
def main():
    parser = argparse.ArgumentParser(description="独立战法回测")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_N, help=f"回测股票数（默认{DEFAULT_TOP_N}）")
    parser.add_argument("--all", action="store_true", help="全市场回测（耗时很长）")
    args = parser.parse_args()

    top_n = 3861 if args.all else args.top

    print(f"=== 战法独立回测 ===")
    print(f"股票数: {top_n} | 回测周期: {BACKTEST_DAYS}交易日 | 持有: {MAX_HOLD_DAYS}天")
    print(f"止损: {STOP_LOSS_PCT*100}% | 止盈: {TAKE_PROFIT_PCT*100}% | 滑点: {SLIPPAGE*100}%")
    print()

    # 1. 加载股票池
    watchlist = load_watchlist(top_n)
    print(f"[1/5] 股票池加载完成: {len(watchlist)}只")

    # 2. 拉取沪深300指数
    print(f"[2/5] 拉取沪深300指数...")
    hs300_df = fetch_index_data()
    if hs300_df is None:
        print("  沪深300拉取失败，使用中性环境")
        regime_map = {}
    else:
        regime_map = classify_regime(hs300_df)
        # 统计各环境天数
        regime_counts = defaultdict(int)
        for v in regime_map.values():
            regime_counts[v] += 1
        print(f"  沪深300数据: {len(hs300_df)}天, 环境分布: {dict(regime_counts)}")

    # 3. 逐股拉取行情 + 计算指标 + 回测
    print(f"[3/5] 开始回测 {len(watchlist)} 只股票...")
    all_trades = []
    success_count = 0
    fail_count = 0
    start_time = time.time()

    for idx, ws in enumerate(watchlist):
        symbol = ws["symbol"]
        # 进度显示
        if (idx + 1) % 10 == 0 or idx == 0:
            elapsed = time.time() - start_time
            eta = elapsed / (idx + 1) * (len(watchlist) - idx - 1) if idx > 0 else 0
            print(f"  [{idx+1}/{len(watchlist)}] {symbol} {ws['name']} | "
                  f"已用{elapsed:.0f}s, 预计剩余{eta:.0f}s | "
                  f"成功{success_count} 失败{fail_count} 交易{len(all_trades)}笔")

        df = fetch_daily(symbol, BACKTEST_DAYS + WARMUP_DAYS)
        if df is None or len(df) < WARMUP_DAYS + 20:
            fail_count += 1
            time.sleep(BATCH_SLEEP)
            continue

        success_count += 1
        try:
            trades = backtest_stock(symbol, ws["name"], df, regime_map, WARMUP_DAYS)
            all_trades.extend(trades)
        except Exception as e:
            pass

        time.sleep(BATCH_SLEEP)

    duration = time.time() - start_time
    print(f"\n  回测完成: 成功{success_count} 失败{fail_count} 总交易{len(all_trades)}笔 耗时{duration:.0f}秒")

    # 4. 统计汇总
    print(f"[4/5] 统计汇总...")
    results = aggregate_results(all_trades)

    # 打印摘要
    print(f"\n{'='*60}")
    print(f"{'战法':<14} {'样本':>6} {'胜率':>8} {'平均收益':>10} {'盈亏比':>8}")
    print(f"{'-'*60}")
    for strat in STRATEGIES:
        d = results.get("overall", {}).get(strat)
        if d:
            print(f"{strat:<14} {d['count']:>6} {d['win_rate']:>7}% {d['avg_return']:>+9.2f}% {d['profit_factor']:>8}")
    print(f"{'='*60}")

    # 5. 生成HTML报告
    print(f"[5/5] 生成HTML报告...")
    html = generate_html_report(results, all_trades, duration)

    output_dir = PROJECT_ROOT / "outputs"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "backtest_report.html"
    output_path.write_text(html, encoding="utf-8")
    print(f"\n报告已生成: {output_path}")

    # 保存原始数据
    json_path = output_dir / "backtest_raw.json"
    json_data = {
        "config": {
            "top_n": top_n,
            "backtest_days": BACKTEST_DAYS,
            "max_hold_days": MAX_HOLD_DAYS,
            "stop_loss_pct": STOP_LOSS_PCT,
            "take_profit_pct": TAKE_PROFIT_PCT,
            "slippage": SLIPPAGE,
        },
        "results": results,
        "trades_count": len(all_trades),
    }
    json_path.write_text(json.dumps(json_data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"原始数据: {json_path}")


if __name__ == "__main__":
    main()
