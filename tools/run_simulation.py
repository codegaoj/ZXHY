"""模拟交易运行工具。

用法：
  python tools/run_simulation.py --init                      # 初始化模拟账户
  python tools/run_simulation.py --run                       # 运行今日模拟交易（检查卖出 + 处理新买入 + 更新净值）
  python tools/run_simulation.py --run --date 2026-09-17     # 指定日期运行
  python tools/run_simulation.py --report                    # 生成模拟交易报告
  python tools/run_simulation.py --list                      # 列出所有模拟账户
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

# 确保能 import 到项目模块
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import pandas as pd

from trading_system.storage import Storage
from trading_system.simulation_engine import SimulationEngine, BuyCandidate
from trading_system.trade_calendar import last_trading_day
from trading_system.reporting import generate_sim_report_html


OUTPUTS_DIR = PROJECT_ROOT / "outputs"
DEFAULT_ACCOUNT_NAME = "验证账户-全买模式"
DEFAULT_CAPITAL = 200000.0  # 20万初始资金
POSITION_PER_STOCK = 10000.0  # 每只票1万元


def init_account(name: str = DEFAULT_ACCOUNT_NAME, capital: float = DEFAULT_CAPITAL) -> dict:
    """初始化模拟账户。"""
    with Storage() as db:
        existing = db.get_sim_account_by_name(name)
        if existing:
            print(f"[模拟交易] 账户已存在：{name} (ID: {existing['id']})")
            return existing

        start_date = last_trading_day().isoformat()
        account_id = db.create_sim_account(
            name=name,
            initial_capital=capital,
            strategy="full_buy",
            start_date=start_date,
        )
        account = db.get_sim_account(account_id)
        print(f"[模拟交易] 账户创建成功：{name} (ID: {account_id})")
        print(f"  初始资金：{capital:,.2f} 元")
        print(f"  起始日期：{start_date}")
        print(f"  策略：全买模式（每只票 {POSITION_PER_STOCK:,.0f} 元）")
        return account


def load_candidates(candidate_date: str) -> list[BuyCandidate]:
    """从历史选股记录中加载指定日期的候选股。"""
    json_path = OUTPUTS_DIR / "history" / f"daily_candidates_{candidate_date}.json"
    if not json_path.exists():
        print(f"[模拟交易] 找不到 {candidate_date} 的选股记录：{json_path}")
        return []

    data = json.loads(json_path.read_text(encoding="utf-8"))
    candidates = []
    for row in data.get("candidates", []):
        candidates.append(BuyCandidate(
            symbol=str(row.get("symbol", "")).strip(),
            name=str(row.get("name", "")).strip(),
            score=float(row.get("candidate_score", 0)),
            plan_action=str(row.get("plan_action", "")),
            candidate_date=candidate_date,
        ))
    return candidates


def run_simulation(run_date: str = "") -> dict:
    """运行一次模拟交易。

    流程：
    1. 获取账户
    2. 处理卖出（检查所有持仓的风控信号）
    3. 处理买入（从当日选股结果中筛选买入候选）
    4. 更新净值
    5. 生成报告
    """
    if not run_date:
        run_date = last_trading_day().isoformat()

    # 获取账户
    with Storage() as db:
        account = db.get_sim_account_by_name(DEFAULT_ACCOUNT_NAME)
        if not account:
            print("[模拟交易] 账户不存在，先初始化...")
            account = init_account()

    account_id = account["id"]
    engine = SimulationEngine(account_id=account_id, position_per_stock=POSITION_PER_STOCK)

    print(f"\n[模拟交易] 运行日期：{run_date}")
    print(f"[模拟交易] 账户：{account['name']} (ID: {account_id})")

    # 1. 处理卖出
    print("\n[模拟交易] 步骤1/3：检查卖出信号...")
    sell_results = engine.process_sells(as_of_date=run_date)
    sold = [r for r in sell_results if r["status"] in ("sold", "partial_sell")]
    if sold:
        print(f"  卖出 {len(sold)} 只：")
        for r in sold:
            print(f"    {r['symbol']} {r['name']}：{r['status']} {r.get('shares', 0)}股，"
                  f"盈亏 {r.get('pnl', 0):.2f}元 ({r.get('pnl_pct', 0):.2f}%)，"
                  f"原因：{r.get('reason', '')[:30]}")
    else:
        print("  无卖出信号，全部持有。")

    # 2. 处理买入
    print("\n[模拟交易] 步骤2/3：处理买入候选...")
    candidates = load_candidates(run_date)
    print(f"  候选股总数：{len(candidates)}")

    buy_results = engine.process_buys(candidates)
    bought = [r for r in buy_results if r["status"] == "bought"]
    if bought:
        print(f"  买入 {len(bought)} 只：")
        for r in bought:
            print(f"    {r['symbol']} {r['name']}：{r['buy_price']:.2f}元 × {r['shares']}股 = {r['cost']:.2f}元")
    else:
        skipped = [r for r in buy_results if r["status"] == "skipped"]
        failed = [r for r in buy_results if r["status"] == "failed"]
        print(f"  无买入（跳过 {len(skipped)}，失败 {len(failed)}）")

    # 3. 更新净值
    print("\n[模拟交易] 步骤3/3：更新净值...")
    nav = engine.update_nav(as_of_date=run_date)
    print(f"  账户净值：{nav['nav']:,.2f} 元")
    print(f"  现金：{nav['cash']:,.2f} 元")
    print(f"  持仓市值：{nav['market_value']:,.2f} 元")
    print(f"  持仓数量：{nav['holding_count']} 只")
    print(f"  当日收益：{nav['daily_return_pct']:+.2f}%")
    print(f"  累计收益：{nav['total_return_pct']:+.2f}%")

    # 4. 生成报告
    print("\n[模拟交易] 生成报告...")
    performance = engine.calculate_performance()
    html_path = generate_sim_report_html(
        account_id=account_id,
        account_name=account["name"],
        performance=performance,
        nav=nav,
    )
    print(f"  报告已生成：{html_path}")

    return {
        "account": account,
        "sell_results": sell_results,
        "buy_results": buy_results,
        "nav": nav,
        "performance": performance,
        "report": str(html_path),
    }


def list_accounts() -> None:
    """列出所有模拟账户。"""
    with Storage() as db:
        accounts = db.list_sim_accounts()
        if not accounts:
            print("暂无模拟账户。使用 --init 创建。")
            return
        print(f"共 {len(accounts)} 个模拟账户：\n")
        print(f"{'ID':<5}{'名称':<25}{'初始资金':>12}{'当前净值':>12}{'累计收益':>10}{'状态':<8}")
        print("-" * 75)
        for a in accounts:
            total_ret = ((a["total_value"] / a["initial_capital"]) - 1) * 100
            print(f"{a['id']:<5}{a['name']:<25}{a['initial_capital']:>12,.2f}"
                  f"{a['total_value']:>12,.2f}{total_ret:>+9.2f}%{a['status']:<8}")


def generate_report() -> str:
    """生成模拟交易报告。"""
    with Storage() as db:
        account = db.get_sim_account_by_name(DEFAULT_ACCOUNT_NAME)
        if not account:
            print("账户不存在，请先初始化。")
            return ""

    engine = SimulationEngine(account_id=account["id"], position_per_stock=POSITION_PER_STOCK)
    performance = engine.calculate_performance()

    from trading_system.storage import Storage
    with Storage() as db:
        latest_nav = db.get_latest_sim_nav(account["id"])
        nav = latest_nav or {
            "date": "", "nav": account["total_value"],
            "cash": account["current_cash"],
            "market_value": account["total_value"] - account["current_cash"],
            "daily_return_pct": 0, "total_return_pct": 0, "holding_count": 0,
        }

    html_path = generate_sim_report_html(
        account_id=account["id"],
        account_name=account["name"],
        performance=performance,
        nav=nav,
    )
    print(f"报告已生成：{html_path}")
    return str(html_path)


def main():
    parser = argparse.ArgumentParser(description="模拟交易工具")
    parser.add_argument("--init", action="store_true", help="初始化模拟账户")
    parser.add_argument("--run", action="store_true", help="运行今日模拟交易")
    parser.add_argument("--report", action="store_true", help="生成模拟交易报告")
    parser.add_argument("--list", action="store_true", help="列出所有模拟账户")
    parser.add_argument("--date", type=str, default="", help="指定运行日期（YYYY-MM-DD）")
    parser.add_argument("--capital", type=float, default=DEFAULT_CAPITAL, help="初始资金")
    args = parser.parse_args()

    if args.init:
        init_account(capital=args.capital)
    elif args.run:
        result = run_simulation(run_date=args.date)
        # 保存运行结果摘要
        summary_path = OUTPUTS_DIR / "simulation_latest.json"
        summary = {
            "run_date": result["nav"]["date"],
            "nav": result["nav"],
            "performance": result["performance"],
            "report": result["report"],
        }
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    elif args.report:
        generate_report()
    elif args.list:
        list_accounts()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
