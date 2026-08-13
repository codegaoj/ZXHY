"""按周/按月汇总复盘。

扫描 outputs/history/ 目录下所有带日期的候选股历史备份，
在指定时间范围内汇总每只候选股的表现，统计胜率、平均收益、最佳/最差标的。

用法：
    python tools/periodic_review.py --period weekly   # 最近7天
    python tools/periodic_review.py --period monthly   # 最近30天
    python tools/periodic_review.py --period all       # 全部历史
    python tools/periodic_review.py --start 2026-08-01 --end 2026-08-13  # 自定义区间
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class CandidateRecord:
    """单只候选股在某天的选入记录。"""
    selected_date: str
    symbol: str
    name: str
    selected_close: float
    pct_change_at_select: float
    tags: str
    candidate_score: float
    sector: str
    support_price: float
    resistance_price: float
    white_line: float
    yellow_line: float


@dataclass
class PerformanceRecord:
    """候选股从选入日到最新交易日的表现。"""
    record: CandidateRecord
    latest_close: float
    latest_date: str
    holding_days: int
    return_pct: float  # 收益率 %
    max_return_pct: float  # 持有期最大收益率
    min_return_pct: float  # 持有期最大亏损率
    broke_white: bool  # 是否破白线
    broke_yellow: bool  # 是否破黄线
    hit_resistance: bool  # 是否触及压力位
    hit_support: bool  # 是否触及支撑位
    status: str  # "盈利" / "亏损" / "平" / "无数据"


@dataclass
class PeriodicStats:
    """周期汇总统计。"""
    period_label: str  # "最近7天" / "最近30天" / "全部历史" / "2026-08-01 ~ 2026-08-13"
    date_range: str
    total_selections: int  # 总选入次数
    unique_stocks: int  # 去重股票数
    win_count: int  # 盈利数
    loss_count: int  # 亏损数
    flat_count: int  # 持平
    no_data_count: int  # 无数据
    win_rate: float  # 胜率 %
    avg_return: float  # 平均收益率
    max_return: float  # 单只最大收益
    min_return: float  # 单只最大亏损
    avg_holding_days: float
    best_stock: str  # 最佳标的
    worst_stock: str  # 最差标的
    sector_performance: dict  # 板块平均收益
    tag_performance: dict  # 标签平均收益
    daily_breakdown: list  # 每日选入胜率
    broke_white_count: int  # 破白线数
    broke_yellow_count: int  # 破黄线数


# ---------------------------------------------------------------------------
# 历史数据加载
# ---------------------------------------------------------------------------


def load_history_files(start_date: Optional[date], end_date: Optional[date]) -> list[tuple[str, Path]]:
    """扫描 history/ 目录，返回 [(date_str, file_path), ...] 按日期升序。"""
    history_dir = PROJECT_ROOT / "outputs" / "history"
    if not history_dir.exists():
        return []

    results = []
    for f in sorted(history_dir.glob("daily_candidates_*.json")):
        stem = f.stem  # daily_candidates_2026-08-12
        date_part = stem.replace("daily_candidates_", "")
        try:
            file_date = date.fromisoformat(date_part)
        except ValueError:
            continue
        if start_date and file_date < start_date:
            continue
        if end_date and file_date > end_date:
            continue
        results.append((date_part, f))

    return results


def load_candidates_from_file(path: Path) -> list[dict]:
    """从历史 JSON 文件加载候选股列表。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("candidates", [])
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 行情数据获取
# ---------------------------------------------------------------------------


def retry(fn, times=3, pause=1.5):
    last = None
    for i in range(times):
        try:
            return fn()
        except Exception as exc:
            last = exc
            time.sleep(pause * (i + 1))
    raise last


def fetch_latest_prices(symbols: set[str]) -> dict[str, dict]:
    """批量获取最新行情，返回 {symbol: {close, date, high, low}}。"""
    try:
        import akshare as ak  # type: ignore
    except ImportError:
        return {}

    result = {}
    # 方法1: 全市场实时行情
    try:
        df = retry(lambda: ak.stock_zh_a_spot(), times=2, pause=2)
        if df is not None and len(df) > 0:
            code_col = "代码" if "代码" in df.columns else None
            if code_col:
                for _, row in df.iterrows():
                    code = str(row[code_col]).strip().zfill(6)
                    if code in symbols:
                        result[code] = {
                            "close": float(row.get("最新价", 0)),
                            "date": date.today().isoformat(),
                            "high": float(row.get("最高", 0)),
                            "low": float(row.get("最低", 0)),
                        }
    except Exception:
        pass

    return result


def fetch_daily_kline(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """获取个股日K线，用于计算持有期最高/最低价和破线情况。"""
    try:
        import akshare as ak  # type: ignore
    except ImportError:
        return pd.DataFrame()

    def prefixed(sym: str) -> str:
        if sym.startswith(("6", "9")):
            return "sh" + sym
        if sym.startswith(("0", "2", "3")):
            return "sz" + sym
        if sym.startswith(("8", "4")):
            return "bj" + sym
        return "sh" + sym

    try:
        start_fmt = start_date.replace("-", "")
        end_fmt = end_date.replace("-", "")
        df = retry(
            lambda: ak.stock_zh_a_daily(
                symbol=prefixed(symbol),
                start_date=start_fmt,
                end_date=end_fmt,
                adjust="qfq",
            ),
            times=2,
            pause=1.5,
        )
        if df is not None and len(df) > 0:
            return df
    except Exception:
        pass
    return pd.DataFrame()


# ---------------------------------------------------------------------------
# 表现计算
# ---------------------------------------------------------------------------


def to_float(value, default=0.0) -> float:
    try:
        if value is None or str(value).strip() in {"", "-", "nan", "None"}:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def compute_performance(record: CandidateRecord, latest_data: dict, kline_df: pd.DataFrame) -> PerformanceRecord:
    """计算单只候选股的表现。"""
    latest_close = latest_data.get("close", 0)
    latest_date = latest_data.get("date", "")
    selected_close = record.selected_close

    if latest_close <= 0 or selected_close <= 0:
        return PerformanceRecord(
            record=record,
            latest_close=0,
            latest_date=latest_date,
            holding_days=0,
            return_pct=0,
            max_return_pct=0,
            min_return_pct=0,
            broke_white=False,
            broke_yellow=False,
            hit_resistance=False,
            hit_support=False,
            status="无数据",
        )

    return_pct = (latest_close - selected_close) / selected_close * 100
    holding_days = 0
    max_return_pct = return_pct
    min_return_pct = return_pct
    broke_white = False
    broke_yellow = False
    hit_resistance = False
    hit_support = False

    if kline_df is not None and len(kline_df) > 0:
        # 找到选入日之后的K线
        date_col = "date" if "date" in kline_df.columns else None
        if date_col is None and "日期" in kline_df.columns:
            date_col = "日期"

        if date_col:
            kline_df = kline_df.copy()
            kline_df[date_col] = pd.to_datetime(kline_df[date_col])
            after_select = kline_df[kline_df[date_col] > pd.Timestamp(record.selected_date)]

            if len(after_select) > 0:
                holding_days = len(after_select)
                high_col = "high" if "high" in after_select.columns else "最高"
                low_col = "low" if "low" in after_select.columns else "最低"
                close_col = "close" if "close" in after_select.columns else "收盘"

                if high_col in after_select.columns:
                    max_high = float(after_select[high_col].max())
                    max_return_pct = (max_high - selected_close) / selected_close * 100
                if low_col in after_select.columns:
                    min_low = float(after_select[low_col].min())
                    min_return_pct = (min_low - selected_close) / selected_close * 100

                # 检查破线
                if record.white_line > 0:
                    if low_col in after_select.columns:
                        broke_white = float(after_select[low_col].min()) < record.white_line
                if record.yellow_line > 0:
                    if low_col in after_select.columns:
                        broke_yellow = float(after_select[low_col].min()) < record.yellow_line

                # 检查触及压力/支撑
                if record.resistance_price > 0 and high_col in after_select.columns:
                    hit_resistance = float(after_select[high_col].max()) >= record.resistance_price
                if record.support_price > 0 and low_col in after_select.columns:
                    hit_support = float(after_select[low_col].min()) <= record.support_price * 1.01

    if return_pct > 0.5:
        status = "盈利"
    elif return_pct < -0.5:
        status = "亏损"
    else:
        status = "平"

    return PerformanceRecord(
        record=record,
        latest_close=latest_close,
        latest_date=latest_date,
        holding_days=holding_days,
        return_pct=round(return_pct, 2),
        max_return_pct=round(max_return_pct, 2),
        min_return_pct=round(min_return_pct, 2),
        broke_white=broke_white,
        broke_yellow=broke_yellow,
        hit_resistance=hit_resistance,
        hit_support=hit_support,
        status=status,
    )


# ---------------------------------------------------------------------------
# 统计汇总
# ---------------------------------------------------------------------------


def build_periodic_stats(perfs: list[PerformanceRecord], period_label: str, date_range: str) -> PeriodicStats:
    """汇总所有表现记录，生成统计。"""
    total = len(perfs)
    win = sum(1 for p in perfs if p.status == "盈利")
    loss = sum(1 for p in perfs if p.status == "亏损")
    flat = sum(1 for p in perfs if p.status == "平")
    no_data = sum(1 for p in perfs if p.status == "无数据")

    valid = [p for p in perfs if p.status != "无数据"]
    returns = [p.return_pct for p in valid]
    holding_days = [p.holding_days for p in valid if p.holding_days > 0]

    avg_return = sum(returns) / len(returns) if returns else 0
    max_ret = max(returns) if returns else 0
    min_ret = min(returns) if returns else 0
    win_rate = win / len(valid) * 100 if valid else 0
    avg_holding = sum(holding_days) / len(holding_days) if holding_days else 0

    # 最佳/最差
    best = max(valid, key=lambda p: p.return_pct) if valid else None
    worst = min(valid, key=lambda p: p.return_pct) if valid else None
    best_str = f"{best.record.symbol} {best.record.name}({best.return_pct:+.2f}%)" if best else "无"
    worst_str = f"{worst.record.symbol} {worst.record.name}({worst.return_pct:+.2f}%)" if worst else "无"

    # 板块表现
    sector_returns = defaultdict(list)
    for p in valid:
        sector = p.record.sector or "未知"
        sector_returns[sector].append(p.return_pct)
    sector_perf = {s: round(sum(rs) / len(rs), 2) for s, rs in sector_returns.items() if len(rs) >= 1}
    sector_perf = dict(sorted(sector_perf.items(), key=lambda x: -x[1]))

    # 标签表现
    tag_returns = defaultdict(list)
    for p in valid:
        for tag in p.record.tags.replace("；", ";").replace(",", ";").replace("，", ";").split(";"):
            tag = tag.strip()
            if tag:
                tag_returns[tag].append(p.return_pct)
    tag_perf = {t: round(sum(rs) / len(rs), 2) for t, rs in tag_returns.items() if len(rs) >= 2}
    tag_perf = dict(sorted(tag_perf.items(), key=lambda x: -x[1]))

    # 每日选入胜率
    daily_groups = defaultdict(list)
    for p in valid:
        daily_groups[p.record.selected_date].append(p.return_pct)
    daily_breakdown = [
        {"date": d, "count": len(rs), "avg_return": round(sum(rs) / len(rs), 2),
         "win_rate": round(sum(1 for r in rs if r > 0) / len(rs) * 100, 1)}
        for d, rs in sorted(daily_groups.items())
    ]

    broke_white_count = sum(1 for p in valid if p.broke_white)
    broke_yellow_count = sum(1 for p in valid if p.broke_yellow)

    unique_stocks = len(set(p.record.symbol for p in perfs))

    return PeriodicStats(
        period_label=period_label,
        date_range=date_range,
        total_selections=total,
        unique_stocks=unique_stocks,
        win_count=win,
        loss_count=loss,
        flat_count=flat,
        no_data_count=no_data,
        win_rate=round(win_rate, 1),
        avg_return=round(avg_return, 2),
        max_return=round(max_ret, 2),
        min_return=round(min_ret, 2),
        avg_holding_days=round(avg_holding, 1),
        best_stock=best_str,
        worst_stock=worst_str,
        sector_performance=dict(list(sector_perf.items())[:10]),
        tag_performance=dict(list(tag_perf.items())[:10]),
        daily_breakdown=daily_breakdown,
        broke_white_count=broke_white_count,
        broke_yellow_count=broke_yellow_count,
    )


# ---------------------------------------------------------------------------
# HTML 报告生成
# ---------------------------------------------------------------------------


def write_html(perfs: list[PerformanceRecord], stats: PeriodicStats, output: Path) -> None:
    """生成 HTML 汇总报告。"""
    # 按收益排序
    sorted_perfs = sorted(perfs, key=lambda p: p.return_pct, reverse=True)

    # 统计卡片
    stat_cards = f"""
    <div class="stat"><span>选入次数</span><strong>{stats.total_selections}</strong></div>
    <div class="stat"><span>去重股票</span><strong>{stats.unique_stocks}</strong></div>
    <div class="stat"><span>胜率</span><strong style="color:{'#4caf50' if stats.win_rate >= 50 else '#f44336'}">{stats.win_rate}%</strong></div>
    <div class="stat"><span>平均收益</span><strong style="color:{'#4caf50' if stats.avg_return >= 0 else '#f44336'}">{stats.avg_return:+.2f}%</strong></div>
    <div class="stat"><span>盈利/亏损</span><strong>{stats.win_count}/{stats.loss_count}</strong></div>
    <div class="stat"><span>平均持有天数</span><strong>{stats.avg_holding_days}</strong></div>
    <div class="stat"><span>最大收益</span><strong style="color:#4caf50;">{stats.max_return:+.2f}%</strong></div>
    <div class="stat"><span>最大亏损</span><strong style="color:#f44336;">{stats.min_return:+.2f}%</strong></div>
    <div class="stat"><span>破白线</span><strong>{stats.broke_white_count}</strong></div>
    <div class="stat"><span>破黄线</span><strong>{stats.broke_yellow_count}</strong></div>
    """

    # 每日胜率明细
    daily_rows = ""
    for d in stats.daily_breakdown:
        color = "#4caf50" if d["avg_return"] >= 0 else "#f44336"
        daily_rows += f"<tr><td>{d['date']}</td><td>{d['count']}</td><td style='color:{color};'>{d['avg_return']:+.2f}%</td><td>{d['win_rate']}%</td></tr>"

    # 板块表现
    sector_rows = ""
    for s, r in stats.sector_performance.items():
        color = "#4caf50" if r >= 0 else "#f44336"
        sector_rows += f"<tr><td>{s}</td><td style='color:{color};'>{r:+.2f}%</td></tr>"

    # 标签表现
    tag_rows = ""
    for t, r in stats.tag_performance.items():
        color = "#4caf50" if r >= 0 else "#f44336"
        tag_rows += f"<tr><td>{t}</td><td style='color:{color};'>{r:+.2f}%</td></tr>"

    # 个股明细
    stock_rows = ""
    for p in sorted_perfs:
        ret_color = "#4caf50" if p.return_pct >= 0 else "#f44336"
        status_color = {"盈利": "#4caf50", "亏损": "#f44336", "平": "#999", "无数据": "#ccc"}[p.status]
        broke = ""
        if p.broke_white:
            broke += '<span style="color:#f44336;">破白</span> '
        if p.broke_yellow:
            broke += '<span style="color:#b71c1c;">破黄</span> '
        if p.hit_resistance:
            broke += '<span style="color:#ff9800;">触压</span> '
        stock_rows += f"""<tr>
            <td>{p.record.selected_date}</td>
            <td><strong>{p.record.symbol}</strong> {p.record.name}</td>
            <td>{p.record.selected_close:.2f}</td>
            <td>{p.latest_close:.2f}</td>
            <td style="color:{ret_color};font-weight:600;">{p.return_pct:+.2f}%</td>
            <td style="color:#4caf50;">{p.max_return_pct:+.2f}%</td>
            <td style="color:#f44336;">{p.min_return_pct:+.2f}%</td>
            <td>{p.holding_days}</td>
            <td style="color:{status_color};">{p.status}</td>
            <td>{broke or '-'}</td>
            <td>{p.record.sector or '-'}</td>
            <td style="font-size:11px;">{p.record.tags[:30] or '-'}</td>
        </tr>"""

    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>周期复盘报告 - {stats.period_label}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, "Microsoft YaHei", sans-serif; background: #f5f6fa; color: #1a1d29; padding: 20px; }}
  header {{ background: linear-gradient(135deg, #355cff, #5e72e4); color: white; padding: 24px 30px; border-radius: 14px; margin-bottom: 20px; }}
  header h1 {{ font-size: 24px; margin-bottom: 6px; }}
  header p {{ opacity: 0.9; font-size: 14px; }}
  .stats {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 20px; }}
  .stat {{ background: white; border-radius: 10px; padding: 14px; text-align: center; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
  .stat span {{ display: block; color: #667085; font-size: 12px; margin-bottom: 4px; }}
  .stat strong {{ font-size: 22px; }}
  section {{ background: white; border-radius: 12px; padding: 20px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }}
  section h2 {{ font-size: 16px; margin-bottom: 12px; color: #1a1d29; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ padding: 7px 10px; text-align: left; border-bottom: 1px solid #e9ecef; }}
  th {{ background: #f8f9fa; font-weight: 600; color: #495057; white-space: nowrap; }}
  tr:hover {{ background: #f8f9fa; }}
  .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  .best-worst {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 16px; }}
  .card {{ border-radius: 10px; padding: 12px 16px; }}
  .card.good {{ background: #e8f5e9; border: 1px solid #4caf50; }}
  .card.bad {{ background: #ffebee; border: 1px solid #f44336; }}
  .card span {{ font-size: 12px; color: #667085; }}
  .card strong {{ display: block; font-size: 15px; margin-top: 4px; }}
  footer {{ text-align: center; color: #667085; font-size: 12px; margin-top: 20px; }}
</style>
</head>
<body>
<header>
  <h1>周期复盘报告</h1>
  <p>统计区间：{stats.period_label}（{stats.date_range}）｜生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
</header>

<div class="best-worst">
  <div class="card good"><span>最佳标的</span><strong>{stats.best_stock}</strong></div>
  <div class="card bad"><span>最差标的</span><strong>{stats.worst_stock}</strong></div>
</div>

<div class="stats">{stat_cards}</div>

<section>
  <h2>每日选入胜率明细</h2>
  <table>
    <thead><tr><th>选入日期</th><th>选入数量</th><th>平均收益</th><th>胜率</th></tr></thead>
    <tbody>{daily_rows or '<tr><td colspan="4" style="text-align:center;color:#999;">暂无数据</td></tr>'}</tbody>
  </table>
</section>

<div class="grid-2">
  <section>
    <h2>板块平均收益</h2>
    <table>
      <thead><tr><th>板块</th><th>平均收益</th></tr></thead>
      <tbody>{sector_rows or '<tr><td colspan="2" style="text-align:center;color:#999;">暂无数据</td></tr>'}</tbody>
    </table>
  </section>
  <section>
    <h2>标签平均收益（≥2次出现）</h2>
    <table>
      <thead><tr><th>标签</th><th>平均收益</th></tr></thead>
      <tbody>{tag_rows or '<tr><td colspan="2" style="text-align:center;color:#999;">暂无数据</td></tr>'}</tbody>
    </table>
  </section>
</div>

<section>
  <h2>个股表现明细（按收益排序）</h2>
  <table>
    <thead><tr>
      <th>选入日</th><th>股票</th><th>选入价</th><th>最新价</th>
      <th>收益率</th><th>最高收益</th><th>最大亏损</th><th>持有天数</th>
      <th>状态</th><th>风险信号</th><th>板块</th><th>标签</th>
    </tr></thead>
    <tbody>{stock_rows or '<tr><td colspan="12" style="text-align:center;color:#999;">暂无数据</td></tr>'}</tbody>
  </table>
</section>

<footer>本报告由交易系统自动生成，仅供策略验证，不构成投资建议。</footer>
</body>
</html>"""
    output.write_text(html_content, encoding="utf-8")


def write_json(perfs: list[PerformanceRecord], stats: PeriodicStats, output: Path) -> None:
    """保存 JSON 格式的汇总结果。"""
    data = {
        "period_label": stats.period_label,
        "date_range": stats.date_range,
        "generated_at": datetime.now().isoformat(),
        "stats": {
            "total_selections": stats.total_selections,
            "unique_stocks": stats.unique_stocks,
            "win_count": stats.win_count,
            "loss_count": stats.loss_count,
            "flat_count": stats.flat_count,
            "no_data_count": stats.no_data_count,
            "win_rate": stats.win_rate,
            "avg_return": stats.avg_return,
            "max_return": stats.max_return,
            "min_return": stats.min_return,
            "avg_holding_days": stats.avg_holding_days,
            "best_stock": stats.best_stock,
            "worst_stock": stats.worst_stock,
            "broke_white_count": stats.broke_white_count,
            "broke_yellow_count": stats.broke_yellow_count,
            "sector_performance": stats.sector_performance,
            "tag_performance": stats.tag_performance,
            "daily_breakdown": stats.daily_breakdown,
        },
        "records": [
            {
                "selected_date": p.record.selected_date,
                "symbol": p.record.symbol,
                "name": p.record.name,
                "selected_close": p.record.selected_close,
                "latest_close": p.latest_close,
                "latest_date": p.latest_date,
                "return_pct": p.return_pct,
                "max_return_pct": p.max_return_pct,
                "min_return_pct": p.min_return_pct,
                "holding_days": p.holding_days,
                "status": p.status,
                "broke_white": p.broke_white,
                "broke_yellow": p.broke_yellow,
                "hit_resistance": p.hit_resistance,
                "hit_support": p.hit_support,
                "sector": p.record.sector,
                "tags": p.record.tags,
                "candidate_score": p.record.candidate_score,
            }
            for p in sorted(perfs, key=lambda x: x.return_pct, reverse=True)
        ],
    }
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="按周/按月汇总复盘")
    parser.add_argument("--period", choices=["weekly", "monthly", "all", "custom"], default="weekly",
                        help="统计周期：weekly=最近7天，monthly=最近30天，all=全部，custom=自定义")
    parser.add_argument("--start", type=str, default="", help="自定义开始日期 YYYY-MM-DD")
    parser.add_argument("--end", type=str, default="", help="自定义结束日期 YYYY-MM-DD")
    args = parser.parse_args()

    today = date.today()

    if args.period == "weekly":
        start_date = today - timedelta(days=7)
        end_date = today
        period_label = "最近7天"
    elif args.period == "monthly":
        start_date = today - timedelta(days=30)
        end_date = today
        period_label = "最近30天"
    elif args.period == "all":
        start_date = None
        end_date = None
        period_label = "全部历史"
    else:
        start_date = date.fromisoformat(args.start) if args.start else None
        end_date = date.fromisoformat(args.end) if args.end else today
        period_label = f"{start_date or '起始'} ~ {end_date}"

    date_range = f"{start_date.isoformat() if start_date else '起始'} ~ {end_date.isoformat() if end_date else '今天'}"
    print(f"[周期复盘] 统计区间：{period_label}（{date_range}）")

    # 1. 加载历史候选股
    history_files = load_history_files(start_date, end_date)
    if not history_files:
        print("[周期复盘] 未找到历史候选股记录。请先通过「每日一键执行」生成候选股。")
        return

    print(f"[周期复盘] 找到 {len(history_files)} 天的历史记录")

    all_records: list[CandidateRecord] = []
    for date_str, file_path in history_files:
        candidates = load_candidates_from_file(file_path)
        for row in candidates:
            all_records.append(CandidateRecord(
                selected_date=date_str,
                symbol=str(row.get("symbol", "")).strip().zfill(6),
                name=str(row.get("name", "")).strip(),
                selected_close=to_float(row.get("close")),
                pct_change_at_select=to_float(row.get("pct_change")),
                tags=str(row.get("tags", "")),
                candidate_score=to_float(row.get("candidate_score")),
                sector=str(row.get("sector", "")),
                support_price=to_float(row.get("support_price")),
                resistance_price=to_float(row.get("resistance_price")),
                white_line=to_float(row.get("white_line")),
                yellow_line=to_float(row.get("yellow_line")),
            ))

    print(f"[周期复盘] 共加载 {len(all_records)} 条选入记录")

    if not all_records:
        print("[周期复盘] 无有效记录，退出。")
        return

    # 2. 获取最新行情
    all_symbols = set(r.symbol for r in all_records)
    print(f"[周期复盘] 获取 {len(all_symbols)} 只股票的最新行情...")

    latest_prices = fetch_latest_prices(all_symbols)
    print(f"[周期复盘] 获取到 {len(latest_prices)} 只股票的行情")

    # 3. 获取K线计算持有期表现（串行，避免并发崩溃）
    print("[周期复盘] 逐只获取K线计算持有期表现...")
    perfs: list[PerformanceRecord] = []
    for i, record in enumerate(all_records):
        if (i + 1) % 10 == 0:
            print(f"  进度：{i + 1}/{len(all_records)}")

        latest = latest_prices.get(record.symbol, {})
        if not latest:
            latest = {"close": 0, "date": "", "high": 0, "low": 0}

        # 获取选入日到今天的K线
        kline_df = fetch_daily(record.symbol, record.selected_date, today.isoformat())

        perf = compute_performance(record, latest, kline_df)
        perfs.append(perf)

    # 4. 汇总统计
    stats = build_periodic_stats(perfs, period_label, date_range)

    print(f"\n[周期复盘] 汇总结果：")
    print(f"  总选入：{stats.total_selections} 次（{stats.unique_stocks} 只股票）")
    print(f"  胜率：{stats.win_rate}%（盈利{stats.win_count}，亏损{stats.loss_count}）")
    print(f"  平均收益：{stats.avg_return:+.2f}%")
    print(f"  最大收益：{stats.max_return:+.2f}%，最大亏损：{stats.min_return:+.2f}%")
    print(f"  破白线：{stats.broke_white_count}，破黄线：{stats.broke_yellow_count}")

    # 5. 保存报告
    outputs = PROJECT_ROOT / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)

    suffix = args.period if args.period != "custom" else f"{start_date or 'start'}_{end_date or 'end'}"
    html_path = outputs / f"periodic_review_{suffix}.html"
    json_path = outputs / f"periodic_review_{suffix}.json"
    write_html(perfs, stats, html_path)
    write_json(perfs, stats, json_path)

    # 同时保存一份到 history/ 目录
    history_dir = outputs / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    dated_html = history_dir / f"periodic_review_{suffix}_{today.isoformat()}.html"
    shutil.copyfile(html_path, dated_html)
    print(f"\n[周期复盘] 报告已保存：")
    print(f"  HTML: {html_path}")
    print(f"  JSON: {json_path}")
    print(f"  历史备份: {dated_html}")


if __name__ == "__main__":
    main()
