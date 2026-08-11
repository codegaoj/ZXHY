from pathlib import Path
import json
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run_cmd(args):
    result = subprocess.run(
        [sys.executable, "-m", "src.trading_system.cli", *args],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout


def main():
    out1 = run_cmd(["samples-summary"])
    assert "样本数" in out1
    summary_path = ROOT / "outputs" / "sample_tag_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["sample_count"] >= 55

    out2 = run_cmd(["search-samples", "B1"])
    assert "共找到" in out2

    out3 = run_cmd(["run-demo"])
    assert "DEMO001" in out3
    plan_path = ROOT / "outputs" / "demo_trade_plan.json"
    plans = json.loads(plan_path.read_text(encoding="utf-8"))
    assert len(plans) == 4
    assert any(plan["risk_actions"] for plan in plans)
    assert any(plan["entry_signals"] for plan in plans)

    out4 = run_cmd(["update-market"])
    assert "已输出" in out4
    assert "来源：compass" in out4
    market_path = ROOT / "data" / "market_snapshot.csv"
    assert market_path.exists()
    market_rows = market_path.read_text(encoding="utf-8-sig")
    assert "B1/B2买点" in market_rows
    assert ",4.82," in market_rows

    out5 = run_cmd(["run-live-plan"])
    assert "DEMO001" in out5
    live_plan_path = ROOT / "outputs" / "live_trade_plan.json"
    live_plans = json.loads(live_plan_path.read_text(encoding="utf-8"))
    assert len(live_plans) == 4

    out6 = run_cmd(["daily-report"])
    assert "daily_close_report.html" in out6
    report_path = ROOT / "outputs" / "daily_close_report.html"
    assert report_path.exists()
    assert "每日收盘报告" in report_path.read_text(encoding="utf-8")
    print("smoke_test passed")


if __name__ == "__main__":
    main()
