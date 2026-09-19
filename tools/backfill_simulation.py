"""补跑模拟交易历史数据（从指定日期到今天）。"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

from trading_system.trade_calendar import trading_days_between, last_trading_day
from run_simulation import run_simulation, init_account, DEFAULT_ACCOUNT_NAME
from trading_system.storage import Storage


def backfill_history(start_date: str, end_date: str = "") -> None:
    """补跑从 start_date 到 end_date 之间所有交易日的模拟交易。"""
    if not end_date:
        end_date = last_trading_day().isoformat()

    # 获取交易日列表
    from datetime import date as _date
    start = _date.fromisoformat(start_date)
    end = _date.fromisoformat(end_date)
    trading_days = [d.isoformat() for d in trading_days_between(start, end)]
    if not trading_days:
        print(f"[补跑] {start_date} 到 {end_date} 之间没有交易日")
        return

    print(f"[补跑] 需要补跑 {len(trading_days)} 个交易日：{trading_days[0]} ~ {trading_days[-1]}")

    # 检查账户是否存在
    with Storage() as db:
        account = db.get_sim_account_by_name(DEFAULT_ACCOUNT_NAME)
        if not account:
            print("[补跑] 账户不存在，先初始化...")
            account = init_account()

        # 检查已经有净值记录的最后日期
        latest_nav = db.get_latest_sim_nav(account["id"])
        if latest_nav:
            print(f"[补跑] 当前最新净值日期：{latest_nav['date']}，净值：{latest_nav['nav']:,.2f}")
            # 过滤掉已经跑过的日期
            trading_days = [d for d in trading_days if d > latest_nav["date"]]
            if not trading_days:
                print("[补跑] 所有日期都已跑过，无需补跑")
                return
            print(f"[补跑] 实际需要补跑 {len(trading_days)} 天：{trading_days[0]} ~ {trading_days[-1]}")

    # 逐个交易日运行
    success = 0
    failed = 0
    for i, trade_date in enumerate(trading_days, 1):
        print(f"\n{'='*60}")
        print(f"[补跑] [{i}/{len(trading_days)}] 运行日期：{trade_date}")
        print(f"{'='*60}")
        try:
            result = run_simulation(run_date=trade_date)
            nav = result.get("nav", {})
            print(f"\n[补跑] ✓ {trade_date} 完成：净值 {nav.get('nav', 0):,.2f}，累计收益 {nav.get('total_return_pct', 0):+.2f}%")
            success += 1
        except Exception as e:
            print(f"\n[补跑] ✗ {trade_date} 失败：{e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*60}")
    print(f"[补跑] 完成！成功 {success} 天，失败 {failed} 天")
    print(f"{'='*60}")

    # 最终状态
    with Storage() as db:
        account = db.get_sim_account_by_name(DEFAULT_ACCOUNT_NAME)
        latest_nav = db.get_latest_sim_nav(account["id"])
        if latest_nav:
            print(f"  最新净值日期：{latest_nav['date']}")
            print(f"  当前净值：{latest_nav['nav']:,.2f} 元")
            print(f"  累计收益：{latest_nav['total_return_pct']:+.2f}%")
            print(f"  持仓数量：{latest_nav['holding_count']} 只")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="补跑模拟交易历史数据")
    parser.add_argument("--start", type=str, default="2026-09-08", help="开始日期（含）")
    parser.add_argument("--end", type=str, default="", help="结束日期（含），默认今天")
    args = parser.parse_args()

    backfill_history(args.start, args.end)
