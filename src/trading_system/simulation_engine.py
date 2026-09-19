"""模拟交易引擎 —— 按照选股信号自动买卖，追踪账户净值。"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .storage import Storage
from .data_provider import AkShareProvider
from .data_normalizer import normalize_daily_frame
from .trade_calendar import last_trading_day, next_trading_day, is_trading_day
from .engines import RiskEngine
from .rulebook import Rulebook


PROJECT_ROOT = Path(r"g:\BaiduNetdiskDownload\z直播文字\trading-system-core")
CONFIG_PATH = PROJECT_ROOT / "config" / "rulebook.json"

# 交易费用
COMMISSION_RATE = 0.00025  # 万分之2.5
STAMP_TAX_RATE = 0.0005    # 千分之0.5（仅卖出）
SLIPPAGE_RATE = 0.001      # 千分之一滑点

# 每只票的固定买入金额（方案A：全买模式）
DEFAULT_POSITION_PER_STOCK = 10000.0


@dataclass
class BuyCandidate:
    symbol: str
    name: str
    score: float
    plan_action: str
    candidate_date: str


class SimulationEngine:
    """模拟交易引擎。

    方案A：全买模式 —— 所有"可纳入买入候选"的股票各买固定金额。
    买入价：选股日次日开盘价（含滑点）
    卖出价：信号触发日次日开盘价（含滑点）
    """

    def __init__(self, account_id: int, position_per_stock: float = DEFAULT_POSITION_PER_STOCK):
        self.account_id = account_id
        self.position_per_stock = position_per_stock
        self.provider = AkShareProvider(adjust="qfq", retries=2, pause_seconds=0.3)
        self.rulebook = Rulebook.load(CONFIG_PATH)
        self.risk_engine = RiskEngine(self.rulebook)
        self._daily_cache: Dict[str, pd.DataFrame] = {}

    # ─── 行情获取 ───

    def _get_daily(self, symbol: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        cache_key = f"{symbol}_{start_date}_{end_date}"
        if cache_key in self._daily_cache:
            return self._daily_cache[cache_key]
        try:
            raw = self.provider.fetch_daily(symbol, start_date, end_date)
            if raw is None or raw.empty:
                self._daily_cache[cache_key] = pd.DataFrame()
                return None
            df = normalize_daily_frame(raw)
            self._daily_cache[cache_key] = df
            return df
        except Exception:
            self._daily_cache[cache_key] = pd.DataFrame()
            return None

    def _get_next_day_open(self, symbol: str, ref_date: str) -> Optional[float]:
        """获取参考日期之后第一个交易日的开盘价。"""
        start = (pd.to_datetime(ref_date) - timedelta(days=1)).strftime("%Y%m%d")
        end = (pd.to_datetime(ref_date) + timedelta(days=10)).strftime("%Y%m%d")
        df = self._get_daily(symbol, start, end)
        if df is None or df.empty:
            return None
        ref = pd.to_datetime(ref_date)
        next_rows = df[df["date"] > ref]
        if next_rows.empty:
            return None
        return float(next_rows.iloc[0].get("open", next_rows.iloc[0]["close"]))

    def _get_price_on_date(self, symbol: str, target_date: str, price_type: str = "close") -> Optional[float]:
        """获取指定日期的价格（close/open/high/low）。"""
        start = (pd.to_datetime(target_date) - timedelta(days=5)).strftime("%Y%m%d")
        end = (pd.to_datetime(target_date) + timedelta(days=5)).strftime("%Y%m%d")
        df = self._get_daily(symbol, start, end)
        if df is None or df.empty:
            return None
        target = pd.to_datetime(target_date)
        row = df[df["date"] == target]
        if row.empty:
            return None
        return float(row.iloc[0].get(price_type, row.iloc[0]["close"]))

    def _calc_buy_cost(self, price: float, shares: int) -> Tuple[float, float]:
        """计算买入成本和佣金。"""
        amount = price * shares
        commission = max(amount * COMMISSION_RATE, 5.0)  # 最低5元
        total_cost = amount + commission
        return total_cost, commission

    def _calc_sell_revenue(self, price: float, shares: int) -> Tuple[float, float]:
        """计算卖出收入和费用。"""
        amount = price * shares
        commission = max(amount * COMMISSION_RATE, 5.0)
        stamp_tax = amount * STAMP_TAX_RATE
        total_fee = commission + stamp_tax
        revenue = amount - total_fee
        return revenue, total_fee

    def _calc_shares(self, price: float) -> int:
        """根据单价计算可买股数（向下取整到100股）。"""
        raw_shares = int(self.position_per_stock / price)
        return max(0, (raw_shares // 100) * 100)

    # ─── 买入逻辑 ───

    def process_buys(self, candidates: List[BuyCandidate]) -> List[Dict[str, Any]]:
        """处理一批候选股的买入。

        全买模式（验证用）：所有"观察等待确认"及以上级别的候选股都买入，
        用于验证选股系统的整体质量（选出来的票后续表现如何）。
        排除："无操作"、"不开新仓"。
        买入价：候选日次日开盘价 + 滑点。
        """
        skip_actions = {"无操作", "观察/不开新仓"}
        buy_candidates = [
            c for c in candidates
            if c.plan_action not in skip_actions and c.score >= 30
        ]
        results = []
        with Storage() as db:
            account = db.get_sim_account(self.account_id)
            if not account:
                raise ValueError(f"模拟账户 {self.account_id} 不存在")
            cash = float(account["current_cash"])

            # 排除已持仓的股票
            open_holdings = db.get_sim_open_holdings(self.account_id)
            held_symbols = {h["symbol"] for h in open_holdings}

            for cand in buy_candidates:
                if cand.symbol in held_symbols:
                    results.append({"symbol": cand.symbol, "name": cand.name, "status": "skipped", "reason": "已持仓"})
                    continue

                # 获取次日开盘价
                buy_price = self._get_next_day_open(cand.symbol, cand.candidate_date)
                if buy_price is None or buy_price <= 0:
                    results.append({"symbol": cand.symbol, "name": cand.name, "status": "failed", "reason": "获取开盘价失败"})
                    continue

                # 加滑点
                buy_price = round(buy_price * (1 + SLIPPAGE_RATE), 2)

                shares = self._calc_shares(buy_price)
                if shares <= 0:
                    results.append({"symbol": cand.symbol, "name": cand.name, "status": "failed", "reason": "资金不足"})
                    continue

                total_cost, commission = self._calc_buy_cost(buy_price, shares)
                if total_cost > cash:
                    results.append({"symbol": cand.symbol, "name": cand.name, "status": "failed", "reason": "现金不足"})
                    continue

                # 确定买入日期（候选日的下一个交易日）
                buy_date = next_trading_day(pd.to_datetime(cand.candidate_date).date()).isoformat()

                # 写入数据库
                holding_id = db.add_sim_holding(
                    account_id=self.account_id,
                    symbol=cand.symbol,
                    name=cand.name,
                    buy_date=buy_date,
                    buy_price=buy_price,
                    shares=shares,
                    commission=commission,
                )
                cash -= total_cost
                db.update_sim_account(self.account_id, current_cash=round(cash, 2))

                results.append({
                    "symbol": cand.symbol,
                    "name": cand.name,
                    "status": "bought",
                    "holding_id": holding_id,
                    "buy_date": buy_date,
                    "buy_price": buy_price,
                    "shares": shares,
                    "cost": round(total_cost, 2),
                })

        return results

    # ─── 卖出逻辑 ───

    def process_sells(self, as_of_date: Optional[str] = None) -> List[Dict[str, Any]]:
        """检查所有持仓的卖出信号并执行。

        卖出规则（按优先级）：
        1. 初始止损（-5%）
        2. 破黄线 → 清仓
        3. 滴滴战法（跌破前低）→ 白线之上减仓，白线之下清仓
        4. 破白线 → 减仓一半
        5. S1卖点 → 减仓观察
        6. MACD死叉 → 仅警示，不操作
        """
        if as_of_date is None:
            as_of_date = last_trading_day().isoformat()

        results = []
        with Storage() as db:
            account = db.get_sim_account(self.account_id)
            if not account:
                return results
            cash = float(account["current_cash"])

            holdings = db.get_sim_open_holdings(self.account_id)
            for h in holdings:
                symbol = h["symbol"]
                # 获取最近30天行情（用于风控判断）
                start = (pd.to_datetime(as_of_date) - timedelta(days=60)).strftime("%Y%m%d")
                end = (pd.to_datetime(as_of_date) + timedelta(days=1)).strftime("%Y%m%d")
                df = self._get_daily(symbol, start, end)
                if df is None or df.empty or len(df) < 10:
                    results.append({"symbol": symbol, "name": h["name"], "status": "skipped", "reason": "行情不足"})
                    continue

                # 找到 as_of_date 对应的行
                target = pd.to_datetime(as_of_date)
                day_rows = df[df["date"] <= target]
                if day_rows.empty:
                    results.append({"symbol": symbol, "name": h["name"], "status": "skipped", "reason": "无当日数据"})
                    continue

                latest = day_rows.iloc[-1]
                close_price = float(latest["close"])
                white_line = float(
                    day_rows["close"].ewm(span=10, adjust=False).mean()
                    .ewm(span=10, adjust=False).mean().iloc[-1]
                )
                yellow_line = float((
                    day_rows["close"].tail(14).mean() + day_rows["close"].tail(28).mean()
                    + day_rows["close"].tail(57).mean() + day_rows["close"].tail(114).mean()
                ) / 4) if len(day_rows) >= 114 else float(day_rows["close"].tail(20).mean())

                support = float(day_rows.tail(20)["low"].min())
                resistance = float(day_rows.tail(20)["high"].max())

                # 计算 prev_low（滴滴战法用）
                prev_low = float(day_rows.iloc[-2]["low"]) if len(day_rows) >= 2 else 0.0

                # 构造一个简化的快照用于风控判断
                from .models import SecuritySnapshot
                snapshot = SecuritySnapshot(
                    symbol=symbol,
                    name=h["name"],
                    close=close_price,
                    prev_close=float(day_rows.iloc[-2]["close"]) if len(day_rows) >= 2 else close_price,
                    prev_low=prev_low,
                    volume=float(latest.get("volume", 0)),
                    volume_ma5=float(day_rows.tail(5)["volume"].mean()),
                    active_market_value_pct=0.0,
                    macd_dif=0.0,
                    macd_dea=0.0,
                    white_line=white_line,
                    yellow_line=yellow_line,
                    support_price=support,
                    resistance_price=resistance,
                    tags=[],
                )

                # 风控判断
                risk_actions = self.risk_engine.evaluate(snapshot)
                if not risk_actions:
                    # 无卖出信号，更新持仓价格
                    db.update_sim_holding_price(h["id"], close_price)
                    results.append({"symbol": symbol, "name": h["name"], "status": "hold", "price": close_price})
                    continue

                top_action = risk_actions[0]
                action_str = top_action.action
                sell_reason = top_action.reason

                # 判断是否需要卖出
                need_sell = False
                sell_ratio = 1.0  # 默认全卖

                if "清仓" in action_str or "止损" in action_str or "退出" in action_str or "回避" in action_str:
                    need_sell = True
                    sell_ratio = 1.0
                elif "减仓" in action_str and "半" in action_str:
                    need_sell = True
                    sell_ratio = 0.5
                elif "减仓" in action_str:
                    need_sell = True
                    sell_ratio = 0.5
                elif "警示" in action_str or "观察" in action_str:
                    need_sell = False  # 仅警示，不操作

                if not need_sell:
                    db.update_sim_holding_price(h["id"], close_price)
                    results.append({
                        "symbol": symbol, "name": h["name"], "status": "warn",
                        "price": close_price, "reason": sell_reason,
                    })
                    continue

                # 确定卖出数量
                sell_shares = int(h["shares"] * sell_ratio)
                if sell_ratio < 1.0:
                    sell_shares = max(100, (sell_shares // 100) * 100)
                sell_shares = min(sell_shares, h["shares"])
                if sell_shares <= 0:
                    db.update_sim_holding_price(h["id"], close_price)
                    results.append({"symbol": symbol, "name": h["name"], "status": "hold", "price": close_price})
                    continue

                # 获取次日开盘价作为卖出价
                sell_price = self._get_next_day_open(symbol, as_of_date)
                if sell_price is None or sell_price <= 0:
                    sell_price = close_price  # 回退用收盘价

                # 减滑点
                sell_price = round(sell_price * (1 - SLIPPAGE_RATE), 2)

                sell_date = next_trading_day(pd.to_datetime(as_of_date).date()).isoformat()

                revenue, fee = self._calc_sell_revenue(sell_price, sell_shares)

                # 如果是部分减仓，先更新持仓股数，再记录卖出
                if sell_shares < h["shares"]:
                    # 部分减仓：直接更新数据库
                    # 先记录交易
                    cost_basis = h["cost_price"] * sell_shares
                    pnl = revenue - cost_basis
                    pnl_pct = (pnl / cost_basis * 100) if cost_basis else 0.0
                    db.conn.execute(
                        "INSERT INTO sim_transactions "
                        "(account_id,holding_id,symbol,name,type,date,price,shares,amount,commission,pnl,pnl_pct,reason) "
                        "VALUES (?,?,?,?, 'sell', ?, ?, ?, ?, ?, ?, ?, ?)",
                        (self.account_id, h["id"], symbol, h["name"],
                         sell_date, sell_price, sell_shares, sell_price * sell_shares,
                         fee, round(pnl, 2), round(pnl_pct, 2),
                         f"减仓：{sell_reason}"),
                    )
                    remaining = h["shares"] - sell_shares
                    db.conn.execute(
                        "UPDATE sim_holdings SET shares=?, current_price=? WHERE id=?",
                        (remaining, sell_price, h["id"]),
                    )
                    db.conn.commit()
                    cash += revenue
                    db.update_sim_account(self.account_id, current_cash=round(cash, 2))

                    results.append({
                        "symbol": symbol, "name": h["name"], "status": "partial_sell",
                        "sell_date": sell_date, "sell_price": sell_price,
                        "shares": sell_shares, "remaining": remaining,
                        "pnl": round(pnl, 2), "reason": sell_reason,
                    })
                else:
                    # 清仓
                    result = db.close_sim_holding(
                        holding_id=h["id"],
                        sell_date=sell_date,
                        sell_price=sell_price,
                        commission=fee,
                        reason=sell_reason,
                    )
                    cash += revenue
                    db.update_sim_account(self.account_id, current_cash=round(cash, 2))

                    results.append({
                        "symbol": symbol, "name": h["name"], "status": "sold",
                        "sell_date": sell_date, "sell_price": sell_price,
                        "shares": sell_shares,
                        "pnl": result["pnl"], "pnl_pct": result["pnl_pct"],
                        "reason": sell_reason,
                    })

        return results

    # ─── 净值更新 ───

    def update_nav(self, as_of_date: Optional[str] = None) -> Dict[str, Any]:
        """更新指定日期的账户净值。"""
        if as_of_date is None:
            as_of_date = last_trading_day().isoformat()

        with Storage() as db:
            account = db.get_sim_account(self.account_id)
            if not account:
                return {}

            cash = float(account["current_cash"])
            holdings = db.get_sim_open_holdings(self.account_id)
            market_value = 0.0

            for h in holdings:
                price = self._get_price_on_date(h["symbol"], as_of_date, "close")
                if price is None:
                    price = float(h.get("current_price") or h["buy_price"])
                mv = price * h["shares"]
                market_value += mv
                db.update_sim_holding_price(h["id"], price)

            total_value = cash + market_value
            holding_count = len(holdings)

            # 计算收益率
            latest_nav = db.get_latest_sim_nav(self.account_id)
            if latest_nav and latest_nav["date"] < as_of_date:
                prev_nav = latest_nav["nav"]
                daily_return = ((total_value / prev_nav) - 1) * 100 if prev_nav else 0.0
            else:
                daily_return = 0.0

            total_return = ((total_value / account["initial_capital"]) - 1) * 100

            # 保存净值
            db.save_sim_nav(
                account_id=self.account_id,
                nav_date=as_of_date,
                nav=round(total_value, 2),
                cash=round(cash, 2),
                market_value=round(market_value, 2),
                daily_return_pct=round(daily_return, 2),
                total_return_pct=round(total_return, 2),
                holding_count=holding_count,
            )

            # 更新账户总值
            db.update_sim_account(self.account_id, total_value=round(total_value, 2))

            return {
                "date": as_of_date,
                "nav": round(total_value, 2),
                "cash": round(cash, 2),
                "market_value": round(market_value, 2),
                "daily_return_pct": round(daily_return, 2),
                "total_return_pct": round(total_return, 2),
                "holding_count": holding_count,
            }

    # ─── 绩效计算 ───

    def calculate_performance(self) -> Dict[str, Any]:
        """计算账户绩效指标。"""
        with Storage() as db:
            account = db.get_sim_account(self.account_id)
            if not account:
                return {}

            nav_history = db.get_sim_nav_history(self.account_id, limit=1000)
            if not nav_history:
                return {"initial_capital": account["initial_capital"], "total_days": 0}

            navs = [n["nav"] for n in nav_history]
            initial = float(account["initial_capital"])
            current = navs[-1]
            total_return = (current / initial - 1) * 100

            # 交易日数
            total_days = len(nav_history)

            # 年化收益率（按250个交易日）
            annualized = ((current / initial) ** (250 / max(total_days, 1)) - 1) * 100 if total_days > 0 else 0.0

            # 日收益率
            daily_returns = []
            for i in range(1, len(navs)):
                if navs[i - 1] > 0:
                    daily_returns.append((navs[i] / navs[i - 1] - 1) * 100)

            # 最大回撤
            peak = navs[0]
            max_drawdown = 0.0
            for nav in navs:
                if nav > peak:
                    peak = nav
                dd = (peak - nav) / peak * 100
                if dd > max_drawdown:
                    max_drawdown = dd

            # 夏普比率（无风险利率取2%年化，日度=2%/250）
            if daily_returns and len(daily_returns) > 1:
                avg_daily = sum(daily_returns) / len(daily_returns)
                import statistics
                std_daily = statistics.stdev(daily_returns) if len(daily_returns) > 1 else 0.0
                risk_free_daily = 2.0 / 250
                sharpe = ((avg_daily - risk_free_daily) / std_daily * math.sqrt(250)) if std_daily > 0 else 0.0
            else:
                sharpe = 0.0

            # 胜率（已平仓交易）
            closed = [h for h in db.get_sim_all_holdings(self.account_id, limit=500) if h["status"] == "closed"]
            win_count = sum(1 for h in closed if h["pnl"] > 0)
            loss_count = sum(1 for h in closed if h["pnl"] < 0)
            win_rate = (win_count / len(closed) * 100) if closed else 0.0

            # 盈亏比
            avg_win = (sum(h["pnl"] for h in closed if h["pnl"] > 0) / win_count) if win_count else 0.0
            avg_loss = (sum(h["pnl"] for h in closed if h["pnl"] < 0) / loss_count) if loss_count else 0.0
            profit_factor = abs(avg_win / avg_loss) if avg_loss != 0 else 0.0

            # 平均持仓天数
            if closed:
                holding_days_list = []
                for h in closed:
                    if h["buy_date"] and h["sell_date"]:
                        days = (pd.to_datetime(h["sell_date"]) - pd.to_datetime(h["buy_date"])).days
                        holding_days_list.append(days)
                avg_holding_days = round(sum(holding_days_list) / len(holding_days_list), 1) if holding_days_list else 0
            else:
                avg_holding_days = 0

            return {
                "initial_capital": initial,
                "current_nav": current,
                "total_return_pct": round(total_return, 2),
                "annualized_return_pct": round(annualized, 2),
                "max_drawdown_pct": round(max_drawdown, 2),
                "sharpe_ratio": round(sharpe, 2),
                "total_days": total_days,
                "total_trades": len(closed),
                "win_count": win_count,
                "loss_count": loss_count,
                "win_rate_pct": round(win_rate, 2),
                "avg_win": round(avg_win, 2),
                "avg_loss": round(avg_loss, 2),
                "profit_factor": round(profit_factor, 2),
                "avg_holding_days": avg_holding_days,
            }
