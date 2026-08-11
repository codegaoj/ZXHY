from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class ActiveMarketValue:
    value: float
    source: str
    date: str = ""
    note: str = ""


def load_latest_active_market_value(path: Path) -> ActiveMarketValue:
    if not path.exists():
        raise FileNotFoundError(f"指南针活跃市值文件不存在：{path}")

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = [row for row in csv.DictReader(f) if row]

    if not rows:
        raise ValueError("指南针活跃市值文件为空。")

    latest = rows[-1]
    raw_value = latest.get("active_market_value_pct") or latest.get("active_market_value")
    if raw_value is None or str(raw_value).strip() == "":
        raise ValueError("指南针活跃市值文件缺少 active_market_value_pct 字段。")

    return ActiveMarketValue(
        value=float(str(raw_value).strip()),
        source=str(latest.get("source", "compass")).strip() or "compass",
        date=str(latest.get("date", "")).strip(),
        note=str(latest.get("note", "")).strip(),
    )


def resolve_active_market_value(config: dict, project_root: Path, fallback_value: Optional[float] = None) -> ActiveMarketValue:
    source = str(config.get("market_breadth_source", "provider")).lower()
    if source == "compass_csv":
        path = project_root / str(config.get("compass_active_market_value_path", "data/compass_active_market_value.csv"))
        try:
            return load_latest_active_market_value(path)
        except (FileNotFoundError, ValueError) as exc:
            if fallback_value is None:
                raise
            return ActiveMarketValue(
                value=float(fallback_value),
                source="provider_fallback",
                note=f"指南针活跃市值不可用，已回退：{exc}",
            )

    if fallback_value is None:
        raise ValueError("未配置指南针活跃市值，也没有可用的行情源代理值。")
    return ActiveMarketValue(value=float(fallback_value), source="provider")
