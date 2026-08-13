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
}

DAILY_WORKFLOW = {
    "name": "每日一键执行",
    "steps": ["refresh_spot", "review_candidates", "screen_candidates"],
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
        return {"date": "", "value": "", "source": "compass", "note": "文件不存在", "recent": [], "cumulative_3d": ""}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return {"date": "", "value": "", "source": "compass", "note": "文件为空", "recent": [], "cumulative_3d": ""}
    row = rows[-1]
    recent = []
    for r in rows[-3:]:
        raw = r.get("active_market_value_pct") or r.get("active_market_value")
        if raw is not None and str(raw).strip() != "":
            recent.append(float(str(raw).strip()))
    cum_sum = round(sum(recent), 4) if recent else 0.0
    return {
        "date": row.get("date", ""),
        "value": row.get("active_market_value_pct", row.get("active_market_value", "")),
        "source": row.get("source", "compass"),
        "note": row.get("note", ""),
        "recent": recent,
        "cumulative_3d": f"{cum_sum:.2f}",
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


def _supplement_with_spot(df, symbol: str):
    """如果日线数据不含今天，用实时行情补一行今日数据。"""
    import pandas as pd

    if df is None or df.empty:
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

    provider = AkShareProvider(adjust="qfq", retries=2, pause_seconds=0.6)
    end = date.today()
    start = end - timedelta(days=180)
    raw = provider.fetch_daily(symbol, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
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


def _run_action(action: str) -> None:
    item = COMMANDS[action]
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
            item["cmd"],
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


def _run_daily_workflow() -> None:
    """串行执行每日一键执行流程：刷新行情 → 复盘昨日候选股 → 生成今日候选股票。"""
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
        with STATE_LOCK:
            STATE["action"] = f"{DAILY_WORKFLOW['name']} - 第{idx}/{total}步：{item['name']}"
            STATE["log"].append(f"--- 第{idx}/{total}步：{item['name']} ---")

        try:
            proc = subprocess.Popen(
                item["cmd"],
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
    .chart-meta {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin: 12px 0; }}
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
    .agent-review-card {{ border: 1px solid var(--rule); border-radius: 10px; padding: 10px 12px; margin-bottom: 6px; background: #fff; font-size: 13px; }}
    .agent-review-card .review-header {{ display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }}
    .agent-review-card .review-brief {{ color: var(--muted); font-size: 12px; }}
    .risk-badge {{ display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 11px; color: white; }}
    .highlight-text {{ color: #178a54; font-size: 12px; }}
    .issue-text {{ color: #b9382f; font-size: 12px; }}
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

    <section>
      <h2>活跃市值</h2>
      <p>先填写当天指南针活跃市值，再生成报告或每日候选股票。当前值写入 data/compass_active_market_value.csv。多头判定规则：单日 >= 4% 或近3日累计 >= 4%。</p>
      <div class="status">
        <div class="stat"><span>当前日期</span><strong id="amvDate">读取中</strong></div>
        <div class="stat"><span>当前活跃市值</span><strong id="amvValue">读取中</strong></div>
        <div class="stat"><span>近3日累计</span><strong id="amvCumulative">读取中</strong></div>
        <div class="stat"><span>来源</span><strong id="amvSource">-</strong></div>
        <div class="stat"><span>备注</span><strong id="amvNote">-</strong></div>
      </div>
      <div class="form-row">
        <label><span>新活跃市值</span><input id="amvInput" type="number" step="0.01" placeholder="例如 4.82"></label>
        <label><span>备注</span><input id="amvNoteInput" type="text" placeholder="可选，例如 收盘后手动录入"></label>
        <button class="primary-button" id="saveAmv" type="button"><strong>保存活跃市值</strong></button>
      </div>
    </section>

    <section>
      <h2>运行流程</h2>
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
        <button class="primary-button" id="runDailyWorkflow" type="button" style="background:#e65100;font-size:16px;padding:12px 24px;"><strong>⚡ 每日一键执行（刷新行情→复盘昨日→生成今日候选）</strong></button>
      </div>
      <div class="hint">执行顺序：先刷新实时行情，再复盘昨日候选股（用今天行情对比昨天选入价），最后生成今天的候选股。首次使用无历史备份时复盘会跳过，第二天起正常工作。</div>
    </section>

    <section>
      <h2>打开报告</h2>
      <div class="reports">
        {links}
      </div>
    </section>

    <section>
      <h2>历史记录 <button id="loadHistory" type="button" class="primary-button" style="font-size:13px;padding:6px 14px;">刷新历史列表</button></h2>
      <div id="historyList" class="hint">点击"刷新历史列表"查看所有历史候选股和复盘记录。</div>
    </section>

    <section>
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

    <section>
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
            <div class="stat"><span>支撑/压力</span><strong id="detailRange">-</strong></div>
          </div>
          <div class="chart-wrap" id="candidateChart"><div class="hint">图表会显示在这里。</div></div>
          <div class="candidate-reason" id="candidateReason"></div>
        </div>
      </div>
    </section>

    <section class="agent-section">
      <h2>Agent 审核报告</h2>
      <div class="agent-summary" id="agentSummary">点击"刷新候选股列表"后自动加载审核结果。</div>
      <div class="agent-stats" id="agentStats"></div>
      <div id="agentWarnings"></div>
      <div id="agentRecs"></div>
      <div id="agentReviewList"></div>
    </section>

    <section>
      <h2>运行日志</h2>
      <pre id="log">等待操作...</pre>
    </section>
  </main>

  <script>
    const buttons = Array.from(document.querySelectorAll('button[data-action]'));
    const saveAmvButton = document.getElementById('saveAmv');
    const showTdxTop10Button = document.getElementById('showTdxTop10');
    const showTdxAllButton = document.getElementById('showTdxAll');
    const copyTdxCodesButton = document.getElementById('copyTdxCodes');
    const loadCandidatesButton = document.getElementById('loadCandidates');
    async function runAction(action) {{
      const res = await fetch('/run', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ action }})
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
      const reviewCards = items.filter(it => it.opinion || it.issues.length > 0 || it.highlights.length > 0).map(it => {{
        const opinionTag = it.opinion ? '<span class="risk-badge" style="background:' + (opinionColors[it.opinion] || '#999') + ';">' + it.opinion + '</span>' : '';
        const riskTag = it.risk_level > 0 ? '<span class="risk-badge" style="background:' + (riskColors[it.risk_level] || '#999') + ';">风险' + it.risk_level + '</span>' : '';
        const rrTag = it.risk_reward > 0 ? '<span style="font-size:11px;color:#667085;">R/R=' + it.risk_reward + '</span>' : '';
        const highlights = it.highlights.length > 0 ? '<div>' + it.highlights.map(h => '<span class="highlight-text">★ ' + h + '</span>').join('<br>') + '</div>' : '';
        const issues = it.issues.length > 0 ? '<div>' + it.issues.map(i => '<span class="issue-text">- ' + i + '</span>').join('<br>') + '</div>' : '';
        const posSug = it.position_suggestion ? '<div style="margin-top:4px;color:#355cff;font-size:12px;">仓位：' + it.position_suggestion + '</div>' : '';
        const brief = it.brief ? '<div class="review-brief">' + it.brief + '</div>' : '';
        return '<div class="agent-review-card"><div class="review-header"><strong>' + it.symbol + ' ' + it.name + '</strong>' + opinionTag + riskTag + rrTag + '</div>' + brief + highlights + issues + posSug + '</div>';
      }}).join('');
      listEl.innerHTML = reviewCards || '<div style="color:#667085;font-size:13px;">暂无逐只审核数据。</div>';
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
      const w = 980, priceH = 340, macdH = 135, brickH = 120, left = 54, right = 24, top = 24, gap = 34;
      const totalH = top + priceH + gap + macdH + gap + brickH + 34;
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
      const brickMax = Math.max(1, ...records.map(r => r.brick || 0));
      function x(i) {{ return left + i * step + step / 2; }}
      function priceY(v) {{ return top + (pMax - v) / (pMax - pMin) * priceH; }}
      const macdTop = top + priceH + gap;
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
      buttons.forEach(btn => btn.disabled = data.running);
      saveAmvButton.disabled = data.running;
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
        if (!confirm('将依次执行：刷新行情缓存→复盘昨日候选股→生成今日候选股票。约需3-5分钟，确认开始？')) return;
        runDailyWorkflowButton.disabled = true;
        runDailyWorkflowButton.textContent = '执行中...';
        try {{
            const res = await fetch('/run', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{action: 'daily_workflow'}})
            }});
            const data = await res.json();
            appendLog(`每日一键执行已触发：${{data.message || 'ok'}}`);
            refreshStatus();
        }} catch(e) {{
            appendLog('每日一键执行请求失败：' + e.message);
        }} finally {{
            setTimeout(() => {{
                runDailyWorkflowButton.disabled = false;
                runDailyWorkflowButton.textContent = '⚡ 每日一键执行（刷新行情→复盘昨日→生成今日候选）';
            }}, 5000);
        }}
    }});
    refreshActiveMarketValue();
    loadCandidateList();
    refreshStatus();
    setInterval(refreshStatus, 1500);
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
        if self.path != "/run":
            self._send_json({"ok": False, "error": "未知接口"}, 404)
            return
        action = payload.get("action")
        if action == "daily_workflow":
            with STATE_LOCK:
                if STATE["running"]:
                    self._send_json({"ok": False, "error": "已有任务正在运行，请等待完成"}, 409)
                    return
            threading.Thread(target=_run_daily_workflow, daemon=True).start()
            self._send_json({"ok": True, "message": "每日一键执行已启动"})
            return
        if action not in COMMANDS:
            self._send_json({"ok": False, "error": "未知操作"}, 400)
            return
        with STATE_LOCK:
            if STATE["running"]:
                self._send_json({"ok": False, "error": "已有任务正在运行，请等待完成"}, 409)
                return
        threading.Thread(target=_run_action, args=(action,), daemon=True).start()
        self._send_json({"ok": True})

    def log_message(self, format: str, *args) -> None:
        return


def main() -> None:
    url = f"http://{HOST}:{PORT}/"
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"操作面板已启动：{url}")
    print("关闭本窗口即可停止服务。")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
