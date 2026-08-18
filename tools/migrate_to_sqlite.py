"""将现有 CSV/JSON 历史数据迁移到 SQLite 数据库。

用法: python tools/migrate_to_sqlite.py
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from trading_system.storage import Storage


def migrate_active_market_values(db: Storage) -> int:
    path = PROJECT_ROOT / "data" / "compass_active_market_value.csv"
    if not path.exists():
        print(f"[跳过] {path} 不存在")
        return 0
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    count = 0
    for row in rows:
        try:
            amv_date = row.get("date", "").strip()
            if not amv_date:
                continue
            raw = row.get("active_market_value_pct") or row.get("active_market_value")
            if raw is None or str(raw).strip() == "":
                continue
            pct = float(str(raw).strip())
            db.save_active_market_value(
                amv_date,
                pct,
                pct,
                source=row.get("source", "compass"),
                note=row.get("note", ""),
            )
            count += 1
        except Exception as exc:
            print(f"  跳过 {row}: {exc}")
    print(f"[活跃市值] 导入 {count} 条")
    return count


def migrate_daily_candidates(db: Storage) -> int:
    history_dir = PROJECT_ROOT / "outputs" / "history"
    if not history_dir.exists():
        print("[跳过] history 目录不存在")
        return 0
    json_files = sorted(history_dir.glob("daily_candidates_*.json"))
    count = 0
    for jf in json_files:
        try:
            data = json.loads(jf.read_text(encoding="utf-8-sig"))
            cand_date = data.get("date", jf.stem.replace("daily_candidates_", ""))
            candidates = data.get("candidates", [])
            if not candidates:
                continue
            db.save_daily_candidates(cand_date, candidates)
            # 同时保存市场状态
            market_regime = data.get("market_regime", "")
            market_reason = data.get("market_reason", "")
            if market_regime:
                stats = data.get("stats", {})
                db.save_market_state(
                    state_date=cand_date,
                    regime=market_regime,
                    active_market_value_pct=float(stats.get("active_market_value_pct", 0)),
                    macd_above_zero=False,
                    reason=market_reason,
                    max_position_pct=0,
                    single_symbol_pct=0,
                    manual_override=data.get("manual_override", False),
                )
            count += len(candidates)
            print(f"  {cand_date}: {len(candidates)} 条候选股")
        except Exception as exc:
            print(f"  跳过 {jf.name}: {exc}")
    print(f"[候选股] 导入 {count} 条")
    return count


def migrate_reviews(db: Storage) -> int:
    history_dir = PROJECT_ROOT / "outputs" / "history"
    if not history_dir.exists():
        print("[跳过] history 目录不存在")
        return 0
    json_files = sorted(history_dir.glob("review_*.json"))
    count = 0
    for jf in json_files:
        try:
            data = json.loads(jf.read_text(encoding="utf-8-sig"))
            stats = data.get("stats", {})
            rows = data.get("rows", [])
            if not rows:
                continue
            selected_date = stats.get("selected_date", jf.stem.replace("review_", ""))
            review_date = stats.get("review_date", "")
            db.save_candidate_reviews(selected_date, review_date, rows)
            db.save_review_stats(review_date, selected_date, stats)
            count += len(rows)
            print(f"  {selected_date}: {len(rows)} 条复盘记录")
        except Exception as exc:
            print(f"  跳过 {jf.name}: {exc}")
    print(f"[复盘] 导入 {count} 条")
    return count


def migrate_market_snapshots(db: Storage) -> int:
    path = PROJECT_ROOT / "data" / "market_snapshot_akshare.csv"
    if not path.exists():
        path = PROJECT_ROOT / "data" / "market_snapshot.csv"
    if not path.exists():
        print("[跳过] market_snapshot CSV 不存在")
        return 0
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return 0
    from datetime import date
    today = date.today().isoformat()
    snapshots = []
    for row in rows:
        snapshots.append({
            "symbol": row.get("symbol", ""),
            "name": row.get("name", ""),
            "close": float(row.get("close", 0) or 0),
            "prev_close": float(row.get("prev_close", 0) or 0),
            "volume": float(row.get("volume", 0) or 0),
            "volume_ma5": float(row.get("volume_ma5", 0) or 0),
            "active_market_value_pct": float(row.get("active_market_value_pct", 0) or 0),
            "macd_dif": float(row.get("macd_dif", 0) or 0),
            "macd_dea": float(row.get("macd_dea", 0) or 0),
            "white_line": float(row.get("white_line", 0) or 0),
            "yellow_line": float(row.get("yellow_line", 0) or 0),
            "support_price": float(row.get("support_price", 0) or 0),
            "resistance_price": float(row.get("resistance_price", 0) or 0),
            "tags": row.get("tags", ""),
        })
    db.save_market_snapshots(today, snapshots)
    print(f"[市场快照] 导入 {len(snapshots)} 条（{today}）")
    return len(snapshots)


def main():
    print("=" * 50)
    print("SQLite 数据迁移工具")
    print("=" * 50)

    with Storage() as db:
        print("\n1. 迁移活跃市值历史...")
        migrate_active_market_values(db)

        print("\n2. 迁移每日候选股历史...")
        migrate_daily_candidates(db)

        print("\n3. 迁移复盘历史...")
        migrate_reviews(db)

        print("\n4. 迁移当前市场快照...")
        migrate_market_snapshots(db)

        print("\n" + "=" * 50)
        print("迁移完成！数据库摘要：")
        summary = db.get_summary()
        for table, count in summary.items():
            if isinstance(count, list):
                print(f"  {table}: {count}")
            else:
                print(f"  {table}: {count}")
        print(f"\n数据库路径: {db.db_path}")


if __name__ == "__main__":
    main()
