from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .backtesting import run_rule_backtest, write_backtest_markdown
from .data_normalizer import build_snapshot
from .data_provider import create_provider, default_date_range, load_watchlist
from .daily_report import write_daily_close_report
from .engines import TradePlanEngine
from .market_breadth import resolve_active_market_value
from .reporting import write_json, write_trade_plan_markdown
from .rulebook import Rulebook
from .sample_library import SampleLibrary
from .snapshot_store import load_snapshots, save_snapshots


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
OUTPUT_DIR = PROJECT_ROOT / "outputs"


def load_rulebook() -> Rulebook:
    return Rulebook.load(CONFIG_DIR / "rulebook.json")


def load_sample_library() -> SampleLibrary:
    cfg = json.loads((CONFIG_DIR / "sample_paths.json").read_text(encoding="utf-8"))
    sample_path = (PROJECT_ROOT / cfg["graphical_sample_library"]).resolve()
    return SampleLibrary.load(sample_path)


def load_data_source_config(config_path: str = "config/data_source.json") -> dict:
    path = PROJECT_ROOT / config_path
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_market_breadth_value(cfg: dict, provider) -> float:
    try:
        active_market_value = resolve_active_market_value(cfg, PROJECT_ROOT)
    except (FileNotFoundError, ValueError):
        provider_active_value = provider.fetch_active_market_value_pct()
        active_market_value = resolve_active_market_value(cfg, PROJECT_ROOT, fallback_value=provider_active_value)
    active_market_value_pct = active_market_value.value
    source_msg = f"活跃市值：{active_market_value_pct}（来源：{active_market_value.source}"
    if active_market_value.date:
        source_msg += f"，日期：{active_market_value.date}"
    source_msg += "）"
    print(source_msg)
    if active_market_value.note:
        print(f"活跃市值备注：{active_market_value.note}")
    return active_market_value_pct


def cmd_samples_summary(_: argparse.Namespace) -> None:
    library = load_sample_library()
    summary = library.summary()
    write_json(OUTPUT_DIR / "sample_tag_summary.json", summary)
    print(f"样本数：{summary['sample_count']}")
    print("图形类型：")
    for key, value in sorted(summary["type_counts"].items(), key=lambda item: item[1], reverse=True):
        print(f"  {key}: {value}")
    print("标签 Top 15：")
    for key, value in sorted(summary["tag_counts"].items(), key=lambda item: item[1], reverse=True)[:15]:
        print(f"  {key}: {value}")
    print(f"已输出：{OUTPUT_DIR / 'sample_tag_summary.json'}")


def cmd_search_samples(args: argparse.Namespace) -> None:
    library = load_sample_library()
    results = library.search(args.keyword)
    for sample in results[: args.limit]:
        print(f"{sample.id} | {sample.sample_type} | {'/'.join(sample.tags)} | {sample.title}")
        if sample.manual_note:
            print(f"  {sample.manual_note}")
    print(f"共找到 {len(results)} 条。")


def build_trade_plans(snapshot_path: Path, json_name: str, markdown_name: str, title: str):
    rulebook = load_rulebook()
    engine = TradePlanEngine(rulebook)
    snapshots = load_snapshots(snapshot_path)
    plans = [engine.build_plan(snapshot) for snapshot in snapshots]
    write_json(OUTPUT_DIR / json_name, [plan.to_dict() for plan in plans])
    write_trade_plan_markdown(OUTPUT_DIR / markdown_name, plans, title=title)
    for plan in plans:
        print(f"{plan.symbol} {plan.name}: {plan.action}，建议仓位 {plan.suggested_position_pct}%，分数 {plan.score}")
    print(f"已输出：{OUTPUT_DIR / json_name}")
    print(f"已输出：{OUTPUT_DIR / markdown_name}")
    return plans


def cmd_run_demo(_: argparse.Namespace) -> None:
    build_trade_plans(
        PROJECT_ROOT / "data" / "demo_market_snapshot.csv",
        "demo_trade_plan.json",
        "demo_trade_plan.md",
        "演示交易计划",
    )


def cmd_update_market(args: argparse.Namespace) -> None:
    cfg = load_data_source_config(args.config)
    provider = create_provider(cfg, PROJECT_ROOT)
    watchlist = load_watchlist(PROJECT_ROOT / cfg.get("watchlist_path", "data/watchlist.csv"))
    days = int(cfg.get("history_days", 180))
    start_date, end_date = default_date_range(days)
    active_market_value_pct = resolve_market_breadth_value(cfg, provider)

    snapshots = []
    failures = []
    pause_seconds = float(cfg.get("akshare_pause_seconds", 0))
    for item in watchlist:
        try:
            daily = provider.fetch_daily(item.symbol, start_date, end_date)
            snapshot = build_snapshot(item, daily, active_market_value_pct)
            snapshots.append(snapshot)
            print(f"已更新：{snapshot.symbol} {snapshot.name} 收盘 {snapshot.close}")
            if pause_seconds > 0:
                time.sleep(pause_seconds)
        except Exception as exc:
            failures.append({"symbol": item.symbol, "name": item.name, "error": str(exc)})
            print(f"更新失败：{item.symbol} {item.name}，原因：{exc}")

    output_path = PROJECT_ROOT / cfg.get("market_snapshot_path", "data/market_snapshot.csv")
    if failures:
        write_json(OUTPUT_DIR / "market_update_failures.json", failures)
    if not snapshots:
        raise RuntimeError("没有任何股票成功生成行情快照，请检查数据源或网络。")
    save_snapshots(output_path, snapshots)
    print(f"已输出：{output_path}")
    if failures:
        print(f"部分股票更新失败，已输出：{OUTPUT_DIR / 'market_update_failures.json'}")


def cmd_run_live_plan(args: argparse.Namespace) -> None:
    cfg = load_data_source_config(args.config)
    snapshot_path = PROJECT_ROOT / cfg.get("market_snapshot_path", "data/market_snapshot.csv")
    if not snapshot_path.exists():
        raise FileNotFoundError("尚未生成真实行情快照，请先运行：python -m src.trading_system.cli update-market")
    build_trade_plans(snapshot_path, "live_trade_plan.json", "live_trade_plan.md", "真实行情交易计划")


def cmd_backtest_rules(args: argparse.Namespace) -> None:
    cfg = load_data_source_config(args.config)
    provider = create_provider(cfg, PROJECT_ROOT)
    watchlist = load_watchlist(PROJECT_ROOT / cfg.get("watchlist_path", "data/watchlist.csv"))
    start_date, end_date = default_date_range(int(args.days))
    active_market_value_pct = resolve_market_breadth_value(cfg, provider)
    result = run_rule_backtest(
        provider=provider,
        watchlist=watchlist,
        active_market_value_pct=active_market_value_pct,
        start_date=start_date,
        end_date=end_date,
        holding_days=int(args.holding_days),
        min_lookback=int(args.min_lookback),
        limit=int(args.limit) if args.limit else None,
    )
    write_json(OUTPUT_DIR / "backtest_rules.json", result)
    write_backtest_markdown(OUTPUT_DIR / "backtest_rules.md", result)
    print("规则回测统计：")
    for rule, item in result["summary"].items():
        if rule == "total_events":
            continue
        print(f"{rule}: 命中 {item['count']} 次，成功率 {item['success_rate_pct']}%，平均后续收益 {item['avg_forward_return_pct']}%")
    print(f"已输出：{OUTPUT_DIR / 'backtest_rules.json'}")
    print(f"已输出：{OUTPUT_DIR / 'backtest_rules.md'}")


def cmd_daily_report(args: argparse.Namespace) -> None:
    cfg = load_data_source_config(args.config)
    snapshot_path = PROJECT_ROOT / cfg.get("market_snapshot_path", "data/market_snapshot.csv")
    if not snapshot_path.exists():
        raise FileNotFoundError("尚未生成行情快照，请先运行 update-market。")
    rulebook = load_rulebook()
    engine = TradePlanEngine(rulebook)
    snapshots = load_snapshots(snapshot_path)
    plans = [engine.build_plan(snapshot) for snapshot in snapshots]
    write_json(OUTPUT_DIR / "live_trade_plan.json", [plan.to_dict() for plan in plans])
    write_trade_plan_markdown(OUTPUT_DIR / "live_trade_plan.md", plans, title="真实行情交易计划")
    write_daily_close_report(OUTPUT_DIR / "daily_close_report.html", snapshot_path, plans)
    print(f"已输出：{OUTPUT_DIR / 'daily_close_report.html'}")


def cmd_validate_akshare(args: argparse.Namespace) -> None:
    cfg = load_data_source_config(args.config)
    if str(cfg.get("source", "")).lower() != "akshare":
        raise ValueError("validate-akshare 需要使用 source=akshare 的配置文件。")
    provider = create_provider(cfg, PROJECT_ROOT)
    watchlist = load_watchlist(PROJECT_ROOT / cfg.get("watchlist_path", "data/watchlist_real.csv"))
    days = int(args.days)
    start_date, end_date = default_date_range(days)
    active_market_value_pct = resolve_market_breadth_value(cfg, provider)

    snapshots = []
    for item in watchlist[: args.limit]:
        daily = provider.fetch_daily(item.symbol, start_date, end_date)
        snapshot = build_snapshot(item, daily, active_market_value_pct)
        snapshots.append(snapshot)
        print(f"AkShare 验证通过：{snapshot.symbol} {snapshot.name} 收盘 {snapshot.close}，记录数 {len(daily)}")

    write_json(OUTPUT_DIR / "akshare_validation.json", [snapshot.__dict__ for snapshot in snapshots])
    print(f"已输出：{OUTPUT_DIR / 'akshare_validation.json'}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="股票交易系统基础命令行")
    sub = parser.add_subparsers(required=True)

    p_summary = sub.add_parser("samples-summary", help="统计图形样本库标签")
    p_summary.set_defaults(func=cmd_samples_summary)

    p_search = sub.add_parser("search-samples", help="搜索图形样本库")
    p_search.add_argument("keyword", help="关键词，例如 B1、MACD、滴滴")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.set_defaults(func=cmd_search_samples)

    p_demo = sub.add_parser("run-demo", help="运行离线演示交易计划")
    p_demo.set_defaults(func=cmd_run_demo)

    p_update = sub.add_parser("update-market", help="拉取行情并生成真实行情快照")
    p_update.add_argument("--config", default="config/data_source.json", help="数据源配置文件")
    p_update.set_defaults(func=cmd_update_market)

    p_live = sub.add_parser("run-live-plan", help="基于真实行情快照生成交易计划")
    p_live.add_argument("--config", default="config/data_source.json", help="数据源配置文件")
    p_live.set_defaults(func=cmd_run_live_plan)

    p_validate = sub.add_parser("validate-akshare", help="验证 AkShare 能否拉取真实股票日线")
    p_validate.add_argument("--config", default="config/data_source_akshare.json", help="AkShare 数据源配置文件")
    p_validate.add_argument("--limit", type=int, default=1, help="验证股票数量")
    p_validate.add_argument("--days", type=int, default=90, help="验证拉取最近多少天")
    p_validate.set_defaults(func=cmd_validate_akshare)

    p_backtest = sub.add_parser("backtest-rules", help="回测 B1、S1、滴滴、砖形图四类规则")
    p_backtest.add_argument("--config", default="config/data_source.json", help="数据源配置文件")
    p_backtest.add_argument("--days", type=int, default=260, help="回测最近多少天")
    p_backtest.add_argument("--holding-days", type=int, default=5, help="命中后观察多少个交易日")
    p_backtest.add_argument("--min-lookback", type=int, default=40, help="最少历史窗口")
    p_backtest.add_argument("--limit", type=int, default=0, help="限制股票数量，0 表示全部")
    p_backtest.set_defaults(func=cmd_backtest_rules)

    p_report = sub.add_parser("daily-report", help="生成每日收盘 HTML 报告")
    p_report.add_argument("--config", default="config/data_source.json", help="数据源配置文件")
    p_report.set_defaults(func=cmd_daily_report)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
