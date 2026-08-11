from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, List

from .models import TradePlan


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_trade_plan_markdown(path: Path, plans: List[TradePlan], title: str = "演示交易计划") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {title}", ""]
    for plan in plans:
        lines.extend([
            f"## {plan.symbol} {plan.name}",
            "",
            f"- 市场状态：{plan.market_regime.value}",
            f"- 建议动作：{plan.action}",
            f"- 建议仓位：{plan.suggested_position_pct}%",
            f"- 信号分数：{plan.score}",
            f"- 备注：{'；'.join(plan.notes)}",
            "",
        ])
        if plan.entry_signals:
            lines.append("### 进攻/观察信号")
            for signal in plan.entry_signals:
                lines.append(f"- {signal.signal_type}：{signal.reason}（分数 {signal.score}）")
            lines.append("")
        if plan.risk_actions:
            lines.append("### 风控动作")
            for risk in plan.risk_actions:
                lines.append(f"- {risk.action}：{risk.reason}（优先级 {risk.priority}）")
            lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
