from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .models import TradePlan
from .storage import Storage


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


# ─── 模拟交易报告 ───

def generate_sim_report_html(
    account_id: int,
    account_name: str,
    performance: Dict,
    nav: Dict,
    output_dir: Optional[Path] = None,
) -> Path:
    """生成模拟交易 HTML 报告。"""
    if output_dir is None:
        from pathlib import Path as _P
        output_dir = _P(r"g:\BaiduNetdiskDownload\z直播文字\trading-system-core\outputs")

    output_dir.mkdir(parents=True, exist_ok=True)
    html_path = output_dir / "simulation_report.html"

    # 获取数据
    with Storage() as db:
        nav_history = db.get_sim_nav_history(account_id, limit=500)
        holdings = db.get_sim_open_holdings(account_id)
        transactions = db.get_sim_transactions(account_id, limit=100)
        closed_holdings = [
            h for h in db.get_sim_all_holdings(account_id, limit=200)
            if h["status"] == "closed"
        ]

    # 净值曲线数据（简化版：用文字点图）
    nav_chart = _build_nav_chart(nav_history)

    # 绩效卡片
    perf_cards = _build_perf_cards(performance)

    # 持仓表格
    holdings_rows = _build_holdings_rows(holdings)

    # 已平仓表格
    closed_rows = _build_closed_rows(closed_holdings)

    # 交易记录
    tx_rows = _build_tx_rows(transactions)

    # 净值历史表格
    nav_rows = _build_nav_rows(nav_history[-30:])  # 最近30天

    page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>模拟交易报告 - {html.escape(account_name)}</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; background:#f5f7fa; color:#1f2937; padding:20px; }}
  .container {{ max-width:1200px; margin:0 auto; }}
  header {{ background:linear-gradient(135deg, #1e3a5f, #2d5a8e); color:white; padding:30px; border-radius:12px; margin-bottom:24px; }}
  header h1 {{ font-size:28px; margin-bottom:8px; }}
  header p {{ opacity:0.9; font-size:14px; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:16px; margin-bottom:24px; }}
  .card {{ background:white; border-radius:10px; padding:20px; box-shadow:0 2px 8px rgba(0,0,0,0.06); }}
  .card .label {{ font-size:13px; color:#6b7280; margin-bottom:6px; }}
  .card .value {{ font-size:24px; font-weight:600; }}
  .card .value.positive {{ color:#10b981; }}
  .card .value.negative {{ color:#ef4444; }}
  section {{ background:white; border-radius:10px; padding:24px; margin-bottom:20px; box-shadow:0 2px 8px rgba(0,0,0,0.06); }}
  section h2 {{ font-size:18px; margin-bottom:16px; color:#1f2937; border-left:4px solid #2d5a8e; padding-left:10px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th {{ background:#f3f4f6; text-align:left; padding:10px 12px; font-weight:600; color:#374151; border-bottom:2px solid #e5e7eb; }}
  td {{ padding:10px 12px; border-bottom:1px solid #f3f4f6; }}
  tr:hover {{ background:#f9fafb; }}
  .positive {{ color:#10b981; }}
  .negative {{ color:#ef4444; }}
  .chart-box {{ background:#f9fafb; border-radius:8px; padding:16px; font-family: monospace; font-size:11px; line-height:1.4; overflow-x:auto; white-space:pre; }}
  .grid2 {{ display:grid; grid-template-columns:1fr 1fr; gap:20px; }}
  @media (max-width: 768px) {{ .grid2 {{ grid-template-columns:1fr; }} }}
  .tag {{ display:inline-block; padding:2px 8px; border-radius:4px; font-size:12px; }}
  .tag-buy {{ background:#dcfce7; color:#166534; }}
  .tag-sell {{ background:#fee2e2; color:#991b1b; }}
  .note {{ font-size:12px; color:#6b7280; margin-top:8px; }}
</style>
</head>
<body>
<div class="container">

<header>
  <h1>📊 模拟交易报告</h1>
  <p>账户：{html.escape(account_name)} &nbsp;|&nbsp; 更新日期：{nav.get('date', '-')}</p>
</header>

<div class="cards">
  {perf_cards}
</div>

<section>
  <h2>净值走势</h2>
  <div class="chart-box">{nav_chart}</div>
  <p class="note">共 {len(nav_history)} 个交易日，初始资金 {performance.get('initial_capital', 0):,.0f} 元</p>
</section>

<div class="grid2">
  <section>
    <h2>当前持仓 ({len(holdings)} 只)</h2>
    <div style="overflow-x:auto;">
      <table>
        <thead><tr><th>代码</th><th>名称</th><th>买入日</th><th>成本</th><th>现价</th><th>盈亏</th><th>盈亏%</th></tr></thead>
        <tbody>{holdings_rows or '<tr><td colspan="7" style="text-align:center;color:#999;">暂无持仓</td></tr>'}</tbody>
      </table>
    </div>
  </section>
  <section>
    <h2>已平仓记录 ({len(closed_holdings)} 笔)</h2>
    <div style="overflow-x:auto;">
      <table>
        <thead><tr><th>代码</th><th>名称</th><th>买入</th><th>卖出</th><th>盈亏%</th><th>卖出原因</th></tr></thead>
        <tbody>{closed_rows or '<tr><td colspan="6" style="text-align:center;color:#999;">暂无平仓</td></tr>'}</tbody>
      </table>
    </div>
  </section>
</div>

<section>
  <h2>最近交易记录</h2>
  <div style="overflow-x:auto;">
    <table>
      <thead><tr><th>日期</th><th>方向</th><th>代码</th><th>名称</th><th>价格</th><th>数量</th><th>金额</th><th>盈亏</th><th>原因</th></tr></thead>
      <tbody>{tx_rows or '<tr><td colspan="9" style="text-align:center;color:#999;">暂无交易</td></tr>'}</tbody>
    </table>
  </div>
</section>

<section>
  <h2>净值历史（最近30天）</h2>
  <div style="overflow-x:auto;">
    <table>
      <thead><tr><th>日期</th><th>净值</th><th>现金</th><th>持仓市值</th><th>当日收益</th><th>累计收益</th><th>持仓数</th></tr></thead>
      <tbody>{nav_rows or '<tr><td colspan="7" style="text-align:center;color:#999;">暂无数据</td></tr>'}</tbody>
    </table>
  </div>
</section>

</div>
</body>
</html>"""

    html_path.write_text(page, encoding="utf-8")
    return html_path


def _build_perf_cards(perf: Dict) -> str:
    """构建绩效卡片 HTML。"""
    cards = [
        ("累计收益率", f"{perf.get('total_return_pct', 0):+.2f}%", _pct_class(perf.get('total_return_pct', 0))),
        ("年化收益率", f"{perf.get('annualized_return_pct', 0):+.2f}%", _pct_class(perf.get('annualized_return_pct', 0))),
        ("最大回撤", f"-{perf.get('max_drawdown_pct', 0):.2f}%", "negative"),
        ("夏普比率", f"{perf.get('sharpe_ratio', 0):.2f}", "positive" if perf.get('sharpe_ratio', 0) > 1 else ""),
        ("胜率", f"{perf.get('win_rate_pct', 0):.1f}%", "positive" if perf.get('win_rate_pct', 0) > 50 else "negative"),
        ("盈亏比", f"{perf.get('profit_factor', 0):.2f}", "positive" if perf.get('profit_factor', 0) > 1 else "negative"),
        ("交易次数", f"{perf.get('total_trades', 0)} 笔", ""),
        ("平均持仓", f"{perf.get('avg_holding_days', 0)} 天", ""),
    ]
    html_parts = []
    for label, value, cls in cards:
        html_parts.append(
            f'<div class="card"><div class="label">{label}</div>'
            f'<div class="value {cls}">{value}</div></div>'
        )
    return "\n".join(html_parts)


def _build_nav_chart(nav_history: List[Dict]) -> str:
    """用 ASCII 字符画一个简易净值曲线图。"""
    if not nav_history:
        return "暂无净值数据"

    navs = [n["nav"] for n in nav_history]
    if len(navs) < 2:
        return f"起始净值：{navs[0]:,.2f}"

    min_nav = min(navs)
    max_nav = max(navs)
    if max_nav == min_nav:
        max_nav = min_nav * 1.01
        min_nav = min_nav * 0.99

    height = 20  # 行数
    width = min(len(navs), 60)  # 最多显示60个点

    # 采样
    step = max(1, len(navs) // 60)
    sampled = navs[::step][:60]

    # 生成图表
    chart_lines = []
    scale = (max_nav - min_nav) / height

    for row in range(height):
        threshold = max_nav - row * scale
        line = ""
        for val in sampled:
            if val >= threshold:
                line += "█"
            else:
                line += " "
        # 加 y 轴标签
        label = f"{threshold:,.0f}"
        chart_lines.append(f"{label:>10} │{line}")

    # x 轴
    x_axis = " " * 11 + "└" + "─" * len(sampled)
    chart_lines.append(x_axis)

    # 日期标签
    if nav_history:
        start_date = nav_history[0]["date"]
        end_date = nav_history[-1]["date"]
        padding = max(0, len(sampled) - len(start_date) - len(end_date))
        chart_lines.append(f"{'':>11}  {start_date}{'':>{padding}}{end_date}")

    return "\n".join(chart_lines)


def _build_holdings_rows(holdings: List[Dict]) -> str:
    rows = []
    for h in sorted(holdings, key=lambda x: x["pnl_pct"] if x["pnl_pct"] else 0, reverse=True):
        pnl_pct = h.get("pnl_pct", 0) or 0
        pnl = h.get("pnl", 0) or 0
        cls = "positive" if pnl_pct >= 0 else "negative"
        rows.append(
            f"<tr><td>{h['symbol']}</td><td>{html.escape(h.get('name',''))}</td>"
            f"<td>{h.get('buy_date','')}</td>"
            f"<td>{h.get('buy_price',0):.2f}</td>"
            f"<td>{h.get('current_price',0):.2f}</td>"
            f'<td class="{cls}">{pnl:+.2f}</td>'
            f'<td class="{cls}">{pnl_pct:+.2f}%</td></tr>'
        )
    return "\n".join(rows)


def _build_closed_rows(closed: List[Dict]) -> str:
    rows = []
    for h in sorted(closed, key=lambda x: x.get("sell_date", ""), reverse=True)[:30]:
        pnl_pct = h.get("pnl_pct", 0) or 0
        cls = "positive" if pnl_pct >= 0 else "negative"
        reason = html.escape(str(h.get("sell_reason", ""))[:15])
        rows.append(
            f"<tr><td>{h['symbol']}</td><td>{html.escape(h.get('name',''))}</td>"
            f"<td>{h.get('buy_date','')}</td>"
            f"<td>{h.get('sell_date','')}</td>"
            f'<td class="{cls}">{pnl_pct:+.2f}%</td>'
            f"<td>{reason}</td></tr>"
        )
    return "\n".join(rows)


def _build_tx_rows(transactions: List[Dict]) -> str:
    rows = []
    for t in transactions[:30]:
        is_buy = t["type"] == "buy"
        tag_class = "tag-buy" if is_buy else "tag-sell"
        tag_text = "买入" if is_buy else "卖出"
        pnl_str = f"{t.get('pnl',0):+.2f}" if t.get("pnl") is not None else "-"
        pnl_cls = ""
        if t.get("pnl") is not None:
            pnl_cls = "positive" if t["pnl"] >= 0 else "negative"
        reason = html.escape(str(t.get("reason", ""))[:20])
        rows.append(
            f"<tr><td>{t.get('date','')}</td>"
            f'<td><span class="tag {tag_class}">{tag_text}</span></td>'
            f"<td>{t['symbol']}</td><td>{html.escape(t.get('name',''))}</td>"
            f"<td>{t.get('price',0):.2f}</td><td>{t.get('shares',0)}</td>"
            f"<td>{t.get('amount',0):,.0f}</td>"
            f'<td class="{pnl_cls}">{pnl_str}</td>'
            f"<td>{reason}</td></tr>"
        )
    return "\n".join(rows)


def _build_nav_rows(nav_history: List[Dict]) -> str:
    rows = []
    for n in reversed(nav_history):
        daily = n.get("daily_return_pct", 0)
        total = n.get("total_return_pct", 0)
        d_cls = "positive" if daily >= 0 else "negative"
        t_cls = "positive" if total >= 0 else "negative"
        rows.append(
            f"<tr><td>{n['date']}</td>"
            f"<td>{n['nav']:,.2f}</td>"
            f"<td>{n.get('cash',0):,.2f}</td>"
            f"<td>{n.get('market_value',0):,.2f}</td>"
            f'<td class="{d_cls}">{daily:+.2f}%</td>'
            f'<td class="{t_cls}">{total:+.2f}%</td>'
            f"<td>{n.get('holding_count',0)}</td></tr>"
        )
    return "\n".join(rows)


def _pct_class(value: float) -> str:
    return "positive" if value >= 0 else "negative"
