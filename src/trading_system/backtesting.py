from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterable, List

import pandas as pd

from .data_normalizer import WatchSymbol, build_snapshot, normalize_daily_frame
from .formula_indicators import evaluate_formulas
from .models import SecuritySnapshot


RULES = ["B1", "S1", "滴滴", "砖形图"]


@dataclass
class BacktestEvent:
    symbol: str
    name: str
    date: str
    rule: str
    close: float
    future_close: float
    forward_return_pct: float
    success: bool
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def _detect_rule_hits(snapshot: SecuritySnapshot, window: pd.DataFrame) -> Dict[str, str]:
    formula = evaluate_formulas(window)
    hits: Dict[str, str] = {}

    if formula.signals.get("B1买点"):
        hits["B1"] = "J<15 且价格位于知行多空线之上，短期趋势线也在多空线之上。"

    if "S1/卖点" in snapshot.tags and snapshot.pct_change < -2:
        hits["S1"] = "标签命中 S1/卖点，且当日跌幅超过 2%。"

    if "滴滴战法" in snapshot.tags and snapshot.close < snapshot.prev_close and snapshot.volume < snapshot.volume_ma5:
        hits["滴滴"] = "标签命中滴滴战法，且缩量下跌。"

    if formula.signals.get("砖型图反红"):
        hits["砖形图"] = "砖型图昨天绿柱、今天反红，且高度达标。"

    return hits


def _is_success(rule: str, forward_return_pct: float) -> bool:
    if rule in {"B1", "砖形图"}:
        return forward_return_pct > 0
    if rule in {"S1", "滴滴"}:
        return forward_return_pct < 0
    return False


def run_rule_backtest(
    provider,
    watchlist: Iterable[WatchSymbol],
    active_market_value_pct: float,
    start_date: str,
    end_date: str,
    holding_days: int = 5,
    min_lookback: int = 40,
    limit: int | None = None,
) -> dict:
    events: List[BacktestEvent] = []
    failures: List[dict] = []
    selected = list(watchlist)
    if limit is not None and limit > 0:
        selected = selected[:limit]

    for item in selected:
        try:
            raw = provider.fetch_daily(item.symbol, start_date, end_date)
            df = normalize_daily_frame(raw)
            if len(df) <= min_lookback + holding_days:
                failures.append({"symbol": item.symbol, "name": item.name, "error": "历史数据不足，无法回测。"})
                continue

            for idx in range(min_lookback, len(df) - holding_days):
                window = df.iloc[: idx + 1].copy()
                snapshot = build_snapshot(item, window, active_market_value_pct)
                hits = _detect_rule_hits(snapshot, window)
                if not hits:
                    continue
                current = df.iloc[idx]
                future = df.iloc[idx + holding_days]
                close = float(current["close"])
                future_close = float(future["close"])
                if close == 0:
                    continue
                forward_return_pct = (future_close / close - 1) * 100
                for rule, reason in hits.items():
                    events.append(BacktestEvent(
                        symbol=item.symbol,
                        name=item.name,
                        date=str(pd.to_datetime(current["date"]).date()),
                        rule=rule,
                        close=round(close, 4),
                        future_close=round(future_close, 4),
                        forward_return_pct=round(forward_return_pct, 4),
                        success=_is_success(rule, forward_return_pct),
                        reason=reason,
                    ))
        except Exception as exc:
            failures.append({"symbol": item.symbol, "name": item.name, "error": str(exc)})

    summary = summarize_events(events)
    return {
        "summary": summary,
        "events": [event.to_dict() for event in events],
        "failures": failures,
        "params": {
            "start_date": start_date,
            "end_date": end_date,
            "holding_days": holding_days,
            "min_lookback": min_lookback,
            "symbols": len(selected),
        },
    }


def summarize_events(events: List[BacktestEvent]) -> dict:
    summary: Dict[str, dict] = {}
    for rule in RULES:
        rule_events = [event for event in events if event.rule == rule]
        count = len(rule_events)
        success_count = sum(1 for event in rule_events if event.success)
        avg_return = sum(event.forward_return_pct for event in rule_events) / count if count else 0.0
        summary[rule] = {
            "count": count,
            "success_count": success_count,
            "success_rate_pct": round(success_count / count * 100, 2) if count else 0.0,
            "avg_forward_return_pct": round(avg_return, 4),
        }
    summary["total_events"] = len(events)
    return summary


def write_backtest_markdown(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# 历史规则回测报告", ""]
    params = result.get("params", {})
    lines.extend([
        f"- 回测区间：{params.get('start_date')} 至 {params.get('end_date')}",
        f"- 持有观察期：{params.get('holding_days')} 个交易日",
        f"- 股票数量：{params.get('symbols')}",
        "",
        "## 规则统计",
        "",
        "| 规则 | 命中次数 | 成功次数 | 成功率 | 平均后续收益 |",
        "|---|---:|---:|---:|---:|",
    ])
    summary = result.get("summary", {})
    for rule in RULES:
        item = summary.get(rule, {})
        lines.append(
            f"| {rule} | {item.get('count', 0)} | {item.get('success_count', 0)} | "
            f"{item.get('success_rate_pct', 0)}% | {item.get('avg_forward_return_pct', 0)}% |"
        )

    events = result.get("events", [])[:50]
    lines.extend(["", "## 最近事件样例", ""])
    if events:
        for event in events:
            lines.append(
                f"- {event['date']} {event['symbol']} {event['name']}：{event['rule']}，"
                f"后续收益 {event['forward_return_pct']}%，{'成功' if event['success'] else '未成功'}。"
            )
    else:
        lines.append("- 本次没有命中事件。")

    failures = result.get("failures", [])
    if failures:
        lines.extend(["", "## 数据失败项", ""])
        for failure in failures:
            lines.append(f"- {failure['symbol']} {failure['name']}：{failure['error']}")

    path.write_text("\n".join(lines), encoding="utf-8")
