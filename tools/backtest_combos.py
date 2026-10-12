#!/usr/bin/env python3
"""震荡环境组合信号回测

测试战法+辅助信号的组合在震荡偏强/震荡中性环境下的表现。
只在同一天同时触发2个信号时才记录为组合交易。

用法:
    python tools/backtest_combos.py
"""
import sys
import os
import csv
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from trading_system.formula_indicators import _sma_cn, _safe_ratio, _cross_up

BACKTEST_DAYS = 486
WARMUP_DAYS = 120
SLIPPAGE = 0.003
STOP_LOSS_PCT = 0.05
TAKE_PROFIT_PCT = 0.10
MAX_HOLD_DAYS = 10
DEFAULT_TOP_N = 500
BATCH_SLEEP = 0.15
USE_RISK_ENGINE = True

COMBOS = [
    ("B1买点", "回踩白线"),
    ("B1买点", "缩量反弹"),
    ("B1买点", "突破压力"),
    ("B1买点", "砖型图反红"),
    ("单针下20", "回踩白线"),
    ("单针下20", "突破压力"),
    ("单针下20", "缩量反弹"),
    ("砖型图反红", "回踩白线"),
    ("砖型图反红", "缩量反弹"),
    ("砖型图反红", "突破压力"),
    ("回踩白线", "缩量反弹"),
    ("回踩白线", "突破压力"),
    ("缩量反弹", "突破压力"),
    ("B1买点", "锤头线"),
    ("单针下20", "锤头线"),
]

REGIMES = ["多头", "震荡偏强", "震荡中性", "震荡偏弱", "空头"]


def load_watchlist(top_n=DEFAULT_TOP_N):
    csv_path = PROJECT_ROOT / "data" / "watchlist_all_a_ex_300_688.csv"
    rows = []
    with open(csv_path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            symbol = r.get("symbol", "").strip().lstrip("\ufeff")
            name = r.get("name", "").strip()
            if symbol and name:
                if symbol.startswith("920") or symbol.startswith("8"):
                    continue
                rows.append({"symbol": symbol, "name": name})
    return rows[:top_n]


def fetch_daily(symbol, total_days):
    import akshare as ak
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=int(total_days * 1.5) + 180)).strftime("%Y%m%d")
    if symbol.startswith("6"):
        sina_symbol = f"sh{symbol}"
    else:
        sina_symbol = f"sz{symbol}"
    for attempt in range(2):
        try:
            df = ak.stock_zh_a_daily(symbol=sina_symbol, start_date=start_date, end_date=end_date, adjust="qfq")
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


def fetch_index_data():
    import akshare as ak
    try:
        df = ak.stock_zh_index_daily(symbol="sh000300")
        if df is None or df.empty:
            return None
        col_map = {"date": "date", "open": "open", "close": "close", "high": "high", "low": "low"}
        df = df.rename(columns=col_map)
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        return df
    except Exception:
        return None


def classify_regime(hs300_df):
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


def compute_indicators(df):
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)
    open_price = df["open"].astype(float) if "open" in df.columns else close.shift(1).fillna(close)

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

    b1 = (j < 15) & trend_ok
    b1_shifted = b1.shift(1, fill_value=False)
    for i in range(2, 6):
        b1_shifted = b1_shifted | b1.shift(i, fill_value=False)
    pct_change = close.pct_change() * 100
    no_upper = ((high - close) <= (close - low) * 0.5) | ((high - close) <= (high - low) * 0.3)
    vol_increase = volume > volume.shift(1)
    b2 = b1_shifted & (pct_change > 4) & vol_increase & (j < 55) & trend_ok & no_upper

    vol_avg_5 = volume.rolling(5, min_periods=1).mean().shift(1)
    vol_ratio_to_prev = volume / volume.shift(1)
    vol_shrink_count = (vol_ratio_to_prev < 0.9).rolling(5, min_periods=1).sum().shift(1)

    shrink_rebound = (vol_shrink_count >= 3) & (volume > vol_avg_5 * 1.2) & (pct_change > 0) & trend_ok
    body_pct = _safe_ratio(close - open_price, open_price) * 100
    upper_shadow_ratio = _safe_ratio(high - close, high - low)
    key_long_yang = (body_pct > 3) & (volume > vol_avg_5 * 1.5) & (upper_shadow_ratio < 0.3)

    body_len = (close - open_price).abs()
    candle_min = pd.concat([open_price, close], axis=1).min(axis=1)
    candle_max = pd.concat([open_price, close], axis=1).max(axis=1)
    lower_shadow = candle_min - low
    upper_shadow_k = high - candle_max
    low_5_min = low.rolling(5, min_periods=1).min()
    key_hammer = (lower_shadow > body_len * 2) & (upper_shadow_k < body_len * 0.5) & (low <= low_5_min)

    near_white_prev = _safe_ratio((low.shift(1) - zxdq.shift(1)).abs(), zxdq.shift(1)) < 0.02
    near_white_prev2 = _safe_ratio((low.shift(2) - zxdq.shift(2)).abs(), zxdq.shift(2)) < 0.02
    pullback_white = (near_white_prev | near_white_prev2) & (close > zxdq) & (pct_change > 0) & trend_ok

    resistance_20 = high.rolling(20, min_periods=1).max().shift(1)
    breakout_resistance = (close.shift(1) < resistance_20) & (close > resistance_20) & (volume > vol_avg_5 * 1.2)

    return {
        "B1买点": b1, "B2买点": b2, "单针下20": single_needle,
        "砖型图反红": brick_reversal, "回踩白线": pullback_white,
        "突破压力": breakout_resistance, "放量长阳": key_long_yang,
        "锤头线": key_hammer, "缩量反弹": shrink_rebound,
        "_resistance_20": resistance_20, "_close": close,
        "_open": open_price, "_high": high, "_low": low,
        "_zxdq": zxdq, "_zxdkx": zxdkx, "_prev_low": low.shift(1),
    }


def backtest_stock_combo(symbol, name, df, regime_map, warmup):
    if len(df) < warmup + 20:
        return []
    ind = compute_indicators(df)
    close = ind["_close"]
    open_price = ind["_open"]
    high = ind["_high"]
    low = ind["_low"]
    resistance_20 = ind["_resistance_20"]
    zxdq = ind["_zxdq"]
    zxdkx = ind["_zxdkx"]
    prev_low = ind["_prev_low"]

    trades = []
    for i in range(warmup, len(df) - 1):
        date_str = df["date"].iloc[i].strftime("%Y-%m-%d")
        regime = regime_map.get(date_str, "未知")

        if i + 1 >= len(df):
            continue
        buy_price = open_price.iloc[i + 1]
        if pd.isna(buy_price) or buy_price <= 0:
            continue
        buy_price *= (1 + SLIPPAGE)

        stop_loss = buy_price * (1 - STOP_LOSS_PCT)
        res = resistance_20.iloc[i]
        take_profit = float(res) if (not pd.isna(res) and res > buy_price) else buy_price * (1 + TAKE_PROFIT_PCT)

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

            if day_low <= stop_loss:
                exit_price = stop_loss
                exit_reason = "止损"
                exit_day = j
                break
            if day_high >= take_profit:
                exit_price = take_profit
                exit_reason = "止盈"
                exit_day = j
                break

            if USE_RISK_ENGINE:
                day_white = zxdq.iloc[j]
                day_yellow = zxdkx.iloc[j]
                day_prev_low = prev_low.iloc[j]

                if not pd.isna(day_yellow) and day_close < day_yellow:
                    exit_price = day_close
                    exit_reason = "破黄线"
                    exit_day = j
                    break

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

                if (not pd.isna(day_white) and day_close < day_white
                        and not reduced):
                    realized_ret += position_pct * 0.5 * (day_close / buy_price - 1) * 100
                    position_pct *= 0.5
                    reduced = True
        else:
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

        for s1, s2 in COMBOS:
            sig1 = ind[s1]
            sig2 = ind[s2]
            if i < len(sig1) and i < len(sig2) and bool(sig1.iloc[i]) and bool(sig2.iloc[i]):
                trades.append({
                    "symbol": symbol, "name": name, "date": date_str, "regime": regime,
                    "combo": f"{s1}+{s2}",
                    "buy_price": round(buy_price, 2), "exit_price": round(float(exit_price), 2),
                    "exit_reason": exit_reason, "hold_days": exit_day - i - 1,
                    "return_pct": round(ret, 2), "is_win": is_win,
                    "partial_sell": reduced,
                })
    return trades


def aggregate_results(trades):
    df = pd.DataFrame(trades)
    if df.empty:
        return {}
    results = {}
    combo_names = [f"{s1}+{s2}" for s1, s2 in COMBOS]

    results["overall"] = {}
    for combo in combo_names:
        subset = df[df["combo"] == combo]
        if subset.empty:
            results["overall"][combo] = None
            continue
        wins = subset[subset["is_win"]]
        losses = subset[~subset["is_win"]]
        avg_win = wins["return_pct"].mean() if len(wins) > 0 else 0
        avg_loss = losses["return_pct"].mean() if len(losses) > 0 else 0
        results["overall"][combo] = {
            "count": len(subset), "wins": len(wins),
            "win_rate": round(len(wins) / len(subset) * 100, 1),
            "avg_return": round(subset["return_pct"].mean(), 2),
            "median_return": round(subset["return_pct"].median(), 2),
            "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2),
            "profit_factor": round(abs(avg_win / avg_loss), 2) if avg_loss != 0 else 999.0,
            "max_win": round(subset["return_pct"].max(), 2),
            "max_loss": round(subset["return_pct"].min(), 2),
        }

    results["matrix"] = {}
    for combo in combo_names:
        results["matrix"][combo] = {}
        for regime in REGIMES + ["未知"]:
            subset = df[(df["combo"] == combo) & (df["regime"] == regime)]
            if subset.empty:
                results["matrix"][combo][regime] = None
                continue
            wins = subset[subset["is_win"]]
            results["matrix"][combo][regime] = {
                "count": len(subset),
                "win_rate": round(len(wins) / len(subset) * 100, 1),
                "avg_return": round(subset["return_pct"].mean(), 2),
            }

    results["regime_dist"] = {}
    for regime in REGIMES + ["未知"]:
        results["regime_dist"][regime] = len(df[df["regime"] == regime])
    results["total_trades"] = len(df)
    results["total_symbols"] = df["symbol"].nunique()
    return results


def generate_html_report(results, trades, duration):
    overall = results.get("overall", {})
    matrix = results.get("matrix", {})
    regime_dist = results.get("regime_dist", {})
    total_trades = results.get("total_trades", 0)
    total_symbols = results.get("total_symbols", 0)
    combo_names = [f"{s1}+{s2}" for s1, s2 in COMBOS]

    overall_rows = ""
    for combo in combo_names:
        d = overall.get(combo)
        if d is None:
            overall_rows += f"<tr><td>{combo}</td><td colspan='9' style='color:#999'>无信号</td></tr>"
            continue
        wr_color = "#27ae60" if d["win_rate"] >= 50 else "#e74c3c" if d["win_rate"] < 35 else "#f39c12"
        overall_rows += f"""
        <tr>
            <td><strong>{combo}</strong></td>
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

    matrix_rows = ""
    for combo in combo_names:
        cells = ""
        for regime in REGIMES + ["未知"]:
            d = matrix.get(combo, {}).get(regime)
            if d is None:
                cells += "<td style='background:#f5f5f5;color:#999'>-</td>"
            else:
                wr = d["win_rate"]
                if wr >= 60:
                    bg, fg = "#1a961a", "#fff"
                elif wr >= 50:
                    bg, fg = "#a6d96a", "#333"
                elif wr >= 40:
                    bg, fg = "#fdae61", "#333"
                else:
                    bg, fg = "#d7191c", "#fff"
                cells += (
                    f"<td style='background:{bg};color:{fg}' title='样本{d['count']}'>"
                    f"<div style='font-weight:bold'>{wr}%</div>"
                    f"<div style='font-size:11px'>{d['avg_return']:+.1f}%</div>"
                    f"<div style='font-size:10px;opacity:0.8'>n={d['count']}</div></td>"
                )
        matrix_rows += f"<tr><td><strong>{combo}</strong></td>{cells}</tr>"

    detail_rows = ""
    df_trades = pd.DataFrame(trades)
    if not df_trades.empty:
        df_trades = df_trades.sort_values("date", ascending=False).head(300)
        for _, r in df_trades.iterrows():
            ret_color = "#27ae60" if r["return_pct"] >= 0 else "#e74c3c"
            detail_rows += f"""<tr>
                <td>{r['date']}</td><td>{r['symbol']}</td><td>{r['name']}</td>
                <td><strong>{r['combo']}</strong></td><td>{r['regime']}</td>
                <td>{r['buy_price']:.2f}</td><td>{r['exit_price']:.2f}</td>
                <td>{r['exit_reason']}</td><td>{r['hold_days']}天</td>
                <td style="color:{ret_color};font-weight:bold">{r['return_pct']:+.2f}%</td>
                <td>{'胜' if r['is_win'] else '负'}</td>
            </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>组合信号回测报告</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:-apple-system,"Microsoft YaHei",sans-serif; background:#f5f6fa; color:#333; line-height:1.6; }}
.container {{ max-width:1200px; margin:0 auto; padding:20px; }}
h1 {{ text-align:center; color:#2c3e50; margin:20px 0; font-size:26px; }}
h2 {{ color:#2c3e50; margin:30px 0 12px; font-size:20px; border-bottom:2px solid #9b59b6; padding-bottom:6px; }}
.card {{ background:#fff; border-radius:10px; padding:16px 24px; box-shadow:0 2px 8px rgba(0,0,0,0.08); display:inline-block; margin:4px; min-width:140px; }}
.card .label {{ font-size:13px; color:#888; }} .card .value {{ font-size:24px; font-weight:bold; color:#2c3e50; }}
table {{ width:100%; border-collapse:collapse; background:#fff; border-radius:8px; overflow:hidden; box-shadow:0 2px 8px rgba(0,0,0,0.08); }}
th {{ background:#2c3e50; color:#fff; padding:10px 8px; text-align:center; font-size:13px; }}
td {{ padding:8px 8px; text-align:center; font-size:13px; border-bottom:1px solid #eee; }}
tr:hover {{ background:#f8f9fa; }}
.note {{ font-size:12px; color:#999; margin:8px 0; }}
.detail-table {{ max-height:600px; overflow-y:auto; }}
.detail-table th {{ position:sticky; top:0; z-index:10; }}
</style></head>
<body><div class="container">
    <h1>震荡环境组合信号回测报告</h1>
    <div class="note" style="text-align:center">回测时间: {datetime.now().strftime('%Y-%m-%d %H:%M')} | 耗时: {duration:.1f}秒</div>
    <div style="text-align:center;margin:16px 0">
        <div class="card"><div class="label">回测股票数</div><div class="value">{total_symbols}</div></div>
        <div class="card"><div class="label">总组合交易</div><div class="value">{total_trades}</div></div>
        <div class="card"><div class="label">组合数</div><div class="value">{len(COMBOS)}</div></div>
        <div class="card"><div class="label">回测周期</div><div class="value">{BACKTEST_DAYS}天</div></div>
    </div>
    <h2>一、各组合总体表现</h2>
    <table><thead><tr>
        <th>组合</th><th>样本</th><th>胜率</th><th>平均收益</th><th>中位数</th>
        <th>平均盈利</th><th>平均亏损</th><th>盈亏比</th><th>最大盈利</th><th>最大亏损</th>
    </tr></thead><tbody>{overall_rows}</tbody></table>
    <h2>二、组合×市场环境热力矩阵</h2>
    <table><thead><tr>
        <th>组合</th><th>多头</th><th>震荡偏强</th><th>震荡中性</th><th>震荡偏弱</th><th>空头</th><th>未知</th>
    </tr></thead><tbody>{matrix_rows}</tbody></table>
    <h2>三、交易明细（前300条）</h2>
    <div class="detail-table"><table><thead><tr>
        <th>日期</th><th>代码</th><th>名称</th><th>组合</th><th>环境</th>
        <th>买入价</th><th>卖出价</th><th>出场</th><th>持有</th><th>收益</th><th>胜负</th>
    </tr></thead><tbody>{detail_rows}</tbody></table></div>
</div></body></html>"""
    return html


def main():
    print("=== 震荡环境组合信号回测 ===")
    print(f"股票数: {DEFAULT_TOP_N} | 回测周期: {BACKTEST_DAYS}交易日 | 持有: {MAX_HOLD_DAYS}天")
    print(f"组合数: {len(COMBOS)} | 止损: {STOP_LOSS_PCT*100}% | 止盈: {TAKE_PROFIT_PCT*100}% | 滑点: {SLIPPAGE*100}%")
    print()

    start = time.time()

    print("[1/5] 加载股票池...")
    stocks = load_watchlist()
    print(f"  股票池: {len(stocks)}只")

    print("[2/5] 拉取沪深300指数...")
    hs300 = fetch_index_data()
    if hs300 is None:
        print("  沪深300拉取失败，退出")
        return
    regime_map = classify_regime(hs300)
    print(f"  沪深300数据: {len(hs300)}天")

    print(f"[3/5] 开始回测 {len(stocks)} 只股票...")
    all_trades = []
    success = 0
    fail = 0
    for idx, s in enumerate(stocks, 1):
        df = fetch_daily(s["symbol"], BACKTEST_DAYS)
        if df is None or len(df) < WARMUP_DAYS + 20:
            fail += 1
        else:
            trades = backtest_stock_combo(s["symbol"], s["name"], df, regime_map, WARMUP_DAYS)
            all_trades.extend(trades)
            success += 1
        elapsed = time.time() - start
        remain = elapsed / idx * (len(stocks) - idx) if idx > 0 else 0
        if idx % 10 == 0 or idx == len(stocks):
            print(f"  [{idx}/{len(stocks)}] {s['symbol']} {s['name']} | 已用{elapsed:.0f}s, 预计剩余{remain:.0f}s | 成功{success} 失败{fail} 交易{len(all_trades)}笔")
        time.sleep(BATCH_SLEEP)

    duration = time.time() - start
    print(f"\n  回测完成: 成功{success} 失败{fail} 总交易{len(all_trades)}笔 耗时{duration:.0f}秒")

    print("[4/5] 统计汇总...")
    results = aggregate_results(all_trades)
    if not results:
        print("  无交易数据")
        return

    print()
    print("=" * 80)
    print(f"{'组合':<30} {'样本':>8} {'胜率':>8} {'平均收益':>10} {'盈亏比':>8}")
    print("-" * 80)
    for combo in [f"{s1}+{s2}" for s1, s2 in COMBOS]:
        d = results["overall"].get(combo)
        if d is None:
            print(f"{combo:<30} {'无信号':>8}")
            continue
        print(f"{combo:<30} {d['count']:>8} {d['win_rate']:>7.1f}% {d['avg_return']:>+9.2f}% {d['profit_factor']:>8}")
    print("=" * 80)

    print("\n震荡偏强环境:")
    for combo in [f"{s1}+{s2}" for s1, s2 in COMBOS]:
        d = results["matrix"].get(combo, {}).get("震荡偏强")
        if d and d["count"] > 0:
            print(f"  {combo:<30} n={d['count']:>5}  胜率={d['win_rate']:>5.1f}%  收益={d['avg_return']:>+6.2f}%")
    print("\n震荡中性环境:")
    for combo in [f"{s1}+{s2}" for s1, s2 in COMBOS]:
        d = results["matrix"].get(combo, {}).get("震荡中性")
        if d and d["count"] > 0:
            print(f"  {combo:<30} n={d['count']:>5}  胜率={d['win_rate']:>5.1f}%  收益={d['avg_return']:>+6.2f}%")

    print("\n[5/5] 生成HTML报告...")
    html = generate_html_report(results, all_trades, duration)
    report_path = PROJECT_ROOT / "outputs" / "backtest_combo_report.html"
    raw_path = PROJECT_ROOT / "outputs" / "backtest_combo_raw.json"
    report_path.write_text(html, encoding="utf-8")
    raw_data = {"config": {"top_n": DEFAULT_TOP_N, "backtest_days": BACKTEST_DAYS,
                           "max_hold_days": MAX_HOLD_DAYS, "stop_loss_pct": STOP_LOSS_PCT,
                           "take_profit_pct": TAKE_PROFIT_PCT, "slippage": SLIPPAGE},
                "combos": [list(c) for c in COMBOS],
                "results": results, "trades_count": len(all_trades)}
    raw_path.write_text(json.dumps(raw_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已生成: {report_path}")
    print(f"原始数据: {raw_path}")


if __name__ == "__main__":
    main()
