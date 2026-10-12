#!/usr/bin/env python3
"""展示周一选股结果"""
import json, glob, os
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent

files = sorted(glob.glob(str(PROJECT / "outputs" / "history" / "*monday*.json")), reverse=True)
if not files:
    files = sorted(glob.glob(str(PROJECT / "outputs" / "*monday*.json")), reverse=True)
if not files:
    files = sorted(glob.glob(str(PROJECT / "outputs" / "history" / "*.json")), reverse=True)

if not files:
    print("未找到选股结果文件")
    exit(1)

with open(files[0], encoding="utf-8") as f:
    data = json.load(f)

print(f"文件: {files[0]}")
print(f"选股日期: {data.get('date', 'N/A')}")
print(f"市场状态: {data.get('market_state', data.get('market_regime', 'N/A'))}")
print(f"活跃市值%: {data.get('active_market_value_pct', 'N/A')}")
print(f"市场理由: {data.get('market_reason', data.get('reason', 'N/A'))}")
print()

candidates = data.get("candidates", [])
print(f"总候选数: {len(candidates)}")
print()

# 只显示plan_action不是"无操作"的
actionable = [c for c in candidates if c.get("plan_action") not in ("无操作", "无操作/不开新仓")]
if actionable:
    print("=== 有操作建议的股票 ===")
    for i, c in enumerate(actionable):
        print(f"{i+1}. {c['symbol']} {c['name']} | 收盘{c['close']} 涨{c['pct_change']}% | 评分{c['candidate_score']} | 动作:{c['plan_action']} 仓位{c['suggested_position_pct']}%")
        print(f"   标签: {c['tags']}")
        print(f"   理由: {c['candidate_reason'][:100]}")
        print()

print("=== Top 15 候选 ===")
for i, c in enumerate(candidates[:15]):
    print(f"{i+1}. {c['symbol']} {c['name']} | 收盘{c['close']} 涨{c['pct_change']}% | 评分{c['candidate_score']} | 动作:{c['plan_action']} 仓位{c['suggested_position_pct']}%")
    print(f"   标签: {c['tags']}")
    print(f"   理由: {c['candidate_reason'][:100]}")
    print()
