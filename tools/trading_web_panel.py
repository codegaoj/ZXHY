from __future__ import annotations

import csv
import json
import mimetypes
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
PORT = 8765

STATE = {
    "running": False,
    "action": "",
    "started_at": "",
    "finished_at": "",
    "exit_code": None,
    "log": [],
}
STATE_LOCK = threading.Lock()


COMMANDS = {
    "refresh_spot": {
        "name": "刷新全市场实时行情缓存",
        "cmd": [
            sys.executable,
            "-c",
            "import akshare as ak; df=ak.stock_zh_a_spot(); df.to_csv('data/a_spot_latest.csv', index=False, encoding='utf-8-sig'); print('已输出 data/a_spot_latest.csv，股票数：', len(df))",
        ],
    },
    "daily_report": {
        "name": "生成每日收盘报告",
        "cmd": [
            sys.executable,
            "-m",
            "src.trading_system.cli",
            "daily-report",
            "--config",
            "config/data_source_akshare.json",
        ],
    },
    "backtest": {
        "name": "运行规则回测",
        "cmd": [
            sys.executable,
            "-m",
            "src.trading_system.cli",
            "backtest-rules",
            "--config",
            "config/data_source_akshare.json",
            "--days",
            "260",
            "--holding-days",
            "5",
            "--limit",
            "0",
        ],
    },
    "screen_candidates": {
        "name": "生成每日候选股票",
        "cmd": [
            sys.executable,
            "tools/screen_daily_candidates.py",
        ],
    },
    "review_candidates": {
        "name": "复盘昨日候选股票",
        "cmd": [
            sys.executable,
            "tools/review_yesterday_candidates.py",
        ],
    },
    "periodic_review_weekly": {
        "name": "按周复盘汇总",
        "cmd": [
            sys.executable,
            "tools/periodic_review.py",
            "--period",
            "weekly",
        ],
    },
    "periodic_review_monthly": {
        "name": "按月复盘汇总",
        "cmd": [
            sys.executable,
            "tools/periodic_review.py",
            "--period",
            "monthly",
        ],
    },
    "periodic_review_all": {
        "name": "全部历史复盘",
        "cmd": [
            sys.executable,
            "tools/periodic_review.py",
            "--period",
            "all",
        ],
    },
    "simulation": {
        "name": "运行模拟交易",
        "cmd": [
            sys.executable,
            "tools/run_simulation.py",
            "--run",
        ],
    },
}

DAILY_WORKFLOW = {
    "name": "每日一键执行",
    "steps": ["refresh_spot", "review_candidates", "screen_candidates", "simulation"],
}


REPORT_LINKS = [
    ("每日候选股票报告", "outputs/daily_candidates.html"),
    ("候选股票复盘报告", "outputs/review_yesterday_candidates.html"),
    ("候选股票复盘 JSON", "outputs/review_yesterday_candidates.json"),
    ("按周复盘汇总报告", "outputs/periodic_review_weekly.html"),
    ("按月复盘汇总报告", "outputs/periodic_review_monthly.html"),
    ("全部历史复盘报告", "outputs/periodic_review_all.html"),
    ("每日候选 CSV 明细", "outputs/daily_candidates.csv"),
    ("每日收盘报告", "outputs/daily_close_report.html"),
    ("完整分析报告", "outputs/complete-analysis-report/complete-analysis-report.html"),
    ("操作手册", "docs/操作手册.html"),
    ("回测 Markdown", "outputs/backtest_rules.md"),
]


def _active_market_value_path() -> Path:
    return PROJECT_ROOT / "data" / "compass_active_market_value.csv"


def _read_active_market_value() -> dict:
    path = _active_market_value_path()
    if not path.exists():
        return {"date": "", "value": "", "source": "compass", "note": "文件不存在", "recent": [], "cumulative_3d": "", "market_regime": "", "market_reason": "", "max_position_pct": ""}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return {"date": "", "value": "", "source": "compass", "note": "文件为空", "recent": [], "cumulative_3d": "", "market_regime": "", "market_reason": "", "max_position_pct": ""}
    row = rows[-1]
    recent = []
    for r in rows[-3:]:
        raw = r.get("active_market_value_pct") or r.get("active_market_value")
        if raw is not None and str(raw).strip() != "":
            recent.append(float(str(raw).strip()))
    cum_sum = round(sum(recent), 4) if recent else 0.0
    amv = float(row.get("active_market_value_pct", row.get("active_market_value", "0")) or "0")

    market_regime = ""
    market_reason = ""
    max_position_pct = ""
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from trading_system.rulebook import Rulebook
        from trading_system.engines import MarketTimingEngine
        rb = Rulebook.load(PROJECT_ROOT / "config" / "rulebook.json")
        engine = MarketTimingEngine(rb)
        macd_dif = recent[-1] if recent else 0.0
        state = engine.evaluate(amv, macd_dif=macd_dif, recent_values=recent)
        market_regime = state.regime.value
        market_reason = state.reason
        max_position_pct = state.max_position_pct
    except Exception:
        pass

    return {
        "date": row.get("date", ""),
        "value": row.get("active_market_value_pct", row.get("active_market_value", "")),
        "source": row.get("source", "compass"),
        "note": row.get("note", ""),
        "recent": recent,
        "cumulative_3d": f"{cum_sum:.2f}",
        "market_regime": market_regime,
        "market_reason": market_reason,
        "max_position_pct": max_position_pct,
    }


def _write_active_market_value(value: float, note: str = "") -> dict:
    path = _active_market_value_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    text_value = f"{value:.4f}".rstrip("0").rstrip(".")
    new_row = {
        "date": today,
        "active_market_value": text_value,
        "active_market_value_pct": text_value,
        "source": "compass",
        "note": note or "通过操作面板更新",
    }

    # 读取已有行，同日覆盖、异日追加，保留最近 30 天
    existing_rows = []
    if path.exists():
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            existing_rows = [row for row in csv.DictReader(f) if row]

    # 如果今天已有记录则替换，否则追加
    updated = False
    for i, row in enumerate(existing_rows):
        if row.get("date") == today:
            existing_rows[i] = new_row
            updated = True
            break
    if not updated:
        existing_rows.append(new_row)

    # 只保留最近 30 行
    existing_rows = existing_rows[-30:]

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "active_market_value", "active_market_value_pct", "source", "note"])
        writer.writeheader()
        writer.writerows(existing_rows)

    _append_log(f"已更新活跃市值：{text_value}，日期：{today}（累计保留{len(existing_rows)}天）")

    # SQLite 持久化
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from trading_system.storage import Storage
        with Storage() as db:
            db.save_active_market_value(today, value, value, source="compass", note=note or "通过操作面板更新")
            _append_log(f"[DB] 活跃市值已入库：{today}")
    except Exception as exc:
        _append_log(f"[DB] 活跃市值入库失败：{exc}")

    return _read_active_market_value()


def _read_tdx_candidate_codes(limit: int | None = None) -> dict:
    path = PROJECT_ROOT / "outputs" / "daily_candidates.csv"
    if not path.exists():
        return {
            "ok": False,
            "error": "未找到每日候选股票文件，请先点击“生成每日候选股票”。",
            "codes": [],
            "text": "",
            "count": 0,
        }
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    codes = []
    seen = set()
    for row in rows:
        code = str(row.get("symbol", "")).strip().zfill(6)
        if code and code not in seen:
            codes.append(code)
            seen.add(code)
    if limit:
        codes = codes[:limit]

    return {
        "ok": True,
        "codes": codes,
        "text": "\n".join(codes),
        "count": len(codes),
    }


def _to_float(value, default=0.0) -> float:
    try:
        if value is None or str(value).strip() in {"", "-", "nan"}:
            return default
        return float(value)
    except Exception:
        return default


# ---- 实时行情缓存（避免每次点击候选股都拉全市场行情）----
_SPOT_CACHE: dict = {"df": None, "fetched_at": 0.0}
_SPOT_CACHE_TTL = 300  # 5 分钟缓存


def _prefixed_symbol(symbol: str) -> str:
    if symbol.startswith("6"):
        return "sh" + symbol
    if symbol.startswith(("0", "3")):
        return "sz" + symbol
    if symbol.startswith(("4", "8", "9")):
        return "bj" + symbol
    return symbol


def _get_spot_df():
    """获取全市场实时行情（带 5 分钟缓存）。"""
    import time as _time

    now = _time.time()
    if _SPOT_CACHE["df"] is not None and (now - _SPOT_CACHE["fetched_at"]) < _SPOT_CACHE_TTL:
        return _SPOT_CACHE["df"]
    try:
        import akshare as ak

        df = ak.stock_zh_a_spot()
        _SPOT_CACHE["df"] = df
        _SPOT_CACHE["fetched_at"] = now
        return df
    except Exception:
        return _SPOT_CACHE["df"]


def _is_trading_day(d: date | None = None) -> bool:
    """判断是否为交易日（使用 AkShare 交易日历，回退到周末判断）。"""
    from trading_system.trade_calendar import is_trading_day as _tc_is_trading_day
    return _tc_is_trading_day(d)


def _last_trading_day(d: date | None = None) -> date:
    """返回最近一个交易日，如果当天是交易日则返回当天，否则往前推。"""
    from trading_system.trade_calendar import last_trading_day as _tc_last_trading_day
    return _tc_last_trading_day(d)


def _supplement_with_spot(df, symbol: str):
    """如果日线数据不含今天，用实时行情补一行今日数据（仅交易日补全）。"""
    import pandas as pd

    if df is None or df.empty:
        return df
    # 非交易日不补全，避免周六日出现假K线
    if not _is_trading_day():
        return df
    today_str = date.today().isoformat()
    last_date = str(df.iloc[-1]["date"]).split(" ")[0]
    if last_date == today_str:
        return df  # 已有今天数据

    spot_df = _get_spot_df()
    if spot_df is None or spot_df.empty or "代码" not in spot_df.columns:
        return df

    sym_prefixed = _prefixed_symbol(symbol)
    spot_row = spot_df[spot_df["代码"] == sym_prefixed]
    if spot_row.empty:
        spot_row = spot_df[spot_df["代码"] == symbol]
    if spot_row.empty:
        return df

    sr = spot_row.iloc[0]
    today_data = {
        "date": pd.Timestamp.today().normalize(),
        "open": _to_float(sr.get("今开")),
        "close": _to_float(sr.get("最新价")),
        "high": _to_float(sr.get("最高")),
        "low": _to_float(sr.get("最低")),
        "volume": _to_float(sr.get("成交量")),
    }
    if today_data["close"] <= 0:
        return df
    return pd.concat([df, pd.DataFrame([today_data])], ignore_index=True)


def _read_agent_review() -> dict:
    path = PROJECT_ROOT / "outputs" / "agent_review.json"
    if not path.exists():
        return {"ok": False, "reviews": {}, "warnings": [], "recommendations": [], "summary": "", "stats": {}}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        reviews_map = {r["symbol"]: r for r in data.get("reviews", [])}
        return {
            "ok": True,
            "reviews": reviews_map,
            "warnings": data.get("warnings", []),
            "recommendations": data.get("recommendations", []),
            "summary": data.get("summary", ""),
            "stats": data.get("stats", {}),
        }
    except Exception:
        return {"ok": False, "reviews": {}, "warnings": [], "recommendations": [], "summary": "", "stats": {}}


def _read_candidate_rows(limit: int = 40) -> dict:
    path = PROJECT_ROOT / "outputs" / "daily_candidates.csv"
    if not path.exists():
        return {"ok": False, "error": "未找到每日候选股票文件，请先生成每日候选股票。", "items": []}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    agent_review = _read_agent_review()
    items = []
    for row in rows[:limit]:
        symbol = str(row.get("symbol", "")).strip().zfill(6)
        review = agent_review["reviews"].get(symbol, {})
        items.append({
            "symbol": symbol,
            "name": str(row.get("name", "")).strip(),
            "pct_change": _to_float(row.get("pct_change")),
            "close": _to_float(row.get("close")),
            "tags": str(row.get("tags", "")).strip(),
            "score": _to_float(row.get("candidate_score")),
            "reason": str(row.get("candidate_reason", "")).strip(),
            "chart_quality": int(_to_float(row.get("chart_quality"), 50)),
            "chart_grade": str(row.get("chart_grade", "")).strip(),
            "sector": str(row.get("sector", "")).strip(),
            "sector_strength": str(row.get("sector_strength", "")).strip(),
            "sector_change": _to_float(row.get("sector_change")),
            "opinion": review.get("opinion", ""),
            "issues": review.get("issues", []),
            "highlights": review.get("highlights", []),
            "risk_reward": review.get("risk_reward", 0),
            "risk_level": review.get("risk_level", 0),
            "position_suggestion": review.get("position_suggestion", ""),
            "brief": review.get("brief", ""),
        })
    return {
        "ok": True,
        "items": items,
        "count": len(items),
        "agent_warnings": agent_review["warnings"],
        "agent_summary": agent_review["summary"],
        "agent_recommendations": agent_review["recommendations"],
        "agent_stats": agent_review["stats"],
    }


def _pick_col(df, names: list[str]) -> str:
    for name in names:
        if name in df.columns:
            return name
    raise ValueError(f"缺少字段：{names}")


def _normalize_detail_daily(raw):
    import pandas as pd

    date_col = _pick_col(raw, ["date", "日期", "trade_date"])
    open_col = _pick_col(raw, ["open", "开盘", "开盘价"])
    close_col = _pick_col(raw, ["close", "收盘", "收盘价"])
    high_col = _pick_col(raw, ["high", "最高", "最高价"])
    low_col = _pick_col(raw, ["low", "最低", "最低价"])
    volume_col = _pick_col(raw, ["volume", "成交量", "vol"])
    df = raw.rename(columns={
        date_col: "date",
        open_col: "open",
        close_col: "close",
        high_col: "high",
        low_col: "low",
        volume_col: "volume",
    }).copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for col in ["open", "close", "high", "low", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["date", "open", "close", "high", "low", "volume"]).sort_values("date").reset_index(drop=True)


def _sma_cn_detail(series, n: int, m: int = 1):
    import pandas as pd

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


def _safe_ratio_detail(numerator, denominator, default: float = 0.0):
    denominator = denominator.replace(0, None)
    return (numerator / denominator).fillna(default)


def _read_history_list() -> dict:
    """扫描 outputs/history/ 目录，返回历史记录列表。"""
    history_dir = PROJECT_ROOT / "outputs" / "history"
    if not history_dir.exists():
        return {"ok": True, "candidates": [], "reviews": [], "periodic": [], "total": 0}

    candidates = []
    reviews = []
    periodic = []

    for f in sorted(history_dir.iterdir(), reverse=True):
        if not f.is_file():
            continue
        name = f.name
        size_kb = round(f.stat().st_size / 1024, 1)
        mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        entry = {"name": name, "size_kb": size_kb, "mtime": mtime, "url": f"/view/outputs/history/{name}"}

        if name.startswith("daily_candidates_"):
            candidates.append(entry)
        elif name.startswith("review_"):
            reviews.append(entry)
        elif name.startswith("periodic_review_"):
            periodic.append(entry)

    return {
        "ok": True,
        "candidates": candidates,
        "reviews": reviews,
        "periodic": periodic,
        "total": len(candidates) + len(reviews) + len(periodic),
    }


_DETAIL_CACHE: dict[str, tuple[float, "pd.DataFrame"]] = {}


def _read_candidate_detail(symbol: str) -> dict:
    symbol = "".join(ch for ch in str(symbol) if ch.isdigit()).zfill(6)
    if len(symbol) != 6:
        return {"ok": False, "error": "股票代码不正确。"}

    candidates = _read_candidate_rows()
    item = next((x for x in candidates.get("items", []) if x["symbol"] == symbol), {"symbol": symbol, "name": ""})

    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    import pandas as pd
    from trading_system.data_provider import AkShareProvider
    from trading_system.data_normalizer import calculate_macd
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

    end = _last_trading_day()
    start = end - timedelta(days=180)

    cache_key = f"{symbol}_{start}_{end}"
    now = time.time()
    cached = _DETAIL_CACHE.get(cache_key)
    if cached and (now - cached[0]) < 1800:
        raw = cached[1]
    else:
        provider = AkShareProvider(adjust="qfq", retries=1, pause_seconds=0.4)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    lambda p: p.fetch_daily(symbol, start.strftime("%Y%m%d"), end.strftime("%Y%m%d")),
                    provider,
                )
                raw = future.result(timeout=20)
            _DETAIL_CACHE[cache_key] = (now, raw)
        except FutureTimeout:
            return {"ok": False, "error": f"拉取 {symbol} 日线超时（20秒），请稍后重试。"}
        except Exception as exc:
            return {"ok": False, "error": f"拉取日线失败：{exc}"}

    df = _normalize_detail_daily(raw)
    # 用实时行情补全今日数据（东方财富不可用时日线可能缺今天）
    df = _supplement_with_spot(df, symbol)
    if len(df) < 30:
        return {"ok": False, "error": f"{symbol} 有效日线不足，暂不能绘图。"}

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    zxdq = close.ewm(span=10, adjust=False).mean().ewm(span=10, adjust=False).mean()
    zxdkx = (
        close.rolling(14, min_periods=1).mean()
        + close.rolling(28, min_periods=1).mean()
        + close.rolling(57, min_periods=1).mean()
        + close.rolling(114, min_periods=1).mean()
    ) / 4
    macd = calculate_macd(close)
    df = pd.concat([df, macd], axis=1)
    df["white_line"] = zxdq
    df["yellow_line"] = zxdkx
    high4 = high.rolling(4, min_periods=1).max()
    low4 = low.rolling(4, min_periods=1).min()
    var1a = _safe_ratio_detail(high4 - close, high4 - low4) * 100 - 90
    var2a = _sma_cn_detail(var1a, 4, 1) + 100
    var3a = _safe_ratio_detail(close - low4, high4 - low4) * 100
    var4a = _sma_cn_detail(var3a, 6, 1)
    var5a = _sma_cn_detail(var4a, 6, 1) + 100
    var6a = var5a - var2a
    df["brick"] = (var6a - 4).where(var6a > 4, 0)
    view = df.tail(60).copy()
    recent20 = df.tail(20)
    support = float(recent20["low"].min())
    resistance = float(recent20["high"].max())

    records = []
    for _, row in view.iterrows():
        records.append({
            "date": row["date"].strftime("%m-%d"),
            "open": round(float(row["open"]), 4),
            "close": round(float(row["close"]), 4),
            "high": round(float(row["high"]), 4),
            "low": round(float(row["low"]), 4),
            "volume": int(row.get("volume", 0) or 0),
            "white": round(float(row["white_line"]), 4),
            "yellow": round(float(row["yellow_line"]), 4),
            "dif": round(float(row["macd_dif"]), 4),
            "dea": round(float(row["macd_dea"]), 4),
            "hist": round(float((row["macd_dif"] - row["macd_dea"]) * 2), 4),
            "brick": round(float(row["brick"]), 4),
        })

    latest = records[-1]
    return {
        "ok": True,
        "symbol": symbol,
        "name": item.get("name", ""),
        "tags": item.get("tags", ""),
        "candidate_reason": item.get("reason", ""),
        "support": round(support, 4),
        "resistance": round(resistance, 4),
        "latest": latest,
        "records": records,
        "opinion": item.get("opinion", ""),
        "issues": item.get("issues", []),
        "highlights": item.get("highlights", []),
        "risk_reward": item.get("risk_reward", 0),
        "risk_level": item.get("risk_level", 0),
        "position_suggestion": item.get("position_suggestion", ""),
        "brief": item.get("brief", ""),
    }


def _append_log(line: str) -> None:
    with STATE_LOCK:
        STATE["log"].append(line.rstrip())
        STATE["log"] = STATE["log"][-500:]


def _run_action(action: str, market_override: str = None) -> None:
    item = COMMANDS[action]
    cmd = list(item["cmd"])
    if market_override and action == "screen_candidates":
        cmd.extend(["--market-override", market_override])
    with STATE_LOCK:
        STATE.update({
            "running": True,
            "action": item["name"],
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": "",
            "exit_code": None,
            "log": [f"开始：{item['name']}"],
        })

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            _append_log(line)
        code = proc.wait()
    except Exception as exc:
        code = -1
        _append_log(f"执行失败：{exc}")

    with STATE_LOCK:
        STATE.update({
            "running": False,
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "exit_code": code,
        })
        STATE["log"].append("完成。" if code == 0 else f"结束，退出码：{code}")


def _run_daily_workflow(market_override: str = None) -> None:
    """串行执行每日一键执行流程：刷新行情 → 复盘昨日候选股 → 生成今日候选股票 → 运行模拟交易。"""
    steps = DAILY_WORKFLOW["steps"]
    total = len(steps)
    with STATE_LOCK:
        STATE.update({
            "running": True,
            "action": DAILY_WORKFLOW["name"],
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": "",
            "exit_code": None,
            "log": [f"开始：{DAILY_WORKFLOW['name']}（共{total}步）"],
        })

    overall_code = 0
    for idx, step in enumerate(steps, 1):
        item = COMMANDS[step]
        cmd = list(item["cmd"])
        if market_override and step == "screen_candidates":
            cmd.extend(["--market-override", market_override])
        with STATE_LOCK:
            STATE["action"] = f"{DAILY_WORKFLOW['name']} - 第{idx}/{total}步：{item['name']}"
            STATE["log"].append(f"--- 第{idx}/{total}步：{item['name']} ---")

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=PROJECT_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                _append_log(line)
            code = proc.wait()
        except Exception as exc:
            code = -1
            _append_log(f"执行失败：{exc}")

        if code != 0:
            overall_code = code
            _append_log(f"第{idx}步「{item['name']}」失败，退出码：{code}，暂停后续步骤。")
            break
        else:
            _append_log(f"第{idx}步「{item['name']}」完成。")

    with STATE_LOCK:
        STATE.update({
            "running": False,
            "action": DAILY_WORKFLOW["name"],
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "exit_code": overall_code,
        })
        if overall_code == 0:
            STATE["log"].append("每日一键执行全部完成。")
        else:
            STATE["log"].append(f"每日一键执行中断，退出码：{overall_code}")


def _safe_project_file(relative_path: str) -> Path | None:
    relative_path = unquote(relative_path).lstrip("/").replace("\\", "/")
    path = (PROJECT_ROOT / relative_path).resolve()
    try:
        path.relative_to(PROJECT_ROOT)
    except ValueError:
        return None
    if not path.exists() or not path.is_file():
        return None
    return path


def _db_summary() -> dict:
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from trading_system.storage import Storage
        with Storage() as db:
            return {"ok": True, **db.get_summary()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _db_candidates(target_date: str) -> dict:
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from trading_system.storage import Storage
        with Storage() as db:
            candidates = db.query_daily_candidates(target_date)
            state = db.get_market_state(target_date)
            return {"ok": True, "date": target_date, "count": len(candidates), "candidates": candidates, "market_state": state}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _db_reviews(target_date: str) -> dict:
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from trading_system.storage import Storage
        with Storage() as db:
            reviews = db.query_candidate_reviews(target_date)
            stats = db.get_review_stats(target_date)
            return {"ok": True, "selected_date": target_date, "count": len(reviews), "rows": reviews, "stats": stats}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _db_symbol_history(symbol: str) -> dict:
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from trading_system.storage import Storage
        with Storage() as db:
            candidates = db.get_candidate_history(symbol)
            reviews = db.get_review_history(symbol)
            return {"ok": True, "symbol": symbol, "candidate_history": candidates, "review_history": reviews}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _holdings_list() -> dict:
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from trading_system.storage import Storage
        with Storage() as db:
            return {"ok": True, "holdings": db.get_open_holdings()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _holdings_signals() -> dict:
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from trading_system.portfolio_engine import PortfolioEngine
        engine = PortfolioEngine()
        results = engine.check_holdings()
        return {"ok": True, "count": len(results), "holdings": results}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _holdings_add(payload: dict) -> dict:
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from trading_system.storage import Storage
        with Storage() as db:
            hid = db.add_holding(
                symbol=str(payload.get("symbol", "")).strip().zfill(6),
                name=payload.get("name", "").strip(),
                buy_date=payload.get("buy_date", ""),
                buy_price=float(payload.get("buy_price", 0)),
                shares=int(payload.get("shares", 0)),
                commission=float(payload.get("commission", 0) or 0),
                note=payload.get("note", "").strip(),
            )
            return {"ok": True, "id": hid}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _holdings_update(holding_id: int, payload: dict) -> dict:
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from trading_system.storage import Storage
        with Storage() as db:
            db.update_holding(holding_id, **payload)
            return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _holdings_delete(holding_id: int) -> dict:
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from trading_system.storage import Storage
        with Storage() as db:
            db.delete_holding(holding_id)
            return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _holdings_sell(holding_id: int, payload: dict) -> dict:
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from trading_system.storage import Storage
        with Storage() as db:
            result = db.close_holding(
                holding_id,
                sell_date=payload.get("sell_date", ""),
                sell_price=float(payload.get("sell_price", 0)),
                sell_shares=int(payload.get("sell_shares", 0)),
                commission=float(payload.get("commission", 0) or 0),
                note=payload.get("note", "").strip(),
            )
            return {"ok": True, **result}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _holdings_import(payload: dict) -> dict:
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from trading_system.storage import Storage
        rows = payload.get("rows", [])
        with Storage() as db:
            count = db.import_holdings_csv(rows)
            return {"ok": True, "imported": count}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _transactions_list(symbol: str = "") -> dict:
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from trading_system.storage import Storage
        with Storage() as db:
            return {"ok": True, "transactions": db.get_transactions(symbol)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ─── 模拟交易辅助函数 ───

SIM_DEFAULT_ACCOUNT_NAME = "验证账户-全买模式"


def _sim_get_account() -> dict:
    """获取模拟账户信息（含最新净值和绩效统计）。"""
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from trading_system.storage import Storage
        with Storage() as db:
            account = db.get_sim_account_by_name(SIM_DEFAULT_ACCOUNT_NAME)
            if not account:
                return {"ok": False, "error": "模拟账户不存在，请先运行模拟交易初始化"}
            # 获取最新净值
            latest_nav = db.get_latest_sim_nav(account["id"])
            # 获取持仓数
            open_holdings = db.get_sim_open_holdings(account["id"])
            # 计算胜率（已平仓交易）
            all_holdings = db.get_sim_all_holdings(account["id"], limit=500)
            closed = [h for h in all_holdings if h["status"] == "closed"]
            wins = [h for h in closed if h["pnl"] and h["pnl"] > 0]
            win_rate = round(len(wins) / len(closed) * 100, 1) if closed else 0.0

            total_return_pct = 0.0
            current_nav = account["initial_capital"]
            if latest_nav:
                total_return_pct = latest_nav.get("total_return_pct", 0.0)
                current_nav = latest_nav.get("nav", account["initial_capital"])

            return {
                "ok": True,
                "account": {
                    "id": account["id"],
                    "name": account["name"],
                    "initial_capital": account["initial_capital"],
                    "current_cash": account["current_cash"],
                    "total_value": account["total_value"],
                    "current_nav": round(current_nav, 2),
                    "total_return_pct": round(total_return_pct, 2),
                    "holding_count": len(open_holdings),
                    "win_rate": win_rate,
                    "closed_count": len(closed),
                    "win_count": len(wins),
                    "start_date": account.get("start_date", ""),
                },
            }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _sim_get_holdings() -> dict:
    """获取模拟账户当前持仓。"""
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from trading_system.storage import Storage
        with Storage() as db:
            account = db.get_sim_account_by_name(SIM_DEFAULT_ACCOUNT_NAME)
            if not account:
                return {"ok": False, "error": "模拟账户不存在"}
            holdings = db.get_sim_open_holdings(account["id"])
            return {"ok": True, "holdings": holdings}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _sim_get_nav_history() -> dict:
    """获取模拟账户净值历史。"""
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from trading_system.storage import Storage
        with Storage() as db:
            account = db.get_sim_account_by_name(SIM_DEFAULT_ACCOUNT_NAME)
            if not account:
                return {"ok": False, "error": "模拟账户不存在"}
            history = db.get_sim_nav_history(account["id"], limit=500)
            return {"ok": True, "history": history, "initial_capital": account["initial_capital"]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _sim_get_transactions() -> dict:
    """获取模拟账户交易记录。"""
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from trading_system.storage import Storage
        with Storage() as db:
            account = db.get_sim_account_by_name(SIM_DEFAULT_ACCOUNT_NAME)
            if not account:
                return {"ok": False, "error": "模拟账户不存在"}
            transactions = db.get_sim_transactions(account["id"], limit=200)
            return {"ok": True, "transactions": transactions}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _sim_run() -> dict:
    """运行今日模拟交易（异步执行，返回任务状态）。"""
    with STATE_LOCK:
        if STATE["running"]:
            return {"ok": False, "error": "已有任务正在运行，请等待完成"}
    threading.Thread(target=_run_simulation_task, daemon=True).start()
    return {"ok": True, "message": "模拟交易已启动"}


def _run_simulation_task() -> None:
    """后台执行模拟交易任务。"""
    with STATE_LOCK:
        STATE["running"] = True
        STATE["action"] = "模拟交易"
        STATE["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        STATE["finished_at"] = ""
        STATE["exit_code"] = None
        STATE["log"] = ["[模拟交易] 开始执行..."]

    try:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from tools.run_simulation import run_simulation
        result = run_simulation()
        with STATE_LOCK:
            STATE["log"].append(f"[模拟交易] 执行完成")
            if result.get("nav"):
                STATE["log"].append(f"  净值日期：{result['nav'].get('date', '-')}")
                STATE["log"].append(f"  当前净值：{result['nav'].get('nav', '-')}")
                STATE["log"].append(f"  累计收益：{result['nav'].get('total_return_pct', '-')}%")
            if result.get("performance"):
                perf = result["performance"]
                STATE["log"].append(f"  总交易次数：{perf.get('total_trades', '-')}")
                STATE["log"].append(f"  胜率：{perf.get('win_rate', '-')}%")
            STATE["exit_code"] = 0
    except Exception as exc:
        with STATE_LOCK:
            STATE["log"].append(f"[模拟交易] 执行失败：{exc}")
            STATE["exit_code"] = 1
    finally:
        with STATE_LOCK:
            STATE["running"] = False
            STATE["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _html_page() -> str:
    links = "\n".join(
        f'<a class="report-link" href="/view/{path}" target="_blank"><span>{name}</span><small>{path}</small></a>'
        for name, path in REPORT_LINKS
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>A股交易系统操作面板</title>
  <style>
    :root {{
      --bg:#f6f7fb; --bg2:#fff; --ink:#172033; --muted:#667085;
      --rule:#d9deea; --accent:#355cff; --danger:#b9382f; --success:#178a54;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; background: var(--bg); color: var(--ink);
      font-family: "Microsoft YaHei", "PingFang SC", system-ui, sans-serif;
      line-height: 1.6;
    }}
    main {{ width: min(1180px, calc(100% - 32px)); margin: 0 auto; padding: 28px 0 44px; }}
    header, section {{
      background: var(--bg2); border: 1px solid var(--rule); border-radius: 18px;
      padding: 22px; margin-bottom: 16px;
    }}
    h1 {{ margin: 0 0 8px; font-size: clamp(28px, 4vw, 42px); letter-spacing: -0.03em; }}
    h2 {{ margin: 0 0 12px; font-size: 22px; }}
    p {{ margin: 0; color: var(--muted); }}
    .grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }}
    button {{
      border: 1px solid var(--rule); border-radius: 14px; padding: 16px; min-height: 112px;
      background: var(--bg); color: var(--ink); text-align: left; cursor: pointer;
      font: inherit; transition: transform .12s ease, border-color .12s ease, background .12s ease;
    }}
    button:hover {{ transform: translateY(-1px); border-color: var(--accent); }}
    button:disabled {{ opacity: .55; cursor: not-allowed; transform: none; }}
    button strong {{ display: block; color: var(--accent); font-size: 17px; margin-bottom: 6px; }}
    button span {{ display: block; color: var(--muted); font-size: 13px; }}
    .form-row {{
      display: grid; grid-template-columns: 1fr 1fr auto; gap: 10px; align-items: end; margin-top: 14px;
    }}
    label span {{ display: block; color: var(--muted); font-size: 13px; margin-bottom: 6px; }}
    input {{
      width: 100%; border: 1px solid var(--rule); border-radius: 12px; padding: 12px;
      font: inherit; color: var(--ink); background: var(--bg);
    }}
    .primary-button {{
      min-height: auto; text-align: center; background: var(--accent); color: white; border-color: var(--accent);
      padding: 12px 16px; white-space: nowrap;
    }}
    .primary-button:hover {{ border-color: var(--accent); }}
    .primary-button strong {{ color: white; margin: 0; font-size: 15px; }}
    .status {{
      display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-top: 14px;
    }}
    .stat {{ border: 1px solid var(--rule); background: var(--bg); border-radius: 12px; padding: 12px; }}
    .stat span {{ display: block; color: var(--muted); font-size: 12px; }}
    .stat strong {{ display: block; margin-top: 2px; color: var(--ink); font-size: 15px; word-break: break-all; }}
    .reports {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }}
    .report-link {{
      display: block; text-decoration: none; color: var(--ink); background: var(--bg);
      border: 1px solid var(--rule); border-radius: 12px; padding: 12px;
    }}
    .report-link:hover {{ border-color: var(--accent); }}
    .report-link span {{ display: block; font-weight: 700; }}
    .report-link small {{ display: block; margin-top: 4px; color: var(--muted); word-break: break-all; }}
    pre {{
      margin: 0; padding: 14px; max-height: 420px; overflow: auto;
      background: #101828; color: #f8fafc; border-radius: 14px; font-size: 13px; line-height: 1.55;
      white-space: pre-wrap;
    }}
    .hint {{
      margin-top: 12px; padding: 12px 14px; border-left: 4px solid var(--accent);
      background: var(--bg); border-radius: 10px; color: var(--ink);
    }}
    .tdx-actions {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; }}
    .tdx-actions .primary-button {{ min-height: auto; }}
    .tdx-text {{ margin-top: 12px; display: none; }}
    .tdx-text textarea {{
      width: 100%; min-height: 180px; resize: vertical; border: 1px solid var(--rule);
      border-radius: 14px; padding: 14px; font: 15px/1.6 "JetBrains Mono", Consolas, monospace;
      color: var(--ink); background: var(--bg);
    }}
    .tdx-meta {{ margin-top: 8px; color: var(--muted); font-size: 13px; }}
    .candidate-layout {{ display: grid; grid-template-columns: 320px 1fr; gap: 14px; margin-top: 14px; }}
    .candidate-list {{
      max-height: 560px; overflow: auto; border: 1px solid var(--rule);
      border-radius: 14px; background: var(--bg); padding: 8px;
    }}
    .candidate-item {{
      width: 100%; min-height: auto; display: block; text-align: left; margin-bottom: 8px;
      background: #fff; border-radius: 12px; padding: 10px 12px;
    }}
    .candidate-item strong {{ color: var(--ink); font-size: 15px; margin: 0; }}
    .candidate-item span {{ font-size: 12px; }}
    .candidate-item.active {{ border-color: var(--accent); box-shadow: 0 0 0 2px rgba(53,92,255,.12); }}
    .chart-card {{ border: 1px solid var(--rule); border-radius: 14px; background: var(--bg); padding: 14px; min-height: 560px; }}
    .chart-meta {{ display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 10px; margin: 12px 0; }}
    .chart-wrap {{ width: 100%; overflow: auto; background: #fff; border: 1px solid var(--rule); border-radius: 14px; }}
    .chart-wrap svg {{ display: block; width: 100%; min-width: 760px; height: auto; }}
    .candidate-reason {{ margin-top: 10px; color: var(--muted); font-size: 13px; }}
    .agent-section {{ }}
    .agent-summary {{ font-size: 16px; font-weight: 600; color: var(--ink); margin-bottom: 12px; }}
    .agent-stats {{ display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 8px; margin-bottom: 14px; }}
    .agent-stat {{ border: 1px solid var(--rule); background: var(--bg); border-radius: 10px; padding: 8px 10px; text-align: center; }}
    .agent-stat span {{ display: block; color: var(--muted); font-size: 11px; }}
    .agent-stat strong {{ display: block; font-size: 18px; margin-top: 2px; }}
    .agent-warnings {{ background: #fff3e0; border: 1px solid #ff9800; border-radius: 10px; padding: 10px 14px; margin-bottom: 10px; font-size: 13px; }}
    .agent-warnings ul {{ margin: 6px 0 0; padding-left: 20px; }}
    .agent-warnings li {{ margin-bottom: 4px; }}
    .agent-recs {{ background: #e8f5e9; border: 1px solid #4caf50; border-radius: 10px; padding: 10px 14px; margin-bottom: 10px; font-size: 13px; }}
    .agent-recs ul {{ margin: 6px 0 0; padding-left: 20px; }}
    .agent-recs li {{ margin-bottom: 4px; }}
    .agent-review-card {{ border: 1px solid var(--rule); border-radius: 10px; padding: 8px 12px; margin-bottom: 4px; background: #fff; font-size: 13px; cursor: pointer; transition: border-color .12s ease; }}
    .agent-review-card:hover {{ border-color: var(--accent); }}
    .agent-review-card .review-header {{ display: flex; align-items: center; gap: 8px; }}
    .agent-review-card .card-detail {{ display: none; margin-top: 6px; padding-top: 6px; border-top: 1px dashed var(--rule); }}
    .agent-review-card.expanded .card-detail {{ display: block; }}
    .agent-review-card .toggle-icon {{ margin-left: auto; color: var(--muted); font-size: 11px; }}
    .agent-review-card.expanded .toggle-icon::before {{ content: '收起 ▴'; }}
    .agent-review-card .toggle-icon::before {{ content: '展开 ▾'; }}
    .agent-review-list {{ max-height: 560px; overflow-y: auto; display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 6px; align-content: start; }}
    .agent-toggle-bar {{ display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }}
    .agent-toggle-bar button {{ padding: 4px 10px; font-size: 12px; border-radius: 6px; border: 1px solid var(--rule); background: var(--bg); cursor: pointer; }}
    .agent-review-card .review-brief {{ color: var(--muted); font-size: 12px; }}
    .risk-badge {{ display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 11px; color: white; }}
    .highlight-text {{ color: #178a54; font-size: 12px; }}
    .issue-text {{ color: #b9382f; font-size: 12px; }}
    .tab-nav {{ display: flex; gap: 2px; margin-bottom: 16px; border-bottom: 2px solid var(--rule); padding-left: 4px; }}
    .tab-btn {{ padding: 10px 18px; border: none; background: transparent; color: var(--muted); cursor: pointer; font: inherit; font-size: 15px; border-bottom: 3px solid transparent; margin-bottom: -2px; transition: color .12s ease, border-color .12s ease; }}
    .tab-btn:hover {{ color: var(--ink); }}
    .tab-btn.active {{ color: var(--accent); border-bottom-color: var(--accent); font-weight: 700; }}
    section[data-tab] {{ display: none; }}
    section[data-tab].active {{ display: block; }}
    @media (max-width: 980px) {{ .grid, .reports, .status {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} }}
    @media (max-width: 900px) {{ .candidate-layout {{ grid-template-columns: 1fr; }} }}
    @media (max-width: 620px) {{ main {{ width: calc(100% - 20px); }} .grid, .reports, .status, .chart-meta {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>A股交易系统操作面板</h1>
      <p>在这里运行常用流程，并打开生成的报告。运行过程中不要关闭这个命令行窗口。</p>
      <div class="status">
        <div class="stat"><span>当前状态</span><strong id="running">读取中</strong></div>
        <div class="stat"><span>当前任务</span><strong id="action">-</strong></div>
        <div class="stat"><span>开始时间</span><strong id="started">-</strong></div>
        <div class="stat"><span>退出码</span><strong id="exit">-</strong></div>
      </div>
    </header>

    <nav class="tab-nav">
      <button class="tab-btn active" data-tab="dashboard">仪表盘</button>
      <button class="tab-btn" data-tab="screening">选股执行</button>
      <button class="tab-btn" data-tab="holdings">持仓管理</button>
      <button class="tab-btn" data-tab="simulation">模拟交易</button>
      <button class="tab-btn" data-tab="reports">报告与历史</button>
      <button class="tab-btn" data-tab="log">运行日志</button>
    </nav>

    <section data-tab="dashboard" class="active">
      <h2>活跃市值</h2>
      <p>先填写当天指南针活跃市值，再生成报告或每日候选股票。当前值写入 data/compass_active_market_value.csv。多头判定规则：单日 >= 4% 或近3日累计 >= 4%。</p>
      <div class="status">
        <div class="stat"><span>当前日期</span><strong id="amvDate">读取中</strong></div>
        <div class="stat"><span>当前活跃市值</span><strong id="amvValue">读取中</strong></div>
        <div class="stat"><span>近3日累计</span><strong id="amvCumulative">读取中</strong></div>
        <div class="stat"><span>来源</span><strong id="amvSource">-</strong></div>
        <div class="stat"><span>备注</span><strong id="amvNote">-</strong></div>
        <div class="stat"><span>市场状态</span><strong id="amvRegime" style="color:var(--accent);">读取中</strong></div>
      </div>
      <div style="margin-top:14px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
        <label style="font-weight:600;">市场状态覆盖：</label>
        <select id="marketOverrideDash" style="padding:6px 12px;border-radius:8px;border:1px solid #ddd;font-size:14px;">
          <option value="">自动评估（默认）</option>
          <option value="bull">多头（80%仓位）</option>
          <option value="neutral_strong">震荡偏强（50%仓位）</option>
          <option value="neutral_balanced">震荡中性（35%仓位）</option>
          <option value="neutral_weak">震荡偏弱（20%仓位）</option>
          <option value="bear">空头（10%仓位）</option>
        </select>
        <span style="font-size:13px;color:var(--muted);">选择后覆盖系统判定，影响选股和仓位</span>
      </div>
      <div class="form-row">
        <label><span>新活跃市值</span><input id="amvInput" type="number" step="0.01" placeholder="例如 4.82"></label>
        <label><span>备注</span><input id="amvNoteInput" type="text" placeholder="可选，例如 收盘后手动录入"></label>
        <button class="primary-button" id="saveAmv" type="button"><strong>保存活跃市值</strong></button>
      </div>
    </section>

    <section data-tab="screening">
      <h2>运行流程</h2>
      <div style="margin-bottom:14px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
        <label style="font-weight:600;">市场状态覆盖：</label>
        <select id="marketOverride" style="padding:6px 12px;border-radius:8px;border:1px solid #ddd;font-size:14px;">
          <option value="">自动评估（默认）</option>
          <option value="bull">多头（80%仓位）</option>
          <option value="neutral_strong">震荡偏强（50%仓位）</option>
          <option value="neutral_balanced">震荡中性（35%仓位）</option>
          <option value="neutral_weak">震荡偏弱（20%仓位）</option>
          <option value="bear">空头（10%仓位）</option>
        </select>
        <span id="marketRegimeDisplay" style="padding:4px 12px;border-radius:8px;background:#f0f0f5;font-size:13px;font-weight:600;color:#355cff;">加载中...</span>
        <span class="hint" style="margin:0;">选择后覆盖系统自动判定，影响选股方向和仓位建议</span>
      </div>
      <div class="grid">
        <button data-action="refresh_spot"><strong>刷新实时行情</strong><span>拉取全市场行情并缓存到 data/a_spot_latest.csv</span></button>
        <button data-action="daily_report"><strong>生成每日收盘报告</strong><span>基于真实自选股池生成交易计划和 HTML 报告</span></button>
        <button data-action="backtest"><strong>运行规则回测</strong><span>回测 B1、S1、滴滴、砖形图最近表现</span></button>
        <button data-action="screen_candidates"><strong>生成每日候选股票</strong><span>排除 300/688 后筛选当天可盯盘候选</span></button>
        <button data-action="review_candidates"><strong>复盘昨日候选股票</strong><span>对比候选生成日与最新收盘，检查涨跌表现和破白/破黄风险</span></button>
        <button data-action="periodic_review_weekly"><strong>按周复盘汇总</strong><span>汇总最近7天选入记录，统计胜率和平均收益</span></button>
        <button data-action="periodic_review_monthly"><strong>按月复盘汇总</strong><span>汇总最近30天选入记录，统计胜率和平均收益</span></button>
        <button data-action="periodic_review_all"><strong>全部历史复盘</strong><span>汇总所有历史选入记录，全面评估策略表现</span></button>
      </div>
      <div style="margin-top:14px;">
        <button class="primary-button" id="runDailyWorkflow" type="button" style="background:#e65100;font-size:16px;padding:12px 24px;"><strong>⚡ 每日一键执行（刷新行情→复盘昨日→生成今日候选→模拟交易）</strong></button>
      </div>
      <div class="hint">执行顺序：刷新实时行情 → 复盘昨日候选股 → 生成今日候选股票 → 运行模拟交易。首次使用无历史备份时复盘会跳过，第二天起正常工作。</div>
    </section>

    <section data-tab="reports">
      <h2>打开报告</h2>
      <div class="reports">
        {links}
      </div>
    </section>

    <section data-tab="reports">
      <h2>历史记录 <button id="loadHistory" type="button" class="primary-button" style="font-size:13px;padding:6px 14px;">刷新历史列表</button></h2>
      <div id="historyList" class="hint">点击"刷新历史列表"查看所有历史候选股和复盘记录。</div>
    </section>

    <section data-tab="reports">
      <h2>通达信导入文本</h2>
      <p>点击按钮后展示股票代码文本，可直接复制到通达信自选股/自定义板块导入窗口。</p>
      <div class="tdx-actions">
        <button class="primary-button" id="showTdxTop10" type="button"><strong>展示前10只代码</strong></button>
        <button class="primary-button" id="showTdxAll" type="button"><strong>展示全部候选代码</strong></button>
        <button class="primary-button" id="copyTdxCodes" type="button"><strong>复制当前文本</strong></button>
      </div>
      <div class="tdx-text" id="tdxTextBox">
        <textarea id="tdxCodes" readonly placeholder="点击上方按钮后，这里会展示股票代码。"></textarea>
        <div class="tdx-meta" id="tdxMeta">等待展示。</div>
      </div>
    </section>

    <section data-tab="screening">
      <h2>候选股指标拆解</h2>
      <p>点击左侧某只候选股，查看最近60日K线、知行短期趋势线、知行多空线、MACD、砖型图、支撑位和压力位。</p>
      <div class="candidate-layout">
        <div>
          <button class="primary-button" id="loadCandidates" type="button"><strong>刷新候选股列表</strong></button>
          <div class="candidate-list" id="candidateList">点击上方按钮读取候选股。</div>
        </div>
        <div class="chart-card">
          <h3 id="candidateTitle">请选择候选股</h3>
          <div class="chart-meta">
            <div class="stat"><span>最新收盘</span><strong id="detailClose">-</strong></div>
            <div class="stat"><span>白线/黄线</span><strong id="detailLines">-</strong></div>
            <div class="stat"><span>MACD DIF/DEA</span><strong id="detailMacd">-</strong></div>
            <div class="stat"><span>砖型图</span><strong id="detailBrick">-</strong></div>
            <div class="stat"><span>成交量</span><strong id="detailVolume">-</strong></div>
            <div class="stat"><span>支撑/压力</span><strong id="detailRange">-</strong></div>
          </div>
          <div class="chart-wrap" id="candidateChart"><div class="hint">图表会显示在这里。</div></div>
          <div class="candidate-reason" id="candidateReason"></div>
        </div>
      </div>
    </section>

    <section class="agent-section" data-tab="screening">
      <h2>Agent 审核报告</h2>
      <div class="agent-summary" id="agentSummary">点击"刷新候选股列表"后自动加载审核结果。</div>
      <div class="agent-stats" id="agentStats"></div>
      <div id="agentWarnings"></div>
      <div id="agentRecs"></div>
      <div class="agent-toggle-bar" id="agentToggleBar" style="display:none;">
        <button id="expandAllReviews" type="button">展开全部</button>
        <button id="collapseAllReviews" type="button">收起全部</button>
        <span style="font-size:12px;color:var(--muted);">点击卡片可展开/收起详情</span>
      </div>
      <div class="agent-review-list" id="agentReviewList"></div>
    </section>

    <section data-tab="log">
      <h2>运行日志</h2>
      <pre id="log">等待操作...</pre>
    </section>

    <section data-tab="holdings">
      <h2>持股管理</h2>
      <div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;align-items:center;">
        <button id="btnCheckHoldings" onclick="checkHoldings()">检查持仓（卖出信号+盈亏）</button>
        <button id="btnAddHolding" onclick="showAddHoldingForm()">添加持仓</button>
        <button onclick="loadTransactions()">交易流水</button>
      </div>
      <div id="addHoldingForm" style="display:none;background:var(--bg);border:1px solid var(--rule);border-radius:8px;padding:12px;margin-bottom:12px;">
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px;">
          <input id="hfSymbol" placeholder="代码 如 601963" style="padding:6px;border:1px solid var(--rule);border-radius:6px;" />
          <input id="hfName" placeholder="名称" style="padding:6px;border:1px solid var(--rule);border-radius:6px;" />
          <input id="hfDate" type="date" style="padding:6px;border:1px solid var(--rule);border-radius:6px;" />
          <input id="hfPrice" type="number" step="0.01" placeholder="买入价" style="padding:6px;border:1px solid var(--rule);border-radius:6px;" />
          <input id="hfShares" type="number" placeholder="股数" style="padding:6px;border:1px solid var(--rule);border-radius:6px;" />
          <input id="hfCommission" type="number" step="0.01" placeholder="手续费" style="padding:6px;border:1px solid var(--rule);border-radius:6px;" />
        </div>
        <div style="margin-top:8px;display:flex;gap:8px;">
          <button onclick="submitAddHolding()">确认添加</button>
          <button onclick="document.getElementById('addHoldingForm').style.display='none'">取消</button>
        </div>
      </div>
      <div id="holdingsTable" style="overflow-x:auto;"></div>
      <div id="sellModal" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.4);z-index:999;align-items:center;justify-content:center;">
        <div style="background:var(--bg2);border-radius:12px;padding:20px;max-width:420px;width:90%;">
          <h3>确认卖出</h3>
          <div id="sellInfo" style="margin:8px 0;font-size:14px;color:var(--muted);"></div>
          <div style="display:grid;gap:8px;">
            <input id="sellDate" type="date" style="padding:6px;border:1px solid var(--rule);border-radius:6px;" />
            <input id="sellPrice" type="number" step="0.01" placeholder="卖出价" style="padding:6px;border:1px solid var(--rule);border-radius:6px;" />
            <input id="sellShares" type="number" placeholder="卖出股数" style="padding:6px;border:1px solid var(--rule);border-radius:6px;" />
            <input id="sellCommission" type="number" step="0.01" placeholder="手续费" value="0" style="padding:6px;border:1px solid var(--rule);border-radius:6px;" />
          </div>
          <div style="margin-top:12px;display:flex;gap:8px;justify-content:flex-end;">
            <button onclick="confirmSell()">确认卖出</button>
            <button onclick="document.getElementById('sellModal').style.display='none'">取消</button>
          </div>
        </div>
      </div>
    </section>

    <section data-tab="simulation">
      <h2>模拟交易</h2>
      <p>自动跟踪每日选股结果，全量买入验证选股系统质量。每只票固定仓位，按风控规则自动卖出。</p>
      <div class="status">
        <div class="stat"><span>账户名称</span><strong id="simAccountName">-</strong></div>
        <div class="stat"><span>初始资金</span><strong id="simInitialCapital">-</strong></div>
        <div class="stat"><span>当前净值</span><strong id="simCurrentNav">-</strong></div>
        <div class="stat"><span>累计收益</span><strong id="simTotalReturn">-</strong></div>
        <div class="stat"><span>持仓数量</span><strong id="simHoldingCount">-</strong></div>
        <div class="stat"><span>胜率</span><strong id="simWinRate">-</strong></div>
      </div>
      <div style="margin-top:14px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
        <button class="primary-button" id="runSimulationBtn" type="button" onclick="runSimulation()"><strong>运行今日模拟交易</strong></button>
        <button class="primary-button" id="refreshSimBtn" type="button" onclick="loadSimulationData()"><strong>刷新数据</strong></button>
        <span class="hint" id="simStatusHint">点击"刷新数据"查看模拟账户状态</span>
      </div>
      <div class="grid2" style="margin-top:14px;display:grid;grid-template-columns:1fr 1fr;gap:14px;">
        <div>
          <h3>当前持仓</h3>
          <div id="simHoldingsTable" class="chart-card" style="min-height:300px;overflow-x:auto;">
            <div class="hint">暂无数据</div>
          </div>
        </div>
        <div>
          <h3>净值曲线</h3>
          <div id="simNavChart" class="chart-card" style="min-height:300px;">
            <div class="hint">暂无数据</div>
          </div>
        </div>
      </div>
      <div style="margin-top:14px;">
        <h3>交易记录</h3>
        <div id="simTransactionsTable" class="chart-card" style="min-height:200px;overflow-x:auto;">
          <div class="hint">暂无数据</div>
        </div>
      </div>
    </section>
  </main>

  <script>
    // ─── Tab 切换 ───
    document.querySelectorAll('.tab-btn').forEach(btn => {{
      btn.addEventListener('click', () => {{
        const tab = btn.dataset.tab;
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
        document.querySelectorAll('section[data-tab]').forEach(s => s.classList.toggle('active', s.dataset.tab === tab));
      }});
    }});
    // ─── 市场状态覆盖下拉同步（仪表盘 ↔ 选股执行） ───
    const moMain = document.getElementById('marketOverride');
    const moDash = document.getElementById('marketOverrideDash');
    if (moMain && moDash) {{
      moMain.addEventListener('change', () => {{ moDash.value = moMain.value; }});
      moDash.addEventListener('change', () => {{ moMain.value = moDash.value; }});
    }}
    const buttons = Array.from(document.querySelectorAll('button[data-action]'));
    const saveAmvButton = document.getElementById('saveAmv');
    const showTdxTop10Button = document.getElementById('showTdxTop10');
    const showTdxAllButton = document.getElementById('showTdxAll');
    const copyTdxCodesButton = document.getElementById('copyTdxCodes');
    const loadCandidatesButton = document.getElementById('loadCandidates');
    async function runAction(action) {{
      const override = document.getElementById('marketOverride').value;
      const res = await fetch('/run', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ action, market_override: override || null }})
      }});
      const data = await res.json();
      if (!data.ok) alert(data.error || '启动失败');
      await refreshStatus();
    }}
    async function refreshActiveMarketValue() {{
      const res = await fetch('/active-market-value');
      const data = await res.json();
      document.getElementById('amvDate').textContent = data.date || '-';
      document.getElementById('amvValue').textContent = data.value ? data.value + '%' : '-';
      document.getElementById('amvSource').textContent = data.source || '-';
      document.getElementById('amvNote').textContent = data.note || '-';
      const cumEl = document.getElementById('amvCumulative');
      if (data.cumulative_3d && data.recent && data.recent.length > 0) {{
        const cum = parseFloat(data.cumulative_3d);
        const flag = cum >= 4.0 ? ' (达多头阈值)' : '';
        cumEl.textContent = cum.toFixed(2) + '%' + flag;
        cumEl.style.color = cum >= 4.0 ? '#15803d' : '';
      }} else {{
        cumEl.textContent = '仅1天数据';
      }}
      if (data.value) document.getElementById('amvInput').value = data.value;
      const regimeEl = document.getElementById('marketRegimeDisplay');
      const dashRegimeEl = document.getElementById('amvRegime');
      if (data.market_regime) {{
        const regimeText = data.market_regime + '（' + data.max_position_pct + '%仓位）';
        const regimeColors = {{
          '多头': '#15803d', '震荡偏强': '#355cff', '震荡中性': '#6b7280',
          '震荡偏弱': '#b45309', '空头': '#b91c1c'
        }};
        const regimeColor = regimeColors[data.market_regime] || '#355cff';
        regimeEl.textContent = '系统评估：' + regimeText;
        regimeEl.style.color = regimeColor;
        regimeEl.title = data.market_reason || '';
        if (dashRegimeEl) {{
          dashRegimeEl.textContent = regimeText;
          dashRegimeEl.style.color = regimeColor;
          dashRegimeEl.title = data.market_reason || '';
        }}
      }} else {{
        regimeEl.textContent = '系统评估：不可用';
        regimeEl.style.color = '#6b7280';
        if (dashRegimeEl) {{ dashRegimeEl.textContent = '不可用'; dashRegimeEl.style.color = '#6b7280'; }}
      }}
    }}
    async function saveActiveMarketValue() {{
      const value = document.getElementById('amvInput').value;
      const note = document.getElementById('amvNoteInput').value;
      const res = await fetch('/active-market-value', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ value, note }})
      }});
      const data = await res.json();
      if (!data.ok) {{
        alert(data.error || '保存失败');
        return;
      }}
      await refreshActiveMarketValue();
      await refreshStatus();
    }}
    async function showTdxCodes(limit) {{
      const url = limit ? `/tdx-codes?limit=${{limit}}` : '/tdx-codes';
      const res = await fetch(url);
      const data = await res.json();
      if (!data.ok) {{
        alert(data.error || '读取代码失败');
        return;
      }}
      document.getElementById('tdxTextBox').style.display = 'block';
      document.getElementById('tdxCodes').value = data.text || '';
      document.getElementById('tdxMeta').textContent = `已展示 ${{data.count}} 只股票代码，可复制后导入通达信。`;
    }}
    async function copyTdxCodes() {{
      const textarea = document.getElementById('tdxCodes');
      if (!textarea.value.trim()) {{
        alert('请先点击“展示前10只代码”或“展示全部候选代码”。');
        return;
      }}
      try {{
        await navigator.clipboard.writeText(textarea.value);
        document.getElementById('tdxMeta').textContent = '已复制到剪贴板，可到通达信里粘贴导入。';
      }} catch (err) {{
        textarea.focus();
        textarea.select();
        document.getElementById('tdxMeta').textContent = '浏览器未允许自动复制，请手动 Ctrl+C 复制。';
      }}
    }}
    function renderAgentReview(data) {{
      const summaryEl = document.getElementById('agentSummary');
      const statsEl = document.getElementById('agentStats');
      const warningsEl = document.getElementById('agentWarnings');
      const recsEl = document.getElementById('agentRecs');
      const listEl = document.getElementById('agentReviewList');
      // 摘要
      summaryEl.textContent = data.agent_summary || '暂无审核数据。';
      // 统计
      const stats = data.agent_stats || {{}};
      const opinionCounts = stats.opinion_counts || {{}};
      const riskDist = stats.risk_level_distribution || {{}};
      statsEl.innerHTML = '';
      const statItems = [
        {{label: '总数', value: stats.total || 0, color: '#355cff'}},
        {{label: '建议关注', value: opinionCounts['建议关注'] || 0, color: '#4caf50'}},
        {{label: '谨慎', value: opinionCounts['谨慎'] || 0, color: '#ff9800'}},
        {{label: '排除', value: opinionCounts['排除'] || 0, color: '#f44336'}},
        {{label: '平均R/R', value: stats.avg_risk_reward || 0, color: '#355cff'}},
        {{label: '高风险数', value: stats.high_risk_count || 0, color: '#b71c1c'}},
      ];
      statItems.forEach(s => {{
        const div = document.createElement('div');
        div.className = 'agent-stat';
        div.innerHTML = '<span>' + s.label + '</span><strong style="color:' + s.color + ';">' + s.value + '</strong>';
        statsEl.appendChild(div);
      }});
      // 风险提示
      const warnings = data.agent_warnings || [];
      if (warnings.length > 0) {{
        warningsEl.innerHTML = '<div class="agent-warnings"><strong>⚠ 全局风险提示</strong><ul>' +
          warnings.map(w => '<li>' + w + '</li>').join('') + '</ul></div>';
      }} else {{
        warningsEl.innerHTML = '';
      }}
      // 操作建议
      const recs = data.agent_recommendations || [];
      if (recs.length > 0) {{
        recsEl.innerHTML = '<div class="agent-recs"><strong>→ 操作建议</strong><ul>' +
          recs.map(r => '<li>' + r + '</li>').join('') + '</ul></div>';
      }} else {{
        recsEl.innerHTML = '';
      }}
      // 逐只审核列表
      const items = data.items || [];
      const riskColors = {{0: '#4caf50', 1: '#8bc34a', 2: '#ff9800', 3: '#ff5722', 4: '#f44336', 5: '#b71c1c'}};
      const opinionColors = {{'建议关注': '#4caf50', '谨慎': '#ff9800', '排除': '#f44336'}};
      const reviewItems = items.filter(it => it.opinion || it.issues.length > 0 || it.highlights.length > 0);
      const reviewCards = reviewItems.map(it => {{
        const opinionTag = it.opinion ? '<span class="risk-badge" style="background:' + (opinionColors[it.opinion] || '#999') + ';">' + it.opinion + '</span>' : '';
        const riskTag = it.risk_level > 0 ? '<span class="risk-badge" style="background:' + (riskColors[it.risk_level] || '#999') + ';">风险' + it.risk_level + '</span>' : '';
        const rrTag = it.risk_reward > 0 ? '<span style="font-size:11px;color:#667085;">R/R=' + it.risk_reward + '</span>' : '';
        const detailParts = [];
        if (it.brief) detailParts.push('<div class="review-brief">' + it.brief + '</div>');
        if (it.highlights.length > 0) detailParts.push('<div>' + it.highlights.map(h => '<span class="highlight-text">★ ' + h + '</span>').join('<br>') + '</div>');
        if (it.issues.length > 0) detailParts.push('<div>' + it.issues.map(i => '<span class="issue-text">- ' + i + '</span>').join('<br>') + '</div>');
        if (it.position_suggestion) detailParts.push('<div style="margin-top:4px;color:#355cff;font-size:12px;">仓位：' + it.position_suggestion + '</div>');
        const detail = detailParts.length > 0 ? '<div class="card-detail">' + detailParts.join('') + '</div>' : '';
        return '<div class="agent-review-card"><div class="review-header"><strong>' + it.symbol + ' ' + it.name + '</strong>' + opinionTag + riskTag + rrTag + '<span class="toggle-icon"></span></div>' + detail + '</div>';
      }}).join('');
      listEl.innerHTML = reviewCards || '<div style="color:#667085;font-size:13px;">暂无逐只审核数据。</div>';
      // 显示展开/收起控制栏
      const toggleBar = document.getElementById('agentToggleBar');
      toggleBar.style.display = reviewItems.length > 0 ? 'flex' : 'none';
      // 卡片点击展开/收起
      listEl.querySelectorAll('.agent-review-card').forEach(card => {{
        card.addEventListener('click', () => card.classList.toggle('expanded'));
      }});
      // 展开/收起全部
      document.getElementById('expandAllReviews').onclick = () => {{
        listEl.querySelectorAll('.agent-review-card').forEach(c => c.classList.add('expanded'));
      }};
      document.getElementById('collapseAllReviews').onclick = () => {{
        listEl.querySelectorAll('.agent-review-card').forEach(c => c.classList.remove('expanded'));
      }};
    }}
    async function loadCandidateList() {{
      const list = document.getElementById('candidateList');
      list.textContent = '读取中...';
      const res = await fetch('/candidate-list');
      const data = await res.json();
      if (!data.ok) {{
        list.textContent = data.error || '读取失败';
        return;
      }}
      if (!data.items || data.items.length === 0) {{
        list.textContent = '暂无候选股，请先生成每日候选股票。';
        return;
      }}
      // 渲染 Agent 审核报告专区
      renderAgentReview(data);
      list.innerHTML = '';
      // Agent 审核警告
      if (data.agent_warnings && data.agent_warnings.length > 0) {{
        const warnDiv = document.createElement('div');
        warnDiv.style.cssText = 'background:#fff3e0;border:1px solid #ff9800;border-radius:6px;padding:8px 12px;margin-bottom:8px;font-size:13px;';
        warnDiv.innerHTML = '<strong>⚠ Agent 风险提示：</strong><br>' + data.agent_warnings.map(w => '· ' + w).join('<br>');
        list.appendChild(warnDiv);
      }}
      const opinionColors = {{'建议关注': '#4caf50', '谨慎': '#ff9800', '排除': '#f44336'}};
      const riskColors = {{0: '#4caf50', 1: '#8bc34a', 2: '#ff9800', 3: '#ff5722', 4: '#f44336', 5: '#b71c1c'}};
      const strengthColors = {{'强势': '#4caf50', '中性': '#999', '弱势': '#f44336'}};
      data.items.forEach((item, idx) => {{
        const btn = document.createElement('button');
        btn.className = 'candidate-item';
        const opinionTag = item.opinion ? '<span style="float:right;font-size:11px;padding:2px 6px;border-radius:3px;color:white;background:' + (opinionColors[item.opinion] || '#999') + ';">' + item.opinion + '</span>' : '';
        const riskTag = item.risk_level !== undefined && item.risk_level > 0 ? '<span class="risk-badge" style="background:' + (riskColors[item.risk_level] || '#999') + ';">风险' + item.risk_level + '</span>' : '';
        const gradeTag = item.chart_grade ? '<span style="font-size:11px;color:#666;">图形:' + item.chart_grade + '(' + item.chart_quality + '分)</span>' : '';
        const sectorTag = item.sector && item.sector !== '未知' ? '<span style="font-size:11px;">板块:<span style="color:' + (strengthColors[item.sector_strength] || '#999') + ';">' + item.sector + (item.sector_change ? '(' + (item.sector_change > 0 ? '+' : '') + item.sector_change.toFixed(2) + '%)' : '') + '</span></span>' : '';
        const highlightTag = item.highlights && item.highlights.length > 0 ? '<span class="highlight-text">★ ' + item.highlights[0] + '</span>' : '';
        btn.innerHTML = '<strong>' + (idx + 1) + '. ' + item.symbol + ' ' + item.name + '</strong>' + opinionTag + riskTag
          + '<span>涨幅 ' + item.pct_change.toFixed(2) + '%｜分数 ' + item.score.toFixed(2) + (item.risk_reward > 0 ? '｜R/R=' + item.risk_reward : '') + '</span>'
          + '<span>' + (item.tags || '无标签') + '</span>'
          + (gradeTag || sectorTag ? '<span>' + gradeTag + (gradeTag && sectorTag ? '｜' : '') + sectorTag + '</span>' : '')
          + (highlightTag ? highlightTag : '');
        btn.addEventListener('click', () => loadCandidateDetail(item.symbol, btn));
        list.appendChild(btn);
      }});
    }}
    async function loadCandidateDetail(symbol, button) {{
      document.querySelectorAll('.candidate-item').forEach(x => x.classList.remove('active'));
      if (button) button.classList.add('active');
      document.getElementById('candidateTitle').textContent = symbol + ' 指标加载中...';
      document.getElementById('candidateChart').innerHTML = '<div class="hint">正在拉取最近日线并计算指标...</div>';
      const res = await fetch('/candidate-detail?symbol=' + encodeURIComponent(symbol));
      const data = await res.json();
      if (!data.ok) {{
        document.getElementById('candidateTitle').textContent = symbol + ' 加载失败';
        document.getElementById('candidateChart').innerHTML = '<div class="hint">' + (data.error || '加载失败') + '</div>';
        return;
      }}
      document.getElementById('candidateTitle').textContent = data.symbol + ' ' + (data.name || '') + ' 最近60日指标';
      document.getElementById('detailClose').textContent = data.latest.close + '（' + data.latest.date + '）';
      document.getElementById('detailLines').textContent = data.latest.white + ' / ' + data.latest.yellow;
      document.getElementById('detailMacd').textContent = data.latest.dif + ' / ' + data.latest.dea;
      document.getElementById('detailBrick').textContent = data.latest.brick;
      const vol = data.latest.volume || 0;
      document.getElementById('detailVolume').textContent = vol >= 10000 ? (vol / 10000).toFixed(1) + '万手' : vol + '手';
      document.getElementById('detailRange').textContent = data.support + ' / ' + data.resistance;
      document.getElementById('candidateReason').textContent = '标签：' + (data.tags || '无') + '。原因：' + (data.candidate_reason || '无');
      document.getElementById('candidateChart').innerHTML = drawCandidateChart(data);
      // Agent 审核详情
      const riskColors = {{0: '#4caf50', 1: '#8bc34a', 2: '#ff9800', 3: '#ff5722', 4: '#f44336', 5: '#b71c1c'}};
      const opinionColors = {{'建议关注': '#4caf50', '谨慎': '#ff9800', '排除': '#f44336'}};
      let reviewHtml = '';
      if (data.opinion || data.brief) {{
        const opinionTag = data.opinion ? '<span class="risk-badge" style="background:' + (opinionColors[data.opinion] || '#999') + ';">' + data.opinion + '</span>' : '';
        const riskTag = data.risk_level > 0 ? '<span class="risk-badge" style="background:' + (riskColors[data.risk_level] || '#999') + ';">风险' + data.risk_level + '</span>' : '';
        const rrTag = data.risk_reward > 0 ? '<span style="font-size:12px;color:#667085;">R/R=' + data.risk_reward + '</span>' : '';
        reviewHtml += '<div style="margin-top:10px;padding:10px 12px;border:1px solid #d9deea;border-radius:10px;background:#fff;font-size:13px;">';
        reviewHtml += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;"><strong>Agent 审核</strong>' + opinionTag + riskTag + rrTag + '</div>';
        if (data.brief) reviewHtml += '<div class="review-brief">' + data.brief + '</div>';
        if (data.highlights && data.highlights.length > 0) {{
          reviewHtml += '<div style="margin-top:6px;">' + data.highlights.map(h => '<span class="highlight-text">★ ' + h + '</span>').join('<br>') + '</div>';
        }}
        if (data.issues && data.issues.length > 0) {{
          reviewHtml += '<div style="margin-top:6px;">' + data.issues.map(i => '<span class="issue-text">- ' + i + '</span>').join('<br>') + '</div>';
        }}
        if (data.position_suggestion) {{
          reviewHtml += '<div style="margin-top:6px;color:#355cff;">仓位建议：' + data.position_suggestion + '</div>';
        }}
        reviewHtml += '</div>';
      }}
      const reasonEl = document.getElementById('candidateReason');
      reasonEl.innerHTML = '标签：' + (data.tags || '无') + '。原因：' + (data.candidate_reason || '无') + reviewHtml;
    }}
    function drawCandidateChart(data) {{
      const records = data.records || [];
      if (!records.length) return '<div class="hint">无可绘制数据。</div>';
      const w = 980, priceH = 340, volH = 80, macdH = 135, brickH = 180, left = 54, right = 24, top = 24, gap = 34;
      const totalH = top + priceH + gap + volH + gap + macdH + gap + brickH + 34;
      const innerW = w - left - right;
      const step = innerW / Math.max(records.length, 1);
      const priceValues = [];
      records.forEach(r => priceValues.push(r.high, r.low, r.white, r.yellow));
      priceValues.push(data.support, data.resistance);
      let pMin = Math.min.apply(null, priceValues);
      let pMax = Math.max.apply(null, priceValues);
      const pad = Math.max((pMax - pMin) * 0.08, 0.01);
      pMin -= pad; pMax += pad;
      const macdVals = [];
      records.forEach(r => macdVals.push(r.hist, r.dif, r.dea));
      const maxAbs = Math.max(0.01, ...macdVals.map(v => Math.abs(v)));
      const volMax = Math.max(1, ...records.map(r => r.volume || 0));
      const brickMax = Math.max(1, ...records.map(r => r.brick || 0));
      function x(i) {{ return left + i * step + step / 2; }}
      function priceY(v) {{ return top + (pMax - v) / (pMax - pMin) * priceH; }}
      const volTop = top + priceH + gap;
      function volY(v) {{ return volTop + volH - (v / volMax) * (volH - 10); }}
      const macdTop = volTop + volH + gap;
      function macdY(v) {{ return macdTop + macdH / 2 - v / maxAbs * (macdH / 2 - 12); }}
      const brickTop = macdTop + macdH + gap;
      function brickY(v) {{ return brickTop + brickH - (v / brickMax) * (brickH - 14); }}
      function linePath(key, scaleFn) {{
        return records.map((r, i) => (i === 0 ? 'M' : 'L') + x(i).toFixed(1) + ' ' + scaleFn(r[key]).toFixed(1)).join(' ');
      }}
      const candles = records.map((r, i) => {{
        const cx = x(i);
        const up = r.close >= r.open;
        const color = up ? '#d92d20' : '#039855';
        const yOpen = priceY(r.open), yClose = priceY(r.close);
        const bodyY = Math.min(yOpen, yClose);
        const bodyH = Math.max(2, Math.abs(yOpen - yClose));
        const bw = Math.max(4, Math.min(10, step * 0.58));
        return '<line x1="' + cx + '" y1="' + priceY(r.high) + '" x2="' + cx + '" y2="' + priceY(r.low) + '" stroke="' + color + '" stroke-width="1"/>'
          + '<rect x="' + (cx - bw / 2) + '" y="' + bodyY + '" width="' + bw + '" height="' + bodyH + '" fill="' + color + '" opacity="0.88"/>';
      }}).join('');
      const histBars = records.map((r, i) => {{
        const cx = x(i), zero = macdY(0), y = macdY(r.hist);
        const color = r.hist >= 0 ? '#d92d20' : '#039855';
        const bw = Math.max(3, Math.min(9, step * 0.5));
        return '<rect x="' + (cx - bw / 2) + '" y="' + Math.min(y, zero) + '" width="' + bw + '" height="' + Math.max(1, Math.abs(zero - y)) + '" fill="' + color + '" opacity="0.65"/>';
      }}).join('');
      const volBars = records.map((r, i) => {{
        const cx = x(i), vol = r.volume || 0;
        if (vol <= 0) return '';
        const up = r.close >= r.open;
        const color = up ? '#d92d20' : '#039855';
        const bw = Math.max(3, Math.min(10, step * 0.56));
        const yTop = volY(vol);
        return '<rect x="' + (cx - bw / 2) + '" y="' + yTop + '" width="' + bw + '" height="' + (volTop + volH - yTop) + '" fill="' + color + '" opacity="0.5"/>';
      }}).join('');
      const brickGrid = [0.25, 0.5, 0.75].map(pos => {{
        const gy = brickTop + brickH * pos;
        return '<line x1="' + left + '" y1="' + gy + '" x2="' + (w - right) + '" y2="' + gy + '" stroke="#e4e7ec" stroke-width="1"/>';
      }}).join('');
      const brickBlocks = records.map((r, i) => {{
        const cx = x(i);
        const current = r.brick || 0;
        const prev = i > 0 ? (records[i - 1].brick || 0) : (r.brick || 0);
        const yCurrent = brickY(current);
        const yPrev = brickY(prev);
        const color = current >= prev ? '#d92d20' : '#039855';
        const bw = Math.max(6, Math.min(12, step * 0.72));
        const y0 = Math.min(yCurrent, yPrev);
        const h = Math.max(2, Math.abs(yCurrent - yPrev));
        return '<rect x="' + (cx - bw / 2) + '" y="' + y0 + '" width="' + bw + '" height="' + h + '" fill="' + color + '" opacity="0.9" stroke="#ffffff" stroke-width="0.4"/>';
      }}).join('');
      const dateTicks = records.filter((_, i) => i % 10 === 0 || i === records.length - 1).map((r, i, arr) => {{
        const idx = i === arr.length - 1 ? records.length - 1 : records.indexOf(r);
        return '<text x="' + x(idx) + '" y="' + (totalH - 10) + '" text-anchor="middle" font-size="11" fill="#667085">' + r.date + '</text>';
      }}).join('');
      const supportY = priceY(data.support), resistanceY = priceY(data.resistance);
      return '<svg viewBox="0 0 ' + w + ' ' + totalH + '" role="img" aria-label="候选股指标图">'
        + '<rect width="' + w + '" height="' + totalH + '" fill="#fff"/>'
        + '<line x1="' + left + '" y1="' + top + '" x2="' + left + '" y2="' + (top + priceH) + '" stroke="#d0d5dd"/>'
        + '<line x1="' + left + '" y1="' + (top + priceH) + '" x2="' + (w - right) + '" y2="' + (top + priceH) + '" stroke="#d0d5dd"/>'
        + '<text x="' + left + '" y="16" font-size="12" fill="#667085">K线 + 白线/黄线 + 支撑压力</text>'
        + candles
        + '<path d="' + linePath('white', priceY) + '" fill="none" stroke="#344054" stroke-width="2"/>'
        + '<path d="' + linePath('yellow', priceY) + '" fill="none" stroke="#f79009" stroke-width="2"/>'
        + '<line x1="' + left + '" y1="' + supportY + '" x2="' + (w - right) + '" y2="' + supportY + '" stroke="#12b76a" stroke-width="1.5" stroke-dasharray="6 5"/>'
        + '<line x1="' + left + '" y1="' + resistanceY + '" x2="' + (w - right) + '" y2="' + resistanceY + '" stroke="#f04438" stroke-width="1.5" stroke-dasharray="6 5"/>'
        + '<text x="' + (w - right - 74) + '" y="' + (supportY - 5) + '" font-size="12" fill="#039855">支撑 ' + data.support + '</text>'
        + '<text x="' + (w - right - 74) + '" y="' + (resistanceY - 5) + '" font-size="12" fill="#d92d20">压力 ' + data.resistance + '</text>'
        + '<text x="' + (left + 10) + '" y="' + (top + 18) + '" font-size="12" fill="#344054">白线</text>'
        + '<text x="' + (left + 56) + '" y="' + (top + 18) + '" font-size="12" fill="#f79009">黄线</text>'
        + '<rect x="' + left + '" y="' + volTop + '" width="' + innerW + '" height="' + volH + '" fill="#f8fafc"/>'
        + '<line x1="' + left + '" y1="' + volTop + '" x2="' + (w - right) + '" y2="' + volTop + '" stroke="#d0d5dd"/>'
        + '<line x1="' + left + '" y1="' + (volTop + volH) + '" x2="' + (w - right) + '" y2="' + (volTop + volH) + '" stroke="#d0d5dd"/>'
        + '<text x="' + left + '" y="' + (volTop + 14) + '" font-size="12" fill="#667085">成交量</text>'
        + volBars
        + '<line x1="' + left + '" y1="' + macdY(0) + '" x2="' + (w - right) + '" y2="' + macdY(0) + '" stroke="#d0d5dd"/>'
        + '<text x="' + left + '" y="' + (macdTop - 8) + '" font-size="12" fill="#667085">MACD：柱体 + DIF/DEA</text>'
        + histBars
        + '<path d="' + linePath('dif', macdY) + '" fill="none" stroke="#175cd3" stroke-width="1.8"/>'
        + '<path d="' + linePath('dea', macdY) + '" fill="none" stroke="#7a5af8" stroke-width="1.8"/>'
        + '<rect x="' + left + '" y="' + brickTop + '" width="' + innerW + '" height="' + brickH + '" fill="#f8fafc"/>'
        + brickGrid
        + '<line x1="' + left + '" y1="' + (brickTop + brickH) + '" x2="' + (w - right) + '" y2="' + (brickTop + brickH) + '" stroke="#d0d5dd"/>'
        + '<path d="' + linePath('brick', brickY) + '" fill="none" stroke="#98a2b3" stroke-width="1" opacity="0.65"/>'
        + '<text x="' + left + '" y="' + (brickTop - 8) + '" font-size="12" fill="#667085">砖型图：按公式 STICKLINE 从昨日值画到今日值</text>'
        + brickBlocks
        + dateTicks
        + '</svg>';
    }}
    async function refreshStatus() {{
      const res = await fetch('/status');
      const data = await res.json();
      document.getElementById('running').textContent = data.running ? '运行中' : '空闲';
      document.getElementById('action').textContent = data.action || '-';
      document.getElementById('started').textContent = data.started_at || '-';
      document.getElementById('exit').textContent = data.exit_code === null ? '-' : data.exit_code;
      document.getElementById('log').textContent = (data.log || []).join('\\n') || '等待操作...';
      const logEl = document.getElementById('log');
      logEl.scrollTop = logEl.scrollHeight;
      buttons.forEach(btn => btn.disabled = data.running);
      saveAmvButton.disabled = data.running;
      runDailyWorkflowButton.disabled = data.running;
      if (data.running) {{
        runDailyWorkflowButton.textContent = '⏳ ' + (data.action || '执行中...');
      }} else {{
        runDailyWorkflowButton.textContent = '⚡ 每日一键执行（刷新行情→复盘昨日→生成今日候选→模拟交易）';
      }}
    }}
    buttons.forEach(btn => btn.addEventListener('click', () => runAction(btn.dataset.action)));
    saveAmvButton.addEventListener('click', saveActiveMarketValue);
    showTdxTop10Button.addEventListener('click', () => showTdxCodes(10));
    showTdxAllButton.addEventListener('click', () => showTdxCodes(null));
    copyTdxCodesButton.addEventListener('click', copyTdxCodes);
    loadCandidatesButton.addEventListener('click', loadCandidateList);
    // 历史记录加载
    const loadHistoryButton = document.getElementById('loadHistory');
    loadHistoryButton.addEventListener('click', async () => {{
      const el = document.getElementById('historyList');
      el.textContent = '加载中...';
      try {{
        const res = await fetch('/history-list');
        const data = await res.json();
        if (!data.ok || data.total === 0) {{
          el.innerHTML = '<div class="hint">暂无历史记录。每次生成候选股或复盘后会自动保存到这里。</div>';
          return;
        }}
        let html = '';
        if (data.candidates.length > 0) {{
          html += '<h3 style="font-size:14px;margin:8px 0 4px;">候选股记录（' + data.candidates.length + '）</h3>';
          html += '<table style="width:100%;font-size:13px;border-collapse:collapse;"><thead><tr><th style="text-align:left;padding:4px 8px;border-bottom:1px solid #d9deea;">文件名</th><th style="text-align:right;padding:4px 8px;border-bottom:1px solid #d9deea;">大小</th><th style="text-align:right;padding:4px 8px;border-bottom:1px solid #d9deea;">时间</th></tr></thead><tbody>';
          data.candidates.forEach(f => {{
            html += '<tr><td style="padding:4px 8px;"><a href="' + f.url + '" target="_blank">' + f.name + '</a></td><td style="text-align:right;padding:4px 8px;color:#667085;">' + f.size_kb + 'KB</td><td style="text-align:right;padding:4px 8px;color:#667085;">' + f.mtime + '</td></tr>';
          }});
          html += '</tbody></table>';
        }}
        if (data.reviews.length > 0) {{
          html += '<h3 style="font-size:14px;margin:12px 0 4px;">复盘记录（' + data.reviews.length + '）</h3>';
          html += '<table style="width:100%;font-size:13px;border-collapse:collapse;"><thead><tr><th style="text-align:left;padding:4px 8px;border-bottom:1px solid #d9deea;">文件名</th><th style="text-align:right;padding:4px 8px;border-bottom:1px solid #d9deea;">大小</th><th style="text-align:right;padding:4px 8px;border-bottom:1px solid #d9deea;">时间</th></tr></thead><tbody>';
          data.reviews.forEach(f => {{
            html += '<tr><td style="padding:4px 8px;"><a href="' + f.url + '" target="_blank">' + f.name + '</a></td><td style="text-align:right;padding:4px 8px;color:#667085;">' + f.size_kb + 'KB</td><td style="text-align:right;padding:4px 8px;color:#667085;">' + f.mtime + '</td></tr>';
          }});
          html += '</tbody></table>';
        }}
        if (data.periodic.length > 0) {{
          html += '<h3 style="font-size:14px;margin:12px 0 4px;">周期汇总报告（' + data.periodic.length + '）</h3>';
          html += '<table style="width:100%;font-size:13px;border-collapse:collapse;"><thead><tr><th style="text-align:left;padding:4px 8px;border-bottom:1px solid #d9deea;">文件名</th><th style="text-align:right;padding:4px 8px;border-bottom:1px solid #d9deea;">大小</th><th style="text-align:right;padding:4px 8px;border-bottom:1px solid #d9deea;">时间</th></tr></thead><tbody>';
          data.periodic.forEach(f => {{
            html += '<tr><td style="padding:4px 8px;"><a href="' + f.url + '" target="_blank">' + f.name + '</a></td><td style="text-align:right;padding:4px 8px;color:#667085;">' + f.size_kb + 'KB</td><td style="text-align:right;padding:4px 8px;color:#667085;">' + f.mtime + '</td></tr>';
          }});
          html += '</tbody></table>';
        }}
        el.innerHTML = html;
      }} catch(e) {{
        el.textContent = '加载失败：' + e.message;
      }}
    }});
    function appendLog(msg) {{
      const logEl = document.getElementById('log');
      logEl.textContent += '\\n' + msg;
    }}
    const runDailyWorkflowButton = document.getElementById('runDailyWorkflow');
    runDailyWorkflowButton.addEventListener('click', async () => {{
        if (!confirm('将依次执行：刷新行情缓存→复盘昨日候选股→生成今日候选股票。约需5-8分钟，确认开始？')) return;
        runDailyWorkflowButton.disabled = true;
        runDailyWorkflowButton.textContent = '⏳ 启动中...';
        try {{
            const override = document.getElementById('marketOverride').value;
            const res = await fetch('/run', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{action: 'daily_workflow', market_override: override || null}})
            }});
            const data = await res.json();
            if (!data.ok) {{
                alert(data.error || '启动失败');
                runDailyWorkflowButton.disabled = false;
                runDailyWorkflowButton.textContent = '⚡ 每日一键执行（刷新行情→复盘昨日→生成今日候选→模拟交易）';
            }} else {{
                appendLog(`每日一键执行已触发：${{data.message || 'ok'}}`);
                refreshStatus();
            }}
        }} catch(e) {{
            appendLog('每日一键执行请求失败：' + e.message);
            runDailyWorkflowButton.disabled = false;
            runDailyWorkflowButton.textContent = '⚡ 每日一键执行（刷新行情→复盘昨日→生成今日候选→模拟交易）';
        }}
    }});
    refreshActiveMarketValue();
    loadCandidateList();
    refreshStatus();
    setInterval(refreshStatus, 1500);

    // ─── 持仓管理 ───
    let currentSellId = null;
    let holdingsData = [];
    async function checkHoldings() {{
      const btn = document.getElementById('btnCheckHoldings');
      btn.disabled = true; btn.textContent = '正在检查持仓...';
      const el = document.getElementById('holdingsTable');
      el.innerHTML = '<div style="color:var(--muted);">正在拉取行情并检测卖出信号，请稍候...</div>';
      try {{
        const res = await fetch('/holdings/signals');
        const data = await res.json();
        if (!data.ok) {{ el.innerHTML = '<div style="color:var(--danger);">错误：' + data.error + '</div>'; return; }}
        renderHoldings(data.holdings || []);
      }} catch (e) {{
        el.innerHTML = '<div style="color:var(--danger);">请求失败：' + e.message + '</div>';
      }} finally {{
        btn.disabled = false; btn.textContent = '检查持仓（卖出信号+盈亏）';
      }}
    }}
    function renderHoldings(holdings) {{
      const el = document.getElementById('holdingsTable');
      holdingsData = holdings;
      if (!holdings.length) {{ el.innerHTML = '<div style="color:var(--muted);">暂无持仓记录。点击"添加持仓"按钮录入。</div>'; return; }}
      let html = '<table style="width:100%;border-collapse:collapse;font-size:13px;"><thead><tr style="background:var(--bg);">';
      html += '<th style="padding:6px;border:1px solid var(--rule);">代码</th>';
      html += '<th style="padding:6px;border:1px solid var(--rule);">名称</th>';
      html += '<th style="padding:6px;border:1px solid var(--rule);">买入日期</th>';
      html += '<th style="padding:6px;border:1px solid var(--rule);">买入价</th>';
      html += '<th style="padding:6px;border:1px solid var(--rule);">股数</th>';
      html += '<th style="padding:6px;border:1px solid var(--rule);">现价</th>';
      html += '<th style="padding:6px;border:1px solid var(--rule);">市值</th>';
      html += '<th style="padding:6px;border:1px solid var(--rule);">盈亏</th>';
      html += '<th style="padding:6px;border:1px solid var(--rule);">盈亏%</th>';
      html += '<th style="padding:6px;border:1px solid var(--rule);">R/R</th>';
      html += '<th style="padding:6px;border:1px solid var(--rule);">卖出信号</th>';
      html += '<th style="padding:6px;border:1px solid var(--rule);">操作</th>';
      html += '</tr></thead><tbody>';
      holdings.forEach((h, idx) => {{
        const pnlColor = h.pnl >= 0 ? 'var(--success)' : 'var(--danger)';
        const signals = (h.signals || []).map(s => '<span style="background:' + (s.priority <= 1 ? '#fde8e8' : s.priority === 2 ? '#fff3e0' : '#e3f2fd') + ';color:' + (s.priority <= 1 ? 'var(--danger)' : s.priority === 2 ? '#e65100' : '#1565c0') + ';padding:2px 6px;border-radius:4px;font-size:11px;margin:1px;display:inline-block;">' + s.rule + '</span>').join('');
        const errNote = h.error ? '<span style="color:var(--danger);font-size:11px;">' + h.error + '</span>' : '';
        html += '<tr>';
        html += '<td style="padding:6px;border:1px solid var(--rule);">' + h.symbol + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);">' + (h.name || '') + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);">' + h.buy_date + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);">' + h.buy_price + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);">' + h.shares + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);">' + (h.current_price || '—') + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);">' + (h.market_value != null ? h.market_value.toLocaleString() : '—') + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);color:' + pnlColor + ';">' + (h.pnl != null ? h.pnl.toFixed(2) : '—') + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);color:' + pnlColor + ';">' + (h.pnl_pct != null ? h.pnl_pct.toFixed(2) + '%' : '—') + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);">' + (h.rr != null ? h.rr : '—') + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);">' + (signals || errNote || '—') + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);"><button onclick="showSellModal(' + idx + ')" style="padding:3px 8px;font-size:11px;">卖出</button> <button onclick="deleteHolding(' + h.id + ')" style="padding:3px 8px;font-size:11px;color:var(--danger);">删除</button></td>';
        html += '</tr>';
      }});
      html += '</tbody></table>';
      el.innerHTML = html;
    }}
    function showAddHoldingForm() {{
      document.getElementById('addHoldingForm').style.display = 'block';
      document.getElementById('hfDate').value = new Date().toISOString().slice(0, 10);
    }}
    async function submitAddHolding() {{
      const payload = {{
        symbol: document.getElementById('hfSymbol').value,
        name: document.getElementById('hfName').value,
        buy_date: document.getElementById('hfDate').value,
        buy_price: parseFloat(document.getElementById('hfPrice').value),
        shares: parseInt(document.getElementById('hfShares').value),
        commission: parseFloat(document.getElementById('hfCommission').value) || 0,
      }};
      if (!payload.symbol || !payload.buy_date || !payload.buy_price || !payload.shares) {{
        alert('请填写代码、日期、买入价和股数'); return;
      }}
      const res = await fetch('/holdings', {{ method: 'POST', headers: {{'Content-Type':'application/json'}}, body: JSON.stringify(payload) }});
      const data = await res.json();
      if (data.ok) {{ alert('添加成功'); document.getElementById('addHoldingForm').style.display = 'none'; checkHoldings(); }}
      else {{ alert('添加失败：' + data.error); }}
    }}
    function showSellModal(idx) {{
      const h = holdingsData[idx];
      if (!h) return;
      currentSellId = h.id;
      document.getElementById('sellInfo').textContent = h.symbol + ' ' + (h.name || '') + ' | 持仓 ' + h.shares + ' 股 | 买入价 ' + h.buy_price;
      document.getElementById('sellDate').value = new Date().toISOString().slice(0, 10);
      document.getElementById('sellShares').value = h.shares;
      document.getElementById('sellPrice').value = '';
      document.getElementById('sellModal').style.display = 'flex';
    }}
    async function confirmSell() {{
      if (!currentSellId) return;
      const payload = {{
        sell_date: document.getElementById('sellDate').value,
        sell_price: parseFloat(document.getElementById('sellPrice').value),
        sell_shares: parseInt(document.getElementById('sellShares').value),
        commission: parseFloat(document.getElementById('sellCommission').value) || 0,
      }};
      if (!payload.sell_date || !payload.sell_price || !payload.sell_shares) {{ alert('请填写日期、卖出价和股数'); return; }}
      const res = await fetch('/holdings/' + currentSellId + '/sell', {{ method: 'POST', headers: {{'Content-Type':'application/json'}}, body: JSON.stringify(payload) }});
      const data = await res.json();
      if (data.ok) {{
        alert('卖出成功！盈亏：' + data.pnl.toFixed(2) + ' 元 (' + data.pnl_pct.toFixed(2) + '%)');
        document.getElementById('sellModal').style.display = 'none';
        checkHoldings();
      }} else {{
        alert('卖出失败：' + data.error);
      }}
    }}
    async function deleteHolding(id) {{
      if (!confirm('确定删除此持仓记录？相关交易流水也会一并删除。')) return;
      const res = await fetch('/holdings/' + id, {{ method: 'DELETE' }});
      const data = await res.json();
      if (data.ok) {{ checkHoldings(); }} else {{ alert('删除失败：' + data.error); }}
    }}
    async function loadTransactions() {{
      const el = document.getElementById('holdingsTable');
      el.innerHTML = '<div style="color:var(--muted);">加载中...</div>';
      const res = await fetch('/transactions');
      const data = await res.json();
      if (!data.ok) {{ el.innerHTML = '<div style="color:var(--danger);">错误：' + data.error + '</div>'; return; }}
      const txns = data.transactions || [];
      if (!txns.length) {{ el.innerHTML = '<div style="color:var(--muted);">暂无交易记录。</div>'; return; }}
      let html = '<table style="width:100%;border-collapse:collapse;font-size:13px;"><thead><tr style="background:var(--bg);">';
      html += '<th style="padding:6px;border:1px solid var(--rule);">日期</th><th style="padding:6px;border:1px solid var(--rule);">代码</th><th style="padding:6px;border:1px solid var(--rule);">名称</th><th style="padding:6px;border:1px solid var(--rule);">类型</th><th style="padding:6px;border:1px solid var(--rule);">价格</th><th style="padding:6px;border:1px solid var(--rule);">股数</th><th style="padding:6px;border:1px solid var(--rule);">金额</th><th style="padding:6px;border:1px solid var(--rule);">盈亏</th><th style="padding:6px;border:1px solid var(--rule);">盈亏%</th>';
      html += '</tr></thead><tbody>';
      txns.forEach(t => {{
        const isBuy = t.type === 'buy';
        const pnlColor = (t.pnl || 0) >= 0 ? 'var(--success)' : 'var(--danger)';
        html += '<tr>';
        html += '<td style="padding:6px;border:1px solid var(--rule);">' + t.date + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);">' + t.symbol + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);">' + (t.name || '') + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);color:' + (isBuy ? 'var(--accent)' : 'var(--danger)') + ';">' + (isBuy ? '买入' : '卖出') + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);">' + t.price + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);">' + t.shares + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);">' + (t.amount || 0).toFixed(0) + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);color:' + pnlColor + ';">' + (t.pnl != null ? t.pnl.toFixed(2) : '—') + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);color:' + pnlColor + ';">' + (t.pnl_pct != null ? t.pnl_pct.toFixed(2) + '%' : '—') + '</td>';
        html += '</tr>';
      }});
      html += '</tbody></table>';
      el.innerHTML = html;
    }}

    // ─── 模拟交易 ───
    async function loadSimulationData() {{
      const hint = document.getElementById('simStatusHint');
      if (hint) hint.textContent = '加载中...';

      // 并行加载所有数据
      const [accountRes, holdingsRes, navRes, txnsRes] = await Promise.all([
        fetch('/simulation/account'),
        fetch('/simulation/holdings'),
        fetch('/simulation/nav-history'),
        fetch('/simulation/transactions'),
      ]);

      const accountData = await accountRes.json();
      const holdingsData = await holdingsRes.json();
      const navData = await navRes.json();
      const txnsData = await txnsRes.json();

      // 更新账户状态
      if (accountData.ok && accountData.account) {{
        const a = accountData.account;
        document.getElementById('simAccountName').textContent = a.name || '-';
        document.getElementById('simInitialCapital').textContent = a.initial_capital ? a.initial_capital.toLocaleString('zh-CN', {{minimumFractionDigits: 2}}) + ' 元' : '-';
        document.getElementById('simCurrentNav').textContent = a.current_nav ? a.current_nav.toLocaleString('zh-CN', {{minimumFractionDigits: 2}}) + ' 元' : '-';
        const retEl = document.getElementById('simTotalReturn');
        const retPct = a.total_return_pct || 0;
        retEl.textContent = retPct.toFixed(2) + '%';
        retEl.style.color = retPct >= 0 ? 'var(--success)' : 'var(--danger)';
        document.getElementById('simHoldingCount').textContent = a.holding_count != null ? a.holding_count + ' 只' : '-';
        document.getElementById('simWinRate').textContent = a.win_rate != null ? a.win_rate + '% (' + (a.win_count || 0) + '/' + (a.closed_count || 0) + ')' : '-';
        if (hint) hint.textContent = '数据已更新';
      }} else {{
        if (hint) hint.textContent = '账户未初始化：' + (accountData.error || '未知错误');
      }}

      // 渲染持仓
      renderSimHoldings(holdingsData);

      // 渲染净值曲线
      renderSimNavChart(navData);

      // 渲染交易记录
      renderSimTransactions(txnsData);
    }}

    function renderSimHoldings(data) {{
      const el = document.getElementById('simHoldingsTable');
      if (!el) return;
      if (!data.ok) {{ el.innerHTML = '<div style="color:var(--danger);">错误：' + data.error + '</div>'; return; }}
      const holdings = data.holdings || [];
      if (!holdings.length) {{ el.innerHTML = '<div style="color:var(--muted);padding:20px;text-align:center;">暂无持仓</div>'; return; }}
      let html = '<table style="width:100%;border-collapse:collapse;font-size:13px;"><thead><tr style="background:var(--bg);">';
      html += '<th style="padding:6px;border:1px solid var(--rule);">代码</th><th style="padding:6px;border:1px solid var(--rule);">名称</th><th style="padding:6px;border:1px solid var(--rule);">买入日期</th><th style="padding:6px;border:1px solid var(--rule);">买入价</th><th style="padding:6px;border:1px solid var(--rule);">现价</th><th style="padding:6px;border:1px solid var(--rule);">股数</th><th style="padding:6px;border:1px solid var(--rule);">盈亏</th><th style="padding:6px;border:1px solid var(--rule);">盈亏%</th>';
      html += '</tr></thead><tbody>';
      holdings.forEach(h => {{
        const pnl = h.pnl || 0;
        const pnlPct = h.pnl_pct || 0;
        const pnlColor = pnl >= 0 ? 'var(--success)' : 'var(--danger)';
        html += '<tr>';
        html += '<td style="padding:6px;border:1px solid var(--rule);">' + h.symbol + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);">' + (h.name || '') + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);">' + h.buy_date + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);">' + h.buy_price.toFixed(2) + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);">' + (h.current_price ? h.current_price.toFixed(2) : '—') + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);">' + h.shares + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);color:' + pnlColor + ';">' + pnl.toFixed(2) + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);color:' + pnlColor + ';">' + pnlPct.toFixed(2) + '%</td>';
        html += '</tr>';
      }});
      html += '</tbody></table>';
      el.innerHTML = html;
    }}

    function renderSimNavChart(data) {{
      const el = document.getElementById('simNavChart');
      if (!el) return;
      if (!data.ok) {{ el.innerHTML = '<div style="color:var(--danger);">错误：' + data.error + '</div>'; return; }}
      const history = data.history || [];
      const initialCapital = data.initial_capital || 100000;
      if (!history.length) {{ el.innerHTML = '<div style="color:var(--muted);padding:20px;text-align:center;">暂无净值数据</div>'; return; }}

      // 计算图表尺寸
      const width = 480;
      const height = 260;
      const padding = {{ top: 20, right: 20, bottom: 30, left: 60 }};
      const chartW = width - padding.left - padding.right;
      const chartH = height - padding.top - padding.bottom;

      // 找净值范围
      let minNav = Infinity, maxNav = -Infinity;
      history.forEach(h => {{ if (h.nav < minNav) minNav = h.nav; if (h.nav > maxNav) maxNav = h.nav; }});
      // 以初始资金为基准线，上下留空间
      const baseLine = initialCapital;
      const range = Math.max(maxNav - baseLine, baseLine - minNav);
      const paddingRange = range * 0.2 + initialCapital * 0.01;
      const yMax = baseLine + paddingRange;
      const yMin = baseLine - paddingRange;
      const yRange = yMax - yMin;

      // 生成路径点
      const points = history.map((h, i) => {{
        const x = padding.left + (i / (history.length - 1 || 1)) * chartW;
        const y = padding.top + (1 - (h.nav - yMin) / yRange) * chartH;
        return {{ x, y, nav: h.nav, date: h.date }};
      }});

      // 构建SVG路径
      let pathD = 'M ' + points[0].x + ' ' + points[0].y;
      for (let i = 1; i < points.length; i++) {{
        pathD += ' L ' + points[i].x + ' ' + points[i].y;
      }}

      // 基准线（初始资金）
      const baseY = padding.top + (1 - (baseLine - yMin) / yRange) * chartH;

      // 填充区域路径
      let areaD = pathD + ' L ' + points[points.length - 1].x + ' ' + (padding.top + chartH);
      areaD += ' L ' + points[0].x + ' ' + (padding.top + chartH) + ' Z';

      // 判断整体盈亏颜色
      const latestNav = history[history.length - 1].nav;
      const isProfit = latestNav >= baseLine;
      const lineColor = isProfit ? '#178a54' : '#b9382f';
      const areaColor = isProfit ? 'rgba(23, 138, 84, 0.12)' : 'rgba(185, 56, 47, 0.12)';

      let svg = '<svg width="100%" height="100%" viewBox="0 0 ' + width + ' ' + height + '" preserveAspectRatio="xMidYMid meet">';

      // Y轴刻度
      for (let i = 0; i <= 4; i++) {{
        const y = padding.top + (i / 4) * chartH;
        const value = yMax - (i / 4) * yRange;
        svg += '<line x1="' + padding.left + '" y1="' + y + '" x2="' + (width - padding.right) + '" y2="' + y + '" stroke="#eee" stroke-width="1"/>';
        svg += '<text x="' + (padding.left - 6) + '" y="' + (y + 4) + '" text-anchor="end" fill="#999" font-size="11">' + value.toFixed(0) + '</text>';
      }}

      // 基准线
      svg += '<line x1="' + padding.left + '" y1="' + baseY + '" x2="' + (width - padding.right) + '" y2="' + baseY + '" stroke="#999" stroke-width="1" stroke-dasharray="4,4"/>';
      svg += '<text x="' + (width - padding.right) + '" y="' + (baseY - 4) + '" text-anchor="end" fill="#999" font-size="11">初始资金</text>';

      // 填充区域
      svg += '<path d="' + areaD + '" fill="' + areaColor + '"/>';

      // 净值曲线
      svg += '<path d="' + pathD + '" fill="none" stroke="' + lineColor + '" stroke-width="2"/>';

      // 最新点
      const last = points[points.length - 1];
      svg += '<circle cx="' + last.x + '" cy="' + last.y + '" r="4" fill="' + lineColor + '"/>';
      svg += '<text x="' + last.x + '" y="' + (last.y - 8) + '" text-anchor="middle" fill="' + lineColor + '" font-size="12" font-weight="bold">' + last.nav.toFixed(0) + '</text>';

      // X轴日期（起点和终点）
      svg += '<text x="' + padding.left + '" y="' + (height - padding.bottom + 18) + '" text-anchor="start" fill="#999" font-size="11">' + history[0].date + '</text>';
      svg += '<text x="' + (width - padding.right) + '" y="' + (height - padding.bottom + 18) + '" text-anchor="end" fill="#999" font-size="11">' + history[history.length - 1].date + '</text>';

      svg += '</svg>';
      el.innerHTML = svg;
    }}

    function renderSimTransactions(data) {{
      const el = document.getElementById('simTransactionsTable');
      if (!el) return;
      if (!data.ok) {{ el.innerHTML = '<div style="color:var(--danger);">错误：' + data.error + '</div>'; return; }}
      const txns = data.transactions || [];
      if (!txns.length) {{ el.innerHTML = '<div style="color:var(--muted);padding:20px;text-align:center;">暂无交易记录</div>'; return; }}
      let html = '<table style="width:100%;border-collapse:collapse;font-size:13px;"><thead><tr style="background:var(--bg);">';
      html += '<th style="padding:6px;border:1px solid var(--rule);">日期</th><th style="padding:6px;border:1px solid var(--rule);">代码</th><th style="padding:6px;border:1px solid var(--rule);">名称</th><th style="padding:6px;border:1px solid var(--rule);">类型</th><th style="padding:6px;border:1px solid var(--rule);">价格</th><th style="padding:6px;border:1px solid var(--rule);">股数</th><th style="padding:6px;border:1px solid var(--rule);">金额</th><th style="padding:6px;border:1px solid var(--rule);">盈亏</th><th style="padding:6px;border:1px solid var(--rule);">原因</th>';
      html += '</tr></thead><tbody>';
      txns.forEach(t => {{
        const isBuy = t.type === 'buy';
        const pnl = t.pnl || 0;
        const pnlColor = pnl >= 0 ? 'var(--success)' : 'var(--danger)';
        html += '<tr>';
        html += '<td style="padding:6px;border:1px solid var(--rule);">' + t.date + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);">' + t.symbol + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);">' + (t.name || '') + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);color:' + (isBuy ? 'var(--accent)' : 'var(--danger)') + ';">' + (isBuy ? '买入' : '卖出') + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);">' + t.price + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);">' + t.shares + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);">' + (t.amount || 0).toFixed(0) + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);color:' + pnlColor + ';">' + (t.pnl != null ? t.pnl.toFixed(2) : '—') + '</td>';
        html += '<td style="padding:6px;border:1px solid var(--rule);">' + (t.reason || '') + '</td>';
        html += '</tr>';
      }});
      html += '</tbody></table>';
      el.innerHTML = html;
    }}

    async function runSimulation() {{
      const btn = document.getElementById('runSimulationBtn');
      const hint = document.getElementById('simStatusHint');
      if (btn) btn.disabled = true;
      if (hint) hint.textContent = '模拟交易执行中...';

      const res = await fetch('/simulation/run', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: '{{}}'
      }});
      const data = await res.json();
      if (!data.ok) {{
        alert(data.error || '启动失败');
        if (btn) btn.disabled = false;
        if (hint) hint.textContent = '启动失败';
        return;
      }}

      // 轮询任务状态
      const checkInterval = setInterval(async () => {{
        const statusRes = await fetch('/status');
        const status = await statusRes.json();
        if (!status.running) {{
          clearInterval(checkInterval);
          if (btn) btn.disabled = false;
          await loadSimulationData();
          if (hint) hint.textContent = '模拟交易完成';
        }} else {{
          if (hint) hint.textContent = '执行中：' + (status.action || '');
        }}
      }}, 2000);
    }}
  </script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data: dict, status: int = 200) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/?"):
            self._send(200, _html_page().encode("utf-8"), "text/html; charset=utf-8")
            return
        if self.path == "/status":
            with STATE_LOCK:
                self._send_json(dict(STATE))
            return
        if self.path == "/active-market-value":
            self._send_json(_read_active_market_value())
            return
        if self.path.startswith("/tdx-codes"):
            limit = None
            if "limit=10" in self.path:
                limit = 10
            self._send_json(_read_tdx_candidate_codes(limit))
            return
        if self.path.startswith("/candidate-list"):
            self._send_json(_read_candidate_rows())
            return
        if self.path.startswith("/history-list"):
            self._send_json(_read_history_list())
            return
        if self.path == "/db-summary":
            self._send_json(_db_summary())
            return
        if self.path.startswith("/db-candidates"):
            parsed = urlparse(self.path)
            target_date = parse_qs(parsed.query).get("date", [""])[0]
            if not target_date:
                target_date = _last_trading_day().isoformat()
            self._send_json(_db_candidates(target_date))
            return
        if self.path.startswith("/db-reviews"):
            parsed = urlparse(self.path)
            target_date = parse_qs(parsed.query).get("date", [""])[0]
            if not target_date:
                target_date = _last_trading_day().isoformat()
            self._send_json(_db_reviews(target_date))
            return
        if self.path.startswith("/db-symbol-history"):
            parsed = urlparse(self.path)
            symbol = parse_qs(parsed.query).get("symbol", [""])[0]
            if not symbol:
                self._send_json({"ok": False, "error": "缺少 symbol 参数"}, 400)
                return
            self._send_json(_db_symbol_history(symbol))
            return
        if self.path == "/holdings":
            self._send_json(_holdings_list())
            return
        if self.path == "/holdings/signals":
            self._send_json(_holdings_signals())
            return
        if self.path.startswith("/transactions"):
            parsed = urlparse(self.path)
            symbol = parse_qs(parsed.query).get("symbol", [""])[0]
            self._send_json(_transactions_list(symbol))
            return
        # ─── 模拟交易接口 ───
        if self.path == "/simulation/account":
            self._send_json(_sim_get_account())
            return
        if self.path == "/simulation/holdings":
            self._send_json(_sim_get_holdings())
            return
        if self.path == "/simulation/nav-history":
            self._send_json(_sim_get_nav_history())
            return
        if self.path == "/simulation/transactions":
            self._send_json(_sim_get_transactions())
            return
        if self.path.startswith("/candidate-detail"):
            parsed = urlparse(self.path)
            symbol = parse_qs(parsed.query).get("symbol", [""])[0]
            try:
                self._send_json(_read_candidate_detail(symbol))
            except Exception as exc:
                self._send_json({"ok": False, "error": f"读取个股指标失败：{exc}"}, 500)
            return
        if self.path.startswith("/view/"):
            path = _safe_project_file(self.path[len("/view/"):])
            if not path:
                self._send(404, "文件不存在".encode("utf-8"), "text/plain; charset=utf-8")
                return
            content_type = mimetypes.guess_type(str(path))[0] or "text/plain"
            if path.suffix.lower() in {".html", ".htm"}:
                content_type = "text/html; charset=utf-8"
            elif path.suffix.lower() in {".csv", ".md", ".json", ".txt"}:
                content_type = "text/plain; charset=utf-8"
            self._send(200, path.read_bytes(), content_type)
            return
        self._send(404, "页面不存在".encode("utf-8"), "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError:
            self._send_json({"ok": False, "error": "请求格式错误"}, 400)
            return
        if self.path == "/active-market-value":
            try:
                value = float(payload.get("value"))
            except (TypeError, ValueError):
                self._send_json({"ok": False, "error": "请输入有效数字，例如 4.82"}, 400)
                return
            note = str(payload.get("note", "")).strip()
            data = _write_active_market_value(value, note)
            self._send_json({"ok": True, **data})
            return
        if self.path == "/holdings":
            self._send_json(_holdings_add(payload))
            return
        if self.path.startswith("/holdings/"):
            parts = self.path.split("/")
            if len(parts) >= 3:
                try:
                    holding_id = int(parts[2])
                except ValueError:
                    self._send_json({"ok": False, "error": "无效的持仓 ID"}, 400)
                    return
                if len(parts) >= 4 and parts[3] == "sell":
                    self._send_json(_holdings_sell(holding_id, payload))
                    return
                if len(parts) >= 4 and parts[3] == "import":
                    self._send_json(_holdings_import(payload))
                    return
                self._send_json(_holdings_update(holding_id, payload))
                return
            self._send_json({"ok": False, "error": "无效路径"}, 400)
            return
        # ─── 模拟交易：运行 ───
        if self.path == "/simulation/run":
            self._send_json(_sim_run())
            return
        if self.path != "/run":
            self._send_json({"ok": False, "error": "未知接口"}, 404)
            return
        action = payload.get("action")
        market_override = payload.get("market_override")
        if action == "daily_workflow":
            with STATE_LOCK:
                if STATE["running"]:
                    self._send_json({"ok": False, "error": "已有任务正在运行，请等待完成"}, 409)
                    return
            threading.Thread(target=_run_daily_workflow, args=(market_override,), daemon=True).start()
            self._send_json({"ok": True, "message": "每日一键执行已启动"})
            return
        if action not in COMMANDS:
            self._send_json({"ok": False, "error": "未知操作"}, 400)
            return
        with STATE_LOCK:
            if STATE["running"]:
                self._send_json({"ok": False, "error": "已有任务正在运行，请等待完成"}, 409)
                return
        threading.Thread(target=_run_action, args=(action, market_override), daemon=True).start()
        self._send_json({"ok": True})

    def log_message(self, format: str, *args) -> None:
        return

    def do_DELETE(self) -> None:
        if self.path.startswith("/holdings/"):
            parts = self.path.split("/")
            if len(parts) >= 3:
                try:
                    holding_id = int(parts[2])
                except ValueError:
                    self._send_json({"ok": False, "error": "无效的持仓 ID"}, 400)
                    return
                self._send_json(_holdings_delete(holding_id))
                return
        self._send_json({"ok": False, "error": "无效路径"}, 400)


def main() -> None:
    url = f"http://{HOST}:{PORT}/"
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"操作面板已启动：{url}")
    print("关闭本窗口即可停止服务。")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
