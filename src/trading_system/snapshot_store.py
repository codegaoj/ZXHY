from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable, List

from .models import SecuritySnapshot


SNAPSHOT_FIELDS = [
    "symbol",
    "name",
    "close",
    "prev_close",
    "volume",
    "volume_ma5",
    "active_market_value_pct",
    "macd_dif",
    "macd_dea",
    "white_line",
    "yellow_line",
    "support_price",
    "resistance_price",
    "tags",
]


def parse_tags(raw: str) -> List[str]:
    normalized = str(raw).replace("；", ";").replace("，", ";").replace(",", ";").replace("|", ";")
    return [item.strip() for item in normalized.split(";") if item.strip()]


def snapshot_from_row(row: dict) -> SecuritySnapshot:
    return SecuritySnapshot(
        symbol=str(row["symbol"]).strip(),
        name=str(row["name"]).strip(),
        close=float(row["close"]),
        prev_close=float(row["prev_close"]),
        volume=float(row["volume"]),
        volume_ma5=float(row["volume_ma5"]),
        active_market_value_pct=float(row["active_market_value_pct"]),
        macd_dif=float(row["macd_dif"]),
        macd_dea=float(row["macd_dea"]),
        white_line=float(row["white_line"]),
        yellow_line=float(row["yellow_line"]),
        support_price=float(row["support_price"]),
        resistance_price=float(row["resistance_price"]),
        tags=parse_tags(row.get("tags", "")),
    )


def snapshot_to_row(snapshot: SecuritySnapshot) -> dict:
    return {
        "symbol": snapshot.symbol,
        "name": snapshot.name,
        "close": snapshot.close,
        "prev_close": snapshot.prev_close,
        "volume": snapshot.volume,
        "volume_ma5": snapshot.volume_ma5,
        "active_market_value_pct": snapshot.active_market_value_pct,
        "macd_dif": snapshot.macd_dif,
        "macd_dea": snapshot.macd_dea,
        "white_line": snapshot.white_line,
        "yellow_line": snapshot.yellow_line,
        "support_price": snapshot.support_price,
        "resistance_price": snapshot.resistance_price,
        "tags": ";".join(snapshot.tags),
    }


def load_snapshots(path: Path) -> List[SecuritySnapshot]:
    rows: List[SecuritySnapshot] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            rows.append(snapshot_from_row(row))
    return rows


def save_snapshots(path: Path, snapshots: Iterable[SecuritySnapshot]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SNAPSHOT_FIELDS)
        writer.writeheader()
        for snapshot in snapshots:
            writer.writerow(snapshot_to_row(snapshot))
