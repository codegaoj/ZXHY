from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


PROJECT_ROOT = Path(r"g:\BaiduNetdiskDownload\z直播文字\trading-system-core")
DB_PATH = PROJECT_ROOT / "data" / "trading_system.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS market_snapshots (
    date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT,
    close REAL,
    prev_close REAL,
    volume REAL,
    volume_ma5 REAL,
    active_market_value_pct REAL,
    macd_dif REAL,
    macd_dea REAL,
    white_line REAL,
    yellow_line REAL,
    support_price REAL,
    resistance_price REAL,
    tags TEXT,
    PRIMARY KEY (date, symbol)
);

CREATE TABLE IF NOT EXISTS active_market_values (
    date TEXT NOT NULL PRIMARY KEY,
    value REAL,
    pct REAL,
    source TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS market_states (
    date TEXT NOT NULL PRIMARY KEY,
    regime TEXT,
    active_market_value_pct REAL,
    macd_above_zero INTEGER,
    reason TEXT,
    max_position_pct INTEGER,
    single_symbol_pct INTEGER,
    manual_override INTEGER
);

CREATE TABLE IF NOT EXISTS daily_candidates (
    date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT,
    close REAL,
    pct_change REAL,
    spot_pct_change REAL,
    amount REAL,
    volume_ratio REAL,
    white_line REAL,
    yellow_line REAL,
    support_price REAL,
    resistance_price REAL,
    macd_dif REAL,
    macd_dea REAL,
    tags TEXT,
    chart_quality REAL,
    chart_grade TEXT,
    plan_action TEXT,
    suggested_position_pct REAL,
    plan_score REAL,
    candidate_score REAL,
    candidate_reason TEXT,
    risk TEXT,
    signals TEXT,
    sector TEXT,
    sector_strength TEXT,
    sector_change REAL,
    PRIMARY KEY (date, symbol)
);

CREATE TABLE IF NOT EXISTS candidate_reviews (
    selected_date TEXT NOT NULL,
    review_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT,
    selected_close REAL,
    selected_pct REAL,
    today_open REAL,
    today_high REAL,
    today_low REAL,
    today_close REAL,
    today_pct REAL,
    today_amount REAL,
    change_from_selected REAL,
    intraday_pos REAL,
    white_line REAL,
    yellow_line REAL,
    support REAL,
    resistance REAL,
    broke_white INTEGER,
    broke_yellow INTEGER,
    distance_to_resistance REAL,
    distance_to_support REAL,
    performance_category TEXT,
    tags TEXT,
    plan_action TEXT,
    candidate_score REAL,
    PRIMARY KEY (selected_date, symbol)
);

CREATE TABLE IF NOT EXISTS review_stats (
    review_date TEXT NOT NULL,
    selected_date TEXT NOT NULL,
    total INTEGER,
    valid INTEGER,
    avg_change REAL,
    up_count INTEGER,
    down_count INTEGER,
    flat_count INTEGER,
    broke_white_count INTEGER,
    broke_yellow_count INTEGER,
    distribution TEXT,
    PRIMARY KEY (selected_date)
);

CREATE TABLE IF NOT EXISTS agent_reviews (
    date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    verdict TEXT,
    score REAL,
    issues TEXT,
    recommendation TEXT,
    PRIMARY KEY (date, symbol)
);

CREATE TABLE IF NOT EXISTS trade_plans (
    date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT,
    market_regime TEXT,
    action TEXT,
    suggested_position_pct REAL,
    score REAL,
    entry_signals TEXT,
    risk_actions TEXT,
    notes TEXT,
    PRIMARY KEY (date, symbol)
);

CREATE TABLE IF NOT EXISTS holdings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    name TEXT,
    buy_date TEXT NOT NULL,
    buy_price REAL NOT NULL,
    shares INTEGER NOT NULL,
    cost_price REAL,
    commission REAL DEFAULT 0,
    status TEXT DEFAULT 'open',
    note TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    holding_id INTEGER,
    symbol TEXT NOT NULL,
    name TEXT,
    type TEXT NOT NULL,
    date TEXT NOT NULL,
    price REAL NOT NULL,
    shares INTEGER NOT NULL,
    amount REAL,
    commission REAL DEFAULT 0,
    pnl REAL,
    pnl_pct REAL,
    note TEXT,
    FOREIGN KEY (holding_id) REFERENCES holdings(id)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_symbol ON market_snapshots(symbol);
CREATE INDEX IF NOT EXISTS idx_candidates_date ON daily_candidates(date);
CREATE INDEX IF NOT EXISTS idx_reviews_symbol ON candidate_reviews(symbol);
CREATE INDEX IF NOT EXISTS idx_holdings_symbol ON holdings(symbol);
CREATE INDEX IF NOT EXISTS idx_holdings_status ON holdings(status);
CREATE INDEX IF NOT EXISTS idx_transactions_symbol ON transactions(symbol);
"""


class Storage:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # ─── market_snapshots ───

    def save_market_snapshots(self, snap_date: str, snapshots: Sequence[Dict[str, Any]]) -> int:
        rows = []
        for s in snapshots:
            rows.append((
                snap_date,
                s["symbol"],
                s.get("name"),
                s.get("close"),
                s.get("prev_close"),
                s.get("volume"),
                s.get("volume_ma5"),
                s.get("active_market_value_pct"),
                s.get("macd_dif"),
                s.get("macd_dea"),
                s.get("white_line"),
                s.get("yellow_line"),
                s.get("support_price"),
                s.get("resistance_price"),
                s.get("tags"),
            ))
        self.conn.executemany(
            "INSERT OR REPLACE INTO market_snapshots "
            "(date,symbol,name,close,prev_close,volume,volume_ma5,"
            "active_market_value_pct,macd_dif,macd_dea,white_line,yellow_line,"
            "support_price,resistance_price,tags) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def query_market_snapshots(self, snap_date: str) -> List[dict]:
        cur = self.conn.execute(
            "SELECT * FROM market_snapshots WHERE date=? ORDER BY symbol", (snap_date,)
        )
        return [dict(r) for r in cur.fetchall()]

    def get_snapshot_dates(self, limit: int = 30) -> List[str]:
        cur = self.conn.execute(
            "SELECT DISTINCT date FROM market_snapshots ORDER BY date DESC LIMIT ?", (limit,)
        )
        return [r[0] for r in cur.fetchall()]

    # ─── active_market_values ───

    def save_active_market_value(
        self, amv_date: str, value: float, pct: float, source: str = "compass", note: str = ""
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO active_market_values (date,value,pct,source,note) "
            "VALUES (?,?,?,?,?)",
            (amv_date, value, pct, source, note),
        )
        self.conn.commit()

    def get_latest_active_market_value(self) -> Optional[dict]:
        cur = self.conn.execute(
            "SELECT * FROM active_market_values ORDER BY date DESC LIMIT 1"
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def get_recent_active_market_values(self, days: int = 3) -> List[float]:
        cur = self.conn.execute(
            "SELECT pct FROM active_market_values ORDER BY date DESC LIMIT ?", (days,)
        )
        return [r[0] for r in cur.fetchall()]

    def get_all_active_market_values(self, limit: int = 365) -> List[dict]:
        cur = self.conn.execute(
            "SELECT * FROM active_market_values ORDER BY date DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in cur.fetchall()]

    # ─── market_states ───

    def save_market_state(
        self,
        state_date: str,
        regime: str,
        active_market_value_pct: float,
        macd_above_zero: bool,
        reason: str,
        max_position_pct: int,
        single_symbol_pct: int,
        manual_override: bool = False,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO market_states "
            "(date,regime,active_market_value_pct,macd_above_zero,reason,"
            "max_position_pct,single_symbol_pct,manual_override) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                state_date,
                regime,
                active_market_value_pct,
                int(macd_above_zero),
                reason,
                max_position_pct,
                single_symbol_pct,
                int(manual_override),
            ),
        )
        self.conn.commit()

    def get_market_state(self, state_date: str) -> Optional[dict]:
        cur = self.conn.execute(
            "SELECT * FROM market_states WHERE date=?", (state_date,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def get_latest_market_state(self) -> Optional[dict]:
        cur = self.conn.execute(
            "SELECT * FROM market_states ORDER BY date DESC LIMIT 1"
        )
        row = cur.fetchone()
        return dict(row) if row else None

    # ─── daily_candidates ───

    def save_daily_candidates(self, cand_date: str, candidates: Sequence[Dict[str, Any]]) -> int:
        rows = []
        for c in candidates:
            rows.append((
                cand_date,
                c["symbol"],
                c.get("name"),
                c.get("close"),
                c.get("pct_change"),
                c.get("spot_pct_change"),
                c.get("amount"),
                c.get("volume_ratio"),
                c.get("white_line"),
                c.get("yellow_line"),
                c.get("support_price"),
                c.get("resistance_price"),
                c.get("macd_dif"),
                c.get("macd_dea"),
                c.get("tags"),
                c.get("chart_quality"),
                c.get("chart_grade"),
                c.get("plan_action"),
                c.get("suggested_position_pct"),
                c.get("plan_score"),
                c.get("candidate_score"),
                c.get("candidate_reason"),
                c.get("risk"),
                c.get("signals"),
                c.get("sector"),
                c.get("sector_strength"),
                c.get("sector_change"),
            ))
        self.conn.executemany(
            "INSERT OR REPLACE INTO daily_candidates "
            "(date,symbol,name,close,pct_change,spot_pct_change,amount,volume_ratio,"
            "white_line,yellow_line,support_price,resistance_price,macd_dif,macd_dea,"
            "tags,chart_quality,chart_grade,plan_action,suggested_position_pct,"
            "plan_score,candidate_score,candidate_reason,risk,signals,sector,"
            "sector_strength,sector_change) "
            "VALUES (" + ",".join(["?"] * 27) + ")",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def query_daily_candidates(self, cand_date: str) -> List[dict]:
        cur = self.conn.execute(
            "SELECT * FROM daily_candidates WHERE date=? ORDER BY candidate_score DESC",
            (cand_date,),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_candidate_dates(self, limit: int = 30) -> List[str]:
        cur = self.conn.execute(
            "SELECT DISTINCT date FROM daily_candidates ORDER BY date DESC LIMIT ?", (limit,)
        )
        return [r[0] for r in cur.fetchall()]

    # ─── candidate_reviews ───

    def save_candidate_reviews(
        self,
        selected_date: str,
        review_date: str,
        rows_data: Sequence[Dict[str, Any]],
    ) -> int:
        rows = []
        for r in rows_data:
            rows.append((
                selected_date,
                review_date,
                r["symbol"],
                r.get("name"),
                r.get("selected_close"),
                r.get("selected_pct"),
                r.get("today_open"),
                r.get("today_high"),
                r.get("today_low"),
                r.get("today_close"),
                r.get("today_pct"),
                r.get("today_amount"),
                r.get("change_from_selected"),
                r.get("intraday_pos"),
                r.get("white_line"),
                r.get("yellow_line"),
                r.get("support"),
                r.get("resistance"),
                int(r.get("broke_white", False)),
                int(r.get("broke_yellow", False)),
                r.get("distance_to_resistance"),
                r.get("distance_to_support"),
                r.get("performance_category"),
                r.get("tags"),
                r.get("plan_action"),
                r.get("candidate_score"),
            ))
        self.conn.executemany(
            "INSERT OR REPLACE INTO candidate_reviews "
            "(selected_date,review_date,symbol,name,selected_close,selected_pct,"
            "today_open,today_high,today_low,today_close,today_pct,today_amount,"
            "change_from_selected,intraday_pos,white_line,yellow_line,support,"
            "resistance,broke_white,broke_yellow,distance_to_resistance,"
            "distance_to_support,performance_category,tags,plan_action,candidate_score) "
            "VALUES (" + ",".join(["?"] * 26) + ")",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def save_review_stats(
        self,
        review_date: str,
        selected_date: str,
        stats: Dict[str, Any],
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO review_stats "
            "(review_date,selected_date,total,valid,avg_change,up_count,down_count,"
            "flat_count,broke_white_count,broke_yellow_count,distribution) "
            "VALUES (" + ",".join(["?"] * 11) + ")",
            (
                review_date,
                selected_date,
                stats.get("total"),
                stats.get("valid"),
                stats.get("avg_change"),
                stats.get("up_count"),
                stats.get("down_count"),
                stats.get("flat_count"),
                stats.get("broke_white_count"),
                stats.get("broke_yellow_count"),
                json.dumps(stats.get("distribution", {}), ensure_ascii=False),
            ),
        )
        self.conn.commit()

    def query_candidate_reviews(self, selected_date: str) -> List[dict]:
        cur = self.conn.execute(
            "SELECT * FROM candidate_reviews WHERE selected_date=? ORDER BY change_from_selected DESC",
            (selected_date,),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_review_dates(self, limit: int = 30) -> List[str]:
        cur = self.conn.execute(
            "SELECT DISTINCT selected_date FROM candidate_reviews ORDER BY selected_date DESC LIMIT ?",
            (limit,),
        )
        return [r[0] for r in cur.fetchall()]

    def get_review_stats(self, selected_date: str) -> Optional[dict]:
        cur = self.conn.execute(
            "SELECT * FROM review_stats WHERE selected_date=?", (selected_date,)
        )
        row = cur.fetchone()
        if not row:
            return None
        result = dict(row)
        if result.get("distribution"):
            result["distribution"] = json.loads(result["distribution"])
        return result

    # ─── agent_reviews ───

    def save_agent_reviews(self, review_date: str, reviews: Sequence[Dict[str, Any]]) -> int:
        rows = []
        for r in reviews:
            rows.append((
                review_date,
                r.get("symbol"),
                r.get("verdict"),
                r.get("score"),
                json.dumps(r.get("issues", []), ensure_ascii=False) if isinstance(r.get("issues"), list) else r.get("issues"),
                r.get("recommendation"),
            ))
        self.conn.executemany(
            "INSERT OR REPLACE INTO agent_reviews "
            "(date,symbol,verdict,score,issues,recommendation) "
            "VALUES (?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def query_agent_reviews(self, review_date: str) -> List[dict]:
        cur = self.conn.execute(
            "SELECT * FROM agent_reviews WHERE date=? ORDER BY symbol", (review_date,)
        )
        results = []
        for r in cur.fetchall():
            d = dict(r)
            if d.get("issues"):
                try:
                    d["issues"] = json.loads(d["issues"])
                except (json.JSONDecodeError, TypeError):
                    pass
            results.append(d)
        return results

    # ─── trade_plans ───

    def save_trade_plans(self, plan_date: str, plans: Sequence[Dict[str, Any]]) -> int:
        rows = []
        for p in plans:
            rows.append((
                plan_date,
                p["symbol"],
                p.get("name"),
                p.get("market_regime"),
                p.get("action"),
                p.get("suggested_position_pct"),
                p.get("score"),
                json.dumps(p.get("entry_signals", []), ensure_ascii=False),
                json.dumps(p.get("risk_actions", []), ensure_ascii=False),
                json.dumps(p.get("notes", []), ensure_ascii=False),
            ))
        self.conn.executemany(
            "INSERT OR REPLACE INTO trade_plans "
            "(date,symbol,name,market_regime,action,suggested_position_pct,score,"
            "entry_signals,risk_actions,notes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def query_trade_plans(self, plan_date: str) -> List[dict]:
        cur = self.conn.execute(
            "SELECT * FROM trade_plans WHERE date=? ORDER BY symbol", (plan_date,)
        )
        results = []
        for r in cur.fetchall():
            d = dict(r)
            for key in ("entry_signals", "risk_actions", "notes"):
                if d.get(key):
                    try:
                        d[key] = json.loads(d[key])
                    except (json.JSONDecodeError, TypeError):
                        pass
            results.append(d)
        return results

    # ─── summary / history ───

    def get_summary(self) -> dict:
        tables = [
            "market_snapshots",
            "active_market_values",
            "market_states",
            "daily_candidates",
            "candidate_reviews",
            "review_stats",
            "agent_reviews",
            "trade_plans",
            "holdings",
            "transactions",
        ]
        result = {}
        for table in tables:
            cur = self.conn.execute(f"SELECT COUNT(*) FROM {table}")
            result[table] = cur.fetchone()[0]
        result["snapshot_dates"] = self.get_snapshot_dates(5)
        result["candidate_dates"] = self.get_candidate_dates(5)
        result["review_dates"] = self.get_review_dates(5)
        return result

    def get_candidate_history(self, symbol: str, limit: int = 30) -> List[dict]:
        cur = self.conn.execute(
            "SELECT * FROM daily_candidates WHERE symbol=? ORDER BY date DESC LIMIT ?",
            (symbol, limit),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_review_history(self, symbol: str, limit: int = 30) -> List[dict]:
        cur = self.conn.execute(
            "SELECT * FROM candidate_reviews WHERE symbol=? ORDER BY selected_date DESC LIMIT ?",
            (symbol, limit),
        )
        return [dict(r) for r in cur.fetchall()]

    # ─── holdings ───

    def add_holding(
        self,
        symbol: str,
        name: str,
        buy_date: str,
        buy_price: float,
        shares: int,
        commission: float = 0.0,
        note: str = "",
    ) -> int:
        cost_price = buy_price + (commission / shares if shares else 0)
        cur = self.conn.execute(
            "INSERT INTO holdings "
            "(symbol,name,buy_date,buy_price,shares,cost_price,commission,status,note) "
            "VALUES (?,?,?,?,?,?,?, 'open', ?)",
            (symbol, name, buy_date, buy_price, shares, cost_price, commission, note),
        )
        holding_id = cur.lastrowid
        self.conn.execute(
            "INSERT INTO transactions (holding_id,symbol,name,type,date,price,shares,amount,commission,note) "
            "VALUES (?,?,?, 'buy', ?, ?, ?, ?, ?, ?)",
            (holding_id, symbol, name, buy_date, buy_price, shares,
             buy_price * shares, commission, note or "买入"),
        )
        self.conn.commit()
        return holding_id

    def update_holding(self, holding_id: int, **fields) -> None:
        allowed = {"symbol", "name", "buy_date", "buy_price", "shares",
                    "cost_price", "commission", "status", "note"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        sets = ", ".join(f"{k}=?" for k in updates)
        vals = list(updates.values()) + [holding_id]
        self.conn.execute(
            f"UPDATE holdings SET {sets}, updated_at=datetime('now','localtime') WHERE id=?",
            vals,
        )
        self.conn.commit()

    def delete_holding(self, holding_id: int) -> None:
        self.conn.execute("DELETE FROM transactions WHERE holding_id=?", (holding_id,))
        self.conn.execute("DELETE FROM holdings WHERE id=?", (holding_id,))
        self.conn.commit()

    def get_open_holdings(self) -> List[dict]:
        cur = self.conn.execute(
            "SELECT * FROM holdings WHERE status='open' ORDER BY buy_date DESC"
        )
        return [dict(r) for r in cur.fetchall()]

    def get_all_holdings(self, limit: int = 100) -> List[dict]:
        cur = self.conn.execute(
            "SELECT * FROM holdings ORDER BY buy_date DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in cur.fetchall()]

    def get_holding(self, holding_id: int) -> Optional[dict]:
        cur = self.conn.execute(
            "SELECT * FROM holdings WHERE id=?", (holding_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def close_holding(
        self,
        holding_id: int,
        sell_date: str,
        sell_price: float,
        sell_shares: int,
        commission: float = 0.0,
        note: str = "",
    ) -> dict:
        holding = self.get_holding(holding_id)
        if not holding:
            raise ValueError(f"持仓 ID {holding_id} 不存在")
        symbol = holding["symbol"]
        name = holding["name"] or ""
        buy_price = holding["buy_price"]
        cost = buy_price * sell_shares + (holding.get("commission", 0) or 0) * (sell_shares / max(holding["shares"], 1))
        revenue = sell_price * sell_shares - commission
        pnl = revenue - cost
        pnl_pct = (pnl / cost * 100) if cost else 0.0
        self.conn.execute(
            "INSERT INTO transactions "
            "(holding_id,symbol,name,type,date,price,shares,amount,commission,pnl,pnl_pct,note) "
            "VALUES (?,?,?, 'sell', ?, ?, ?, ?, ?, ?, ?, ?)",
            (holding_id, symbol, name, sell_date, sell_price, sell_shares,
             sell_price * sell_shares, commission, pnl, pnl_pct, note or "卖出"),
        )
        remaining = holding["shares"] - sell_shares
        if remaining <= 0:
            self.update_holding(holding_id, status="closed")
        else:
            self.update_holding(holding_id, shares=remaining)
        self.conn.commit()
        return {"pnl": pnl, "pnl_pct": pnl_pct, "remaining_shares": max(remaining, 0)}

    def import_holdings_csv(self, rows: List[Dict[str, Any]]) -> int:
        count = 0
        for r in rows:
            try:
                self.add_holding(
                    symbol=str(r.get("symbol", "")).strip().zfill(6),
                    name=r.get("name", "").strip(),
                    buy_date=r.get("buy_date", "").strip(),
                    buy_price=float(r.get("buy_price", 0)),
                    shares=int(r.get("shares", 0)),
                    commission=float(r.get("commission", 0) or 0),
                    note=r.get("note", "").strip(),
                )
                count += 1
            except Exception:
                continue
        return count

    # ─── transactions ───

    def get_transactions(self, symbol: str = "", limit: int = 100) -> List[dict]:
        if symbol:
            cur = self.conn.execute(
                "SELECT * FROM transactions WHERE symbol=? ORDER BY date DESC LIMIT ?",
                (symbol, limit),
            )
        else:
            cur = self.conn.execute(
                "SELECT * FROM transactions ORDER BY date DESC LIMIT ?", (limit,)
            )
        return [dict(r) for r in cur.fetchall()]
