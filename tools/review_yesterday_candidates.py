from __future__ import annotations

import html
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import akshare as ak  # type: ignore

HISTORY_DAYS = 15
# AkShare 的部分日线接口会调用 py_mini_racer，Windows 下并发调用偶发底层崩溃。
# 复盘任务只有几十只股票，默认串行执行更稳定。
MAX_WORKERS = 1


def retry(fn, times: int = 3, pause: float = 1.0):
    last = None
    for i in range(times):
        try:
            return fn()
        except Exception as exc:
            last = exc
            time.sleep(pause * (i + 1))
    raise last


def to_float(value, default: float = 0.0) -> float:
    try:
        if value is None or str(value).strip() in {"", "-", "nan"}:
            return default
        return float(value)
    except Exception:
        return default


def prefixed(symbol: str) -> str:
    if symbol.startswith("6"):
        return "sh" + symbol
    if symbol.startswith(("0", "3")):
        return "sz" + symbol
    if symbol.startswith(("4", "8", "9")):
        return "bj" + symbol
    return symbol


def load_candidates() -> list[dict]:
    path = PROJECT_ROOT / "outputs" / "daily_candidates.json"
    if not path.exists():
        raise FileNotFoundError("未找到 outputs/daily_candidates.json，请先生成每日候选股票。")
    data = json.loads(path.read_text(encoding="utf-8"))
    candidates = data.get("candidates", [])
    if not candidates:
        raise RuntimeError("每日候选股票为空，请先重新生成候选股票。")
    return candidates


def fetch_daily(symbol: str) -> pd.DataFrame:
    end = date.today()
    start = end - timedelta(days=HISTORY_DAYS + 20)
    start_date = start.strftime("%Y%m%d")
    end_date = end.strftime("%Y%m%d")
    try:
        return retry(
            lambda: ak.stock_zh_a_daily(
                symbol=prefixed(symbol),
                start_date=start_date,
                end_date=end_date,
                adjust="qfq",
            ),
            times=3,
            pause=1,
        )
    except Exception:
        try:
            return retry(
                lambda: ak.stock_zh_a_hist(
                    symbol=symbol,
                    period="daily",
                    start_date=start_date,
                    end_date=end_date,
                    adjust="qfq",
                    timeout=8,
                ),
                times=2,
                pause=1,
            )
        except Exception:
            return pd.DataFrame()


def normalize_daily_row(row) -> dict:
    # stock_zh_a_daily 使用英文列名，stock_zh_a_hist 使用中文列名；这里统一兼容。
    close = to_float(row.get("close", row.get("收盘")))
    open_ = to_float(row.get("open", row.get("开盘")))
    high = to_float(row.get("high", row.get("最高")))
    low = to_float(row.get("low", row.get("最低")))
    amount = to_float(row.get("amount", row.get("成交额")))
    raw_date = row.get("date", row.get("日期", ""))
    return {
        "date": str(raw_date),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "amount": amount,
    }


def performance_category(value: float) -> str:
    if value >= 5:
        return "强势"
    if value >= 2:
        return "偏强"
    if value >= -2:
        return "震荡"
    if value >= -5:
        return "偏弱"
    return "弱势"


def process_one(candidate: dict) -> dict:
    symbol = candidate["symbol"]
    daily = fetch_daily(symbol)

    latest = {}
    previous_close = 0.0
    if daily is not None and not daily.empty:
        latest = normalize_daily_row(daily.iloc[-1])
        if len(daily) >= 2:
            previous = normalize_daily_row(daily.iloc[-2])
            previous_close = previous["close"]

    today_close = latest.get("close", 0.0)
    today_pct = round((today_close / previous_close - 1) * 100, 2) if today_close > 0 and previous_close > 0 else 0.0
    selected_close = to_float(candidate.get("close"))
    change_from_selected = round((today_close / selected_close - 1) * 100, 2) if today_close > 0 and selected_close > 0 else 0.0

    white_line = to_float(candidate.get("white_line"))
    yellow_line = to_float(candidate.get("yellow_line"))
    support = to_float(candidate.get("support_price"))
    resistance = to_float(candidate.get("resistance_price"))
    high = latest.get("high", 0.0)
    low = latest.get("low", 0.0)
    intraday_pos = round((today_close - low) / (high - low) * 100, 1) if high > low else 50.0

    return {
        "symbol": symbol,
        "name": candidate.get("name", ""),
        "selected_close": selected_close,
        "selected_pct": to_float(candidate.get("pct_change")),
        "today_date": latest.get("date", ""),
        "today_open": latest.get("open", 0.0),
        "today_high": high,
        "today_low": low,
        "today_close": today_close,
        "today_pct": today_pct,
        "today_amount": latest.get("amount", 0.0),
        "change_from_selected": change_from_selected,
        "intraday_pos": intraday_pos,
        "white_line": white_line,
        "yellow_line": yellow_line,
        "support": support,
        "resistance": resistance,
        "broke_white": today_close > 0 and today_close < white_line,
        "broke_yellow": today_close > 0 and today_close < yellow_line,
        "distance_to_resistance": round((resistance / today_close - 1) * 100, 2) if today_close > 0 and resistance > 0 else 0.0,
        "distance_to_support": round((today_close / support - 1) * 100, 2) if today_close > 0 and support > 0 else 0.0,
        "performance_category": performance_category(change_from_selected),
        "tags": candidate.get("tags", ""),
        "plan_action": candidate.get("plan_action", ""),
        "candidate_score": to_float(candidate.get("candidate_score")),
    }


def fmt_pct(value: float) -> str:
    prefix = "+" if value > 0 else ""
    return f"{prefix}{value:.2f}%"


def fmt_amount(value: float) -> str:
    if value >= 100_000_000:
        return f"{value / 100_000_000:.2f} 亿"
    if value >= 10_000:
        return f"{value / 10_000:.0f} 万"
    return f"{value:.0f}"


def pct_class(value: float) -> str:
    if value > 0:
        return "up"
    if value < 0:
        return "down"
    return "flat"


def badge_class(category: str) -> str:
    return {
        "强势": "strong",
        "偏强": "mild-up",
        "震荡": "neutral",
        "偏弱": "mild-down",
        "弱势": "weak",
    }.get(category, "neutral")


def write_json(rows: list[dict], stats: dict, path: Path) -> None:
    path.write_text(json.dumps({"stats": stats, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")


def write_html(rows: list[dict], stats: dict, path: Path) -> None:
    top_gainers = sorted(rows, key=lambda item: item["change_from_selected"], reverse=True)[:10]
    top_losers = sorted(rows, key=lambda item: item["change_from_selected"])[:10]
    broke_yellow = [row for row in rows if row["broke_yellow"]]

    def compact_row(row: dict) -> str:
        return f"""
        <tr>
          <td class="code">{html.escape(row['symbol'])}</td>
          <td>{html.escape(row['name'])}</td>
          <td>{row['selected_close']}</td>
          <td>{row['today_close']}</td>
          <td class="{pct_class(row['change_from_selected'])}">{fmt_pct(row['change_from_selected'])}</td>
          <td class="{pct_class(row['today_pct'])}">{fmt_pct(row['today_pct'])}</td>
          <td>{row['intraday_pos']:.0f}%</td>
        </tr>"""

    def detail_row(index: int, row: dict) -> str:
        tags = "".join(f'<span class="tag">{html.escape(tag)}</span>' for tag in str(row["tags"]).split("；") if tag)
        yellow_mark = '<span class="risk">破</span>' if row["broke_yellow"] else ""
        white_mark = '<span class="risk">破</span>' if row["broke_white"] else ""
        return f"""
        <tr>
          <td>{index}</td>
          <td class="code">{html.escape(row['symbol'])}</td>
          <td>{html.escape(row['name'])}</td>
          <td>{row['selected_close']}</td>
          <td>{row['today_close']}</td>
          <td class="{pct_class(row['change_from_selected'])}">{fmt_pct(row['change_from_selected'])}</td>
          <td class="{pct_class(row['today_pct'])}">{fmt_pct(row['today_pct'])}</td>
          <td>{row['today_open']}</td>
          <td>{row['today_high']}</td>
          <td>{row['today_low']}</td>
          <td>{fmt_amount(row['today_amount'])}</td>
          <td>{row['intraday_pos']:.0f}%</td>
          <td>{row['white_line']} {white_mark}</td>
          <td>{row['yellow_line']} {yellow_mark}</td>
          <td>{row['resistance']}</td>
          <td>{row['distance_to_resistance']:.1f}%</td>
          <td><span class="badge {badge_class(row['performance_category'])}">{row['performance_category']}</span></td>
          <td>{html.escape(row['plan_action'])}</td>
          <td>{row['candidate_score']}</td>
          <td>{tags}</td>
        </tr>"""

    alert_items = "".join(
        f'<div class="alert-item"><strong>{html.escape(row["symbol"])} {html.escape(row["name"])}</strong> 收盘 {row["today_close"]}，黄线 {row["yellow_line"]}，较选入日 {fmt_pct(row["change_from_selected"])}。</div>'
        for row in broke_yellow
    ) or '<div class="alert-item">无候选股票跌破黄线。</div>'

    distribution = stats["distribution"]
    distribution_html = "".join(
        f'<div class="dist-card"><span>{name}</span><strong>{count}</strong></div>'
        for name, count in distribution.items()
    )

    body = f"""<!-- Generated by Trae Work -->
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>每日候选股票复盘</title>
  <style>
    :root {{
      --bg:#f4f5fa; --bg2:#fff; --ink:#1a1f36; --muted:#6b7280; --rule:#e5e7eb;
      --accent:#355cff; --up:#dc2626; --down:#16a34a; --warn:#b45309; --danger:#b91c1c;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: var(--ink); font-family: "Microsoft YaHei", system-ui, sans-serif; line-height: 1.62; }}
    main {{ max-width: 1280px; margin: 0 auto; padding: 28px 20px 48px; }}
    header, section {{ background: var(--bg2); border: 1px solid var(--rule); border-radius: 16px; padding: 22px; margin-bottom: 16px; }}
    h1 {{ margin: 0 0 8px; font-size: 30px; }}
    h2 {{ margin: 0 0 14px; font-size: 21px; }}
    p {{ color: var(--muted); margin: 0 0 10px; }}
    .stats, .distribution {{ display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; margin-top: 16px; }}
    .stat, .dist-card {{ background: var(--bg); border: 1px solid var(--rule); border-radius: 12px; padding: 14px; text-align: center; }}
    .stat span, .dist-card span {{ display: block; color: var(--muted); font-size: 12px; }}
    .stat strong, .dist-card strong {{ display: block; margin-top: 4px; font-size: 24px; color: var(--accent); }}
    .up {{ color: var(--up); font-weight: 700; }}
    .down {{ color: var(--down); font-weight: 700; }}
    .flat {{ color: var(--muted); }}
    .grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
    .table-wrap {{ overflow-x: auto; border: 1px solid var(--rule); border-radius: 14px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; min-width: 860px; }}
    .detail table {{ min-width: 1420px; }}
    th, td {{ padding: 9px 10px; border-bottom: 1px solid var(--rule); text-align: right; white-space: nowrap; }}
    th {{ background: var(--bg); color: var(--muted); position: sticky; top: 0; z-index: 1; }}
    th:nth-child(1), th:nth-child(2), th:nth-child(3), td:nth-child(1), td:nth-child(2), td:nth-child(3) {{ text-align: left; }}
    .code {{ font-family: Consolas, monospace; }}
    .badge {{ display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 700; }}
    .strong {{ background: #fee2e2; color: #991b1b; }}
    .mild-up {{ background: #fef3c7; color: #92400e; }}
    .neutral {{ background: #f3f4f6; color: #4b5563; }}
    .mild-down {{ background: #dbeafe; color: #1e40af; }}
    .weak {{ background: #dcfce7; color: #166534; }}
    .tag {{ display: inline-block; margin: 1px 3px 1px 0; padding: 1px 6px; border-radius: 5px; background: #eef2ff; color: #3730a3; font-size: 11px; }}
    .alert {{ border-left: 4px solid var(--danger); background: #fef2f2; border-radius: 12px; padding: 12px 14px; margin-bottom: 10px; }}
    .alert-item {{ margin-top: 8px; padding: 8px 10px; border-radius: 8px; background: white; font-size: 13px; }}
    .note {{ border-left: 4px solid var(--accent); background: var(--bg); border-radius: 12px; padding: 12px 14px; margin-top: 10px; }}
    .risk {{ color: var(--danger); font-weight: 700; margin-left: 4px; }}
    @media (max-width: 920px) {{ .stats, .distribution {{ grid-template-columns: repeat(2, 1fr); }} .grid2 {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>每日候选股票复盘</h1>
      <p>选入日基准来自 `outputs/daily_candidates.json`，复盘行情取最新前复权日线。报告用于验证候选票次日表现和破线风险，不构成投资建议。</p>
      <div class="stats">
        <div class="stat"><span>候选总数</span><strong>{stats['total']}</strong></div>
        <div class="stat"><span>有行情数据</span><strong>{stats['valid']}</strong></div>
        <div class="stat"><span>平均变动</span><strong class="{pct_class(stats['avg_change'])}">{fmt_pct(stats['avg_change'])}</strong></div>
        <div class="stat"><span>上涨</span><strong class="up">{stats['up_count']}</strong></div>
        <div class="stat"><span>下跌</span><strong class="down">{stats['down_count']}</strong></div>
        <div class="stat"><span>破黄线</span><strong>{stats['broke_yellow_count']}</strong></div>
      </div>
    </header>

    <section>
      <h2>表现分布</h2>
      <div class="distribution">{distribution_html}</div>
    </section>

    <div class="grid2">
      <section>
        <h2>涨幅前 10</h2>
        <div class="table-wrap">
          <table>
            <thead><tr><th>代码</th><th>名称</th><th>选入价</th><th>今日收盘</th><th>较选入</th><th>今日涨幅</th><th>盘中位置</th></tr></thead>
            <tbody>{''.join(compact_row(row) for row in top_gainers)}</tbody>
          </table>
        </div>
      </section>
      <section>
        <h2>跌幅前 10</h2>
        <div class="table-wrap">
          <table>
            <thead><tr><th>代码</th><th>名称</th><th>选入价</th><th>今日收盘</th><th>较选入</th><th>今日涨幅</th><th>盘中位置</th></tr></thead>
            <tbody>{''.join(compact_row(row) for row in top_losers)}</tbody>
          </table>
        </div>
      </section>
    </div>

    <section>
      <h2>风险预警</h2>
      <div class="alert">
        <p><strong>破黄线股票：{len(broke_yellow)} 只。</strong>破黄线说明中期趋势支撑有弱化迹象，下一步重点看是否继续破白线。</p>
        {alert_items}
      </div>
      <div class="note">破白线股票：{stats['broke_white_count']} 只。若后续复盘出现破白线，应优先降级为观察或移出候选池。</div>
    </section>

    <section class="detail">
      <h2>全部候选明细</h2>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>排名</th><th>代码</th><th>名称</th><th>选入价</th><th>今日收盘</th><th>较选入</th><th>今日涨幅</th>
              <th>今开</th><th>最高</th><th>最低</th><th>成交额</th><th>盘中位置</th><th>白线</th><th>黄线</th>
              <th>压力位</th><th>距压力</th><th>表现</th><th>系统动作</th><th>候选分</th><th>标签</th>
            </tr>
          </thead>
          <tbody>{''.join(detail_row(i, row) for i, row in enumerate(rows, 1))}</tbody>
        </table>
      </div>
    </section>
  </main>
</body>
</html>"""
    path.write_text(body, encoding="utf-8")


def build_stats(rows: list[dict]) -> dict:
    valid = [row for row in rows if row["today_close"] > 0]
    distribution = {"强势": 0, "偏强": 0, "震荡": 0, "偏弱": 0, "弱势": 0}
    for row in valid:
        distribution[row["performance_category"]] += 1
    avg_change = round(sum(row["change_from_selected"] for row in valid) / len(valid), 2) if valid else 0.0
    return {
        "total": len(rows),
        "valid": len(valid),
        "review_date": valid[0]["today_date"] if valid else "",
        "avg_change": avg_change,
        "up_count": sum(1 for row in valid if row["change_from_selected"] > 0),
        "down_count": sum(1 for row in valid if row["change_from_selected"] < 0),
        "flat_count": sum(1 for row in valid if row["change_from_selected"] == 0),
        "broke_white_count": sum(1 for row in valid if row["broke_white"]),
        "broke_yellow_count": sum(1 for row in valid if row["broke_yellow"]),
        "distribution": distribution,
    }


def main() -> None:
    outputs = PROJECT_ROOT / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    candidates = load_candidates()
    print(f"读取每日候选股票：{len(candidates)} 只")

    rows: list[dict] = []
    if MAX_WORKERS <= 1:
        for idx, candidate in enumerate(candidates, 1):
            rows.append(process_one(candidate))
            if idx % 10 == 0:
                print(f"已复盘 {idx}/{len(candidates)}")
    else:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(process_one, candidate) for candidate in candidates]
            for idx, future in enumerate(as_completed(futures), 1):
                rows.append(future.result())
                if idx % 10 == 0:
                    print(f"已复盘 {idx}/{len(candidates)}")

    rows = sorted(rows, key=lambda item: item["candidate_score"], reverse=True)
    stats = build_stats(rows)

    json_path = outputs / "review_yesterday_candidates.json"
    html_path = outputs / "review_yesterday_candidates.html"
    write_json(rows, stats, json_path)
    write_html(rows, stats, html_path)

    print(json.dumps({"stats": stats, "json": str(json_path), "html": str(html_path)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
