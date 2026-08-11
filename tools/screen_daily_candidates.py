from __future__ import annotations

import csv
import html
import json
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(r"g:\BaiduNetdiskDownload\z直播文字\trading-system-core")
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import akshare as ak  # type: ignore

from trading_system.data_normalizer import WatchSymbol, build_snapshot
from trading_system.engines import TradePlanEngine
from trading_system.indicators import is_macd_bullish
from trading_system.rulebook import Rulebook


DEFAULT_ACTIVE_MARKET_VALUE_PCT = 4.82
HISTORY_DAYS = 180
PREFILTER_LIMIT = 220
MAX_WORKERS = 10
TOP_N = 40


def load_active_market_value_pct() -> float:
    """从 data/compass_active_market_value.csv 读取面板保存的活跃市值，失败时回退默认值。"""
    csv_path = PROJECT_ROOT / "data" / "compass_active_market_value.csv"
    if not csv_path.exists():
        print(f"[警告] 活跃市值文件不存在({csv_path})，使用默认值 {DEFAULT_ACTIVE_MARKET_VALUE_PCT}")
        return DEFAULT_ACTIVE_MARKET_VALUE_PCT
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            rows = [row for row in csv.DictReader(f) if row]
        if not rows:
            raise ValueError("文件为空")
        latest = rows[-1]
        raw = latest.get("active_market_value_pct") or latest.get("active_market_value")
        if raw is None or str(raw).strip() == "":
            raise ValueError("缺少 active_market_value_pct 字段")
        value = float(str(raw).strip())
        src_date = latest.get("date", "")
        print(f"[活跃市值] 读取成功：{value}%（日期：{src_date}，来源：{latest.get('source', 'compass')}）")
        return value
    except Exception as exc:
        print(f"[警告] 读取活跃市值失败({exc})，使用默认值 {DEFAULT_ACTIVE_MARKET_VALUE_PCT}")
        return DEFAULT_ACTIVE_MARKET_VALUE_PCT


def load_recent_active_market_values(days: int = 3) -> list[float]:
    """读取最近 N 天的活跃市值百分比列表（含当天），用于3日累计多头判定。"""
    csv_path = PROJECT_ROOT / "data" / "compass_active_market_value.csv"
    if not csv_path.exists():
        return []
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            rows = [row for row in csv.DictReader(f) if row]
        if not rows:
            return []
        values = []
        for row in rows[-days:]:
            raw = row.get("active_market_value_pct") or row.get("active_market_value")
            if raw is not None and str(raw).strip() != "":
                values.append(float(str(raw).strip()))
        if values:
            print(f"[活跃市值] 近{len(values)}日值：{values}，累计：{sum(values):.2f}%")
        return values
    except Exception as exc:
        print(f"[警告] 读取近期活跃市值失败({exc})")
        return []


def retry(fn, times=3, pause=1.5):
    last = None
    for i in range(times):
        try:
            return fn()
        except Exception as exc:
            last = exc
            time.sleep(pause * (i + 1))
    raise last


def pure_code(raw: str) -> str:
    value = str(raw).strip().lower()
    if value.startswith(("sh", "sz", "bj")):
        value = value[2:]
    return value.zfill(6) if value.isdigit() and len(value) < 6 else value


def to_float(value, default=0.0) -> float:
    try:
        if value is None or str(value).strip() in {"", "-", "nan"}:
            return default
        return float(value)
    except Exception:
        return default


def load_spot() -> pd.DataFrame:
    cache_path = PROJECT_ROOT / "data" / "a_spot_latest.csv"
    if cache_path.exists():
        df = pd.read_csv(cache_path, encoding="utf-8-sig")
        df["source"] = "cached_spot"
        return df
    try:
        df = retry(lambda: ak.stock_zh_a_spot(), times=3, pause=2)
        df["source"] = "sina_spot"
        return df
    except Exception as exc:
        print(f"新浪实时行情不可用，切换东方财富实时行情：{exc}")
    try:
        df = retry(lambda: ak.stock_zh_a_spot_em(), times=3, pause=2)
        rename = {
            "代码": "代码",
            "名称": "名称",
            "最新价": "最新价",
            "涨跌额": "涨跌额",
            "涨跌幅": "涨跌幅",
            "昨收": "昨收",
            "今开": "今开",
            "最高": "最高",
            "最低": "最低",
            "成交量": "成交量",
            "成交额": "成交额",
        }
        df = df.rename(columns=rename)
        df["source"] = "eastmoney_spot"
        return df
    except Exception as exc:
        print(f"东方财富实时行情不可用，切换代码表模式：{exc}")
    codes = retry(lambda: ak.stock_info_a_code_name(), times=3, pause=1)
    df = codes.rename(columns={"code": "代码", "name": "名称"}).copy()
    for col in ["最新价", "涨跌额", "涨跌幅", "昨收", "今开", "最高", "最低", "成交量", "成交额"]:
        df[col] = 0.0
    df["source"] = "code_name_only"
    return df


def filter_pool(spot: pd.DataFrame) -> pd.DataFrame:
    df = spot.copy()
    df["symbol"] = df["代码"].map(pure_code)
    df["name"] = df["名称"].astype(str).str.strip()
    df["close"] = df["最新价"].map(to_float)
    df["pct_change"] = df["涨跌幅"].map(to_float)
    df["amount"] = df["成交额"].map(to_float)
    df["open"] = df["今开"].map(to_float)
    df["high"] = df["最高"].map(to_float)
    df["low"] = df["最低"].map(to_float)
    df["prev_close"] = df["昨收"].map(to_float)
    code_name_only = "source" in df.columns and (df["source"].astype(str) == "code_name_only").all()
    valid = (
        df["symbol"].str.match(r"^\d{6}$", na=False)
        & ~df["symbol"].str.startswith(("300", "688"))
        & ~df["name"].str.contains("ST|退", regex=True, na=False)
    )
    if not code_name_only:
        valid = valid & (df["close"] > 0)
    return df.loc[valid].reset_index(drop=True)


def save_watchlist(pool: pd.DataFrame) -> Path:
    path = PROJECT_ROOT / "data" / "watchlist_all_a_ex_300_688.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["symbol", "name", "tags"])
        writer.writeheader()
        for _, row in pool[["symbol", "name"]].drop_duplicates("symbol").iterrows():
            writer.writerow({"symbol": row["symbol"], "name": row["name"], "tags": ""})
    return path


def save_config() -> Path:
    config = {
        "source": "akshare",
        "adjust": "qfq",
        "akshare_retries": 3,
        "akshare_pause_seconds": 0.4,
        "history_days": 180,
        "watchlist_path": "data/watchlist_all_a_ex_300_688.csv",
        "market_snapshot_path": "data/market_snapshot_all_a_ex_300_688.csv",
        "market_breadth_source": "compass_csv",
        "compass_active_market_value_path": "data/compass_active_market_value.csv",
    }
    path = PROJECT_ROOT / "config" / "data_source_all_a_ex_300_688.json"
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def prefilter_for_history(pool: pd.DataFrame) -> pd.DataFrame:
    df = pool.copy()
    if df["amount"].fillna(0).sum() <= 0:
        # 实时行情不可用时，只能用代码池先做历史日线计算；为控制耗时，优先处理沪深主板和北交所前若干只。
        df["prefilter_score"] = 1.0
        preferred = df[df["symbol"].str.startswith(("000", "001", "002", "003", "600", "601", "603", "605"))].copy()
        if preferred.empty:
            preferred = df.copy()
        return preferred.head(PREFILTER_LIMIT).reset_index(drop=True)
    # 多头区间下，先筛掉流动性过弱、涨跌停附近、当日明显走弱的票。
    df = df[
        (df["amount"] >= 50_000_000)
        & (df["pct_change"] > 0)
        & (df["pct_change"] < 8.8)
        & (df["close"] >= df["open"])
        & (df["high"] > 0)
        & (df["close"] >= df["low"] * 1.01)
    ].copy()
    if df.empty:
        return df
    amount_rank = df["amount"].rank(pct=True)
    pct_rank = df["pct_change"].rank(pct=True)
    close_pos = (df["close"] - df["low"]) / (df["high"] - df["low"]).replace(0, pd.NA)
    df["prefilter_score"] = (amount_rank * 45 + pct_rank * 30 + close_pos.fillna(0) * 25).round(4)
    return df.sort_values("prefilter_score", ascending=False).head(PREFILTER_LIMIT).reset_index(drop=True)


def fetch_daily(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    def prefixed(s: str) -> str:
        if s.startswith("6"):
            return "sh" + s
        if s.startswith(("0", "3")):
            return "sz" + s
        if s.startswith(("4", "8", "9")):
            return "bj" + s
        return s

    errors = []
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
            times=1,
            pause=0.5,
        )
    except Exception as exc:
        errors.append(f"hist:{exc}")
    try:
        return retry(
            lambda: ak.stock_zh_a_hist_tx(
                symbol=prefixed(symbol),
                start_date=start_date,
                end_date=end_date,
                adjust="qfq",
                timeout=8,
            ),
            times=1,
            pause=0.5,
        )
    except Exception as exc:
        errors.append(f"tx:{exc}")
    try:
        return retry(
            lambda: ak.stock_zh_a_daily(
                symbol=prefixed(symbol),
                start_date=start_date,
                end_date=end_date,
                adjust="qfq",
            ),
            times=1,
            pause=0.5,
        )
    except Exception as exc:
        errors.append(f"daily:{exc}")
    raise RuntimeError("；".join(errors))


def risk_text(plan) -> str:
    if not plan.risk_actions:
        return "无"
    return "；".join(f"{risk.action}:{risk.reason}" for risk in plan.risk_actions)


def signal_text(plan) -> str:
    if not plan.entry_signals:
        return ""
    return "；".join(f"{sig.signal_type}:{sig.reason}" for sig in plan.entry_signals)


def score_candidate(snapshot, plan, spot_row) -> tuple[float, str]:
    close_above_lines = snapshot.close >= snapshot.white_line and snapshot.close >= snapshot.yellow_line
    macd_ok = is_macd_bullish(snapshot)
    no_risk = len(plan.risk_actions) == 0
    volume_ratio = snapshot.volume_ratio
    tag_set = set(snapshot.tags)
    reasons = []
    score = 0.0

    severe_risk = any(("清仓" in r.action or "退出" in r.action or "否决" in r.action) for r in plan.risk_actions)
    reduce_risk = any("减仓" in r.action for r in plan.risk_actions)

    if no_risk:
        score += 22
        reasons.append("未触发破白/破黄/MACD否决")
    elif reduce_risk and not severe_risk:
        score -= 8
        reasons.append("有破白类风险，仅适合观察确认")
    elif severe_risk:
        score -= 28
        reasons.append("存在清仓/否决类风险，不宜主动开仓")
    if close_above_lines:
        score += 18
        reasons.append("站上白线和黄线")
    if macd_ok:
        score += 14
        reasons.append("MACD 多头")
    if "B1/B2买点" in tag_set:
        score += 18
        reasons.append("B1/B2 标签")
    if "砖形图" in tag_set:
        score += 16
        reasons.append("砖形图反红")
    if "单针战法" in tag_set:
        score += 12
        reasons.append("单针类低位信号")
    if "BBI/MA60" in tag_set:
        score += 10
        reasons.append("BBI/MA60 多头")
    if "双线/白黄线" in tag_set:
        score += 8
        reasons.append("知行趋势达标")
    if volume_ratio >= 1.2:
        score += min(10, (volume_ratio - 1.0) * 8)
        reasons.append(f"量能放大 {volume_ratio:.2f}x")
    pct = float(spot_row["pct_change"])
    if 1.0 <= pct <= 5.5:
        score += 8
        reasons.append("涨幅温和偏强")
    elif 0 < pct < 1.0:
        score += 4
        reasons.append("小阳线")
    if snapshot.close < snapshot.resistance_price:
        distance_to_resistance = (snapshot.resistance_price / snapshot.close - 1) * 100
        if distance_to_resistance >= 2:
            score += min(8, distance_to_resistance)
            reasons.append(f"距压力位约 {distance_to_resistance:.1f}%")

    return round(score, 2), "；".join(reasons)


def process_one(row: dict, engine: TradePlanEngine, start_date: str, end_date: str, active_market_value_pct: float, recent_values: list[float] = None) -> dict | None:
    symbol = row["symbol"]
    name = row["name"]
    try:
        daily = fetch_daily(symbol, start_date, end_date)
        if daily is None or daily.empty or len(daily) < 60:
            return None
        snapshot = build_snapshot(WatchSymbol(symbol=symbol, name=name, tags=[]), daily, active_market_value_pct, recent_values)
        plan = engine.build_plan(snapshot)
        if float(row.get("amount", 0) or 0) <= 0:
            row["pct_change"] = snapshot.pct_change
            row["amount"] = 0.0
        candidate_score, candidate_reason = score_candidate(snapshot, plan, row)
        if candidate_score < 25:
            return None
        return {
            "symbol": symbol,
            "name": name,
            "close": snapshot.close,
            "pct_change": round(snapshot.pct_change, 2),
            "spot_pct_change": round(float(row["pct_change"]), 2),
            "amount": round(float(row["amount"]), 2),
            "volume_ratio": round(snapshot.volume_ratio, 2),
            "white_line": snapshot.white_line,
            "yellow_line": snapshot.yellow_line,
            "support_price": snapshot.support_price,
            "resistance_price": snapshot.resistance_price,
            "macd_dif": snapshot.macd_dif,
            "macd_dea": snapshot.macd_dea,
            "tags": "；".join(snapshot.tags),
            "plan_action": plan.action,
            "suggested_position_pct": plan.suggested_position_pct,
            "plan_score": plan.score,
            "candidate_score": candidate_score,
            "candidate_reason": candidate_reason,
            "risk": risk_text(plan),
            "signals": signal_text(plan),
        }
    except Exception:
        return None


def write_csv(rows: list[dict], path: Path) -> None:
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_html(rows: list[dict], stats: dict, path: Path, active_market_value_pct: float) -> None:
    def fmt_amount(v):
        return f"{float(v) / 100000000:.2f} 亿"

    tr = []
    for i, row in enumerate(rows, 1):
        tr.append(f"""
        <tr>
          <td>{i}</td>
          <td class="code">{html.escape(row['symbol'])}</td>
          <td>{html.escape(row['name'])}</td>
          <td>{row['close']}</td>
          <td>{row['spot_pct_change']}%</td>
          <td>{fmt_amount(row['amount'])}</td>
          <td>{row['volume_ratio']}x</td>
          <td>{row['candidate_score']}</td>
          <td>{html.escape(row['plan_action'])}</td>
          <td>{row['suggested_position_pct']}%</td>
          <td>{html.escape(row['tags'])}</td>
          <td>{html.escape(row['candidate_reason'])}</td>
        </tr>
        """)

    body = f"""<!-- Generated by Trae Work -->
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>每日候选股票</title>
  <style>
    :root {{
      --bg: #f6f7fb; --bg2: #fff; --ink: #172033; --muted: #667085;
      --rule: #e5e7eb; --accent: #355cff; --danger: #b9382f;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: var(--ink); font-family: "Microsoft YaHei", system-ui, sans-serif; line-height: 1.6; }}
    main {{ max-width: 1220px; margin: 0 auto; padding: 32px 20px 48px; }}
    header, section {{ background: var(--bg2); border: 1px solid var(--rule); border-radius: 16px; padding: 22px; margin-bottom: 16px; }}
    h1 {{ margin: 0 0 8px; font-size: 30px; }}
    h2 {{ margin: 0 0 12px; font-size: 22px; }}
    p {{ color: var(--muted); margin: 0 0 12px; }}
    .cards {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-top: 16px; }}
    .card {{ border: 1px solid var(--rule); border-radius: 12px; padding: 14px; background: var(--bg); }}
    .card span {{ display: block; color: var(--muted); font-size: 13px; }}
    .card strong {{ display: block; color: var(--accent); font-size: 24px; margin-top: 4px; }}
    .table-wrap {{ overflow: auto; max-height: 720px; border: 1px solid var(--rule); border-radius: 14px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; min-width: 1180px; }}
    th, td {{ padding: 10px; border-bottom: 1px solid var(--rule); text-align: left; vertical-align: top; }}
    th {{ position: sticky; top: 0; background: var(--bg); color: var(--muted); z-index: 1; }}
    .code {{ font-family: Consolas, monospace; }}
    .note {{ border-left: 4px solid var(--accent); padding: 12px 14px; background: var(--bg); border-radius: 10px; }}
    @media (max-width: 900px) {{ .cards {{ grid-template-columns: repeat(2, 1fr); }} }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>每日候选股票</h1>
      <p>基于当前活跃市值区间、全 A 排除 300/688 股票池、实时行情预筛与历史日线规则筛选生成。仅用于每日复盘、盘前准备和交易系统验证，不构成投资建议。</p>
      <div class="cards">
        <div class="card"><span>全市场原始数</span><strong>{stats['raw_count']}</strong></div>
        <div class="card"><span>入池股票数</span><strong>{stats['pool_count']}</strong></div>
        <div class="card"><span>历史计算数</span><strong>{stats['prefilter_count']}</strong></div>
        <div class="card"><span>候选数</span><strong>{len(rows)}</strong></div>
        <div class="card"><span>活跃市值</span><strong>{active_market_value_pct}%</strong></div>
      </div>
    </header>
    <section>
      <h2>筛选口径</h2>
      <p class="note">先排除 300/688、ST/退市、无成交票；再排除涨停附近、当日明显走弱和流动性过低股票；最后要求未触发破白/破黄/MACD 否决，且站上白线黄线、MACD 多头，并具备趋势或买点类标签。</p>
    </section>
    <section>
      <h2>候选清单</h2>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>排名</th><th>代码</th><th>名称</th><th>收盘</th><th>涨跌幅</th><th>成交额</th><th>量比</th><th>候选分</th><th>系统动作</th><th>建议仓位</th><th>标签</th><th>入选理由</th>
            </tr>
          </thead>
          <tbody>{''.join(tr)}</tbody>
        </table>
      </div>
    </section>
  </main>
</body>
</html>
"""
    path.write_text(body, encoding="utf-8")


def main():
    outputs = PROJECT_ROOT / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)

    active_market_value_pct = load_active_market_value_pct()
    recent_values = load_recent_active_market_values(days=3)

    spot = load_spot()
    pool = filter_pool(spot)
    watchlist_path = save_watchlist(pool)
    config_path = save_config()
    pre = prefilter_for_history(pool)

    end = date.today()
    start = end - timedelta(days=HISTORY_DAYS + 30)
    start_date = start.strftime("%Y%m%d")
    end_date = end.strftime("%Y%m%d")

    rulebook = Rulebook.load(PROJECT_ROOT / "config" / "rulebook.json")
    engine = TradePlanEngine(rulebook)

    rows = []
    records = pre.to_dict("records")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_one, row, engine, start_date, end_date, active_market_value_pct, recent_values) for row in records]
        for idx, future in enumerate(as_completed(futures), 1):
            result = future.result()
            if result:
                rows.append(result)
            if idx % 50 == 0:
                print(f"已计算 {idx}/{len(records)}，候选 {len(rows)}")

    rows = sorted(rows, key=lambda item: item["candidate_score"], reverse=True)[:TOP_N]
    stats = {
        "raw_count": int(len(spot)),
        "pool_count": int(len(pool)),
        "prefilter_count": int(len(pre)),
        "watchlist_path": str(watchlist_path),
        "config_path": str(config_path),
    }

    daily_csv = outputs / "daily_candidates.csv"
    daily_html = outputs / "daily_candidates.html"
    daily_json = outputs / "daily_candidates.json"
    write_csv(rows, daily_csv)
    write_html(rows, stats, daily_html, active_market_value_pct)
    daily_json.write_text(json.dumps({"stats": stats, "candidates": rows}, ensure_ascii=False, indent=2), encoding="utf-8")

    # 兼容旧版链接，避免之前的报告入口失效。
    shutil.copyfile(daily_csv, outputs / "next_monday_candidates.csv")
    shutil.copyfile(daily_html, outputs / "next_monday_candidates.html")
    shutil.copyfile(daily_json, outputs / "next_monday_candidates.json")
    print(json.dumps({"stats": stats, "candidate_count": len(rows), "top": rows[:10]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
