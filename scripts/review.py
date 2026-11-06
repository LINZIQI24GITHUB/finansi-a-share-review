#!/usr/bin/env python3
"""A-share trade journal — 成交级记账 → 持仓周期 → 绩效与行为分析。

用法:
    python ashare_journal.py demo                      # 生成示例并分析
    python ashare_journal.py add --code 600519 --name 贵州茅台 --side buy \
        --price 1680 --qty 100 --date 2025-03-03 --strategy 趋势突破
    python ashare_journal.py list [--cycles]
    python ashare_journal.py stats [--strategy 趋势突破] [--json]
    python ashare_journal.py report --out 复盘.md
    python ashare_journal.py validate

数据文件默认 fills.csv（可用 ASHARE_JOURNAL 环境变量覆盖）。
只依赖标准库。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compat import calc_fees, check_lot, limit_pct, min_lot  # noqa: E402

DEFAULT_PATH = Path(os.getenv("ASHARE_JOURNAL", "fills.csv"))
FIELDS = [
    "date", "time", "code", "name", "side", "price", "qty",
    "commission", "stamp_duty", "transfer_fee",
    "strategy", "tag", "note",
]
TRADE_STRATEGIES = [
    "趋势突破", "低吸反弹", "打板", "事件驱动", "网格", "定投",
    "业绩预增", "板块轮动", "抄底", "止损离场",
]

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ── helpers ───────────────────────────────────────────────────────────
def pad(text: str, width: int) -> str:
    """Left-align accounting for CJK double width."""
    text = str(text)
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    return text + " " * max(width - w, 0)


def money(x: float) -> str:
    return f"{x:,.2f}"


def pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def weekdays_between(start: str, end: str) -> int:
    """Trading-day proxy: weekdays only (holidays not excluded)."""
    d0 = datetime.strptime(start, "%Y-%m-%d")
    d1 = datetime.strptime(end, "%Y-%m-%d")
    if d1 < d0:
        return 0
    days = (d1 - d0).days
    full_weeks, rest = divmod(days, 7)
    count = full_weeks * 5
    for i in range(rest + 1):
        if (d0.weekday() + i) % 7 < 5:
            count += 1
    return count


# ── storage ───────────────────────────────────────────────────────────
def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            if not row.get("code"):
                continue
            rows.append({k: (v or "").strip() for k, v in row.items()})
    rows.sort(key=lambda r: (r["date"], r.get("time") or "00:00:00"))
    return rows


def save(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def fill_fees(row: dict) -> dict:
    """Fill in commission/stamp/transfer when the user left them blank."""
    amount = float(row["price"]) * int(float(row["qty"]))
    auto = calc_fees(amount, row["side"])
    if not row.get("commission"):
        row["commission"] = f"{auto.commission:.2f}"
    if not row.get("stamp_duty"):
        row["stamp_duty"] = f"{auto.stamp_duty:.2f}"
    if not row.get("transfer_fee"):
        row["transfer_fee"] = f"{auto.transfer_fee:.2f}"
    return row


def row_fee(row: dict) -> float:
    return sum(float(row.get(k) or 0) for k in ("commission", "stamp_duty", "transfer_fee"))


def position_of(rows: list[dict], code: str, on_date: str | None = None) -> tuple[int, int]:
    """(持仓股数, 可卖股数)。T+1：当日买入不算可卖。"""
    code = str(code).zfill(6)
    held = same_day = 0
    for r in rows:
        if str(r.get("code", "")).zfill(6) != code:
            continue
        qty = int(float(r.get("qty") or 0))
        if on_date and r["date"] > on_date:
            continue
        if r["side"] == "buy":
            held += qty
            if on_date and r["date"] == on_date:
                same_day += qty
        else:
            held -= qty
    return held, max(held - same_day, 0)


# ── cycle engine ──────────────────────────────────────────────────────
def build_cycles(fills: list[dict]) -> tuple[list[dict], dict[str, dict]]:
    """Average-cost position accounting → closed cycles + open positions."""
    pos: dict[str, dict] = {}
    closed: list[dict] = []

    for f in fills:
        code = f["code"].zfill(6)
        qty = int(float(f["qty"]))
        price = float(f["price"])
        amount = price * qty
        fee = row_fee(f)

        if f["side"] == "buy":
            p = pos.setdefault(code, {
                "code": code, "name": f.get("name", ""), "qty": 0, "cost": 0.0,
                "open_date": f["date"], "buy_qty": 0, "buy_amount": 0.0,
                "sell_qty": 0, "sell_amount": 0.0, "fees": 0.0, "sell_fees": 0.0,
                "cost_sold": 0.0,
                "strategy": f.get("strategy", ""), "max_value": 0.0,
                "adds_at_loss": 0,
            })
            avg_cost = p["cost"] / p["qty"] if p["qty"] else 0.0
            if p["qty"] and price < avg_cost:
                p["adds_at_loss"] += 1
            p["qty"] += qty
            p["cost"] += amount + fee
            p["buy_qty"] += qty
            p["buy_amount"] += amount
            p["fees"] += fee
            p["max_value"] = max(p["max_value"], amount)
        else:
            p = pos.get(code)
            if p is None or p["qty"] < qty:
                raise SystemExit(f"✗ {f['date']} 卖出 {code} {qty} 股超过持仓，请检查数据")
            avg_unit = p["cost"] / p["qty"]
            cost_out = avg_unit * qty
            p["qty"] -= qty
            p["cost"] -= cost_out
            p["cost_sold"] += cost_out
            p["sell_qty"] += qty
            p["sell_amount"] += amount
            p["fees"] += fee
            p["sell_fees"] += fee
            if p["qty"] == 0:
                proceeds = p["sell_amount"] - p["sell_fees"]
                pnl = proceeds - p["cost_sold"]
                closed.append({
                    "code": code,
                    "name": p["name"],
                    "strategy": p["strategy"] or "未标注",
                    "open_date": p["open_date"],
                    "close_date": f["date"],
                    "buy_qty": p["buy_qty"],
                    "buy_amount": p["buy_amount"],
                    "sell_amount": proceeds,
                    "avg_buy": p["buy_amount"] / p["buy_qty"] if p["buy_qty"] else 0.0,
                    "avg_sell": proceeds / p["sell_qty"] if p["sell_qty"] else 0.0,
                    "fees": p["fees"],
                    "pnl": pnl,
                    "return_pct": pnl / p["buy_amount"] if p["buy_amount"] else 0.0,
                    "hold_days": weekdays_between(p["open_date"], f["date"]),
                    "adds_at_loss": p["adds_at_loss"],
                    "max_value": p["max_value"],
                })
                pos.pop(code)

    return closed, {k: v for k, v in pos.items() if v["qty"] > 0}


# ── metrics ───────────────────────────────────────────────────────────
def compute_stats(fills: list[dict], closed: list[dict], open_pos: dict[str, dict]) -> dict:
    wins = [c for c in closed if c["pnl"] > 0]
    losses = [c for c in closed if c["pnl"] <= 0]
    gross_win = sum(c["pnl"] for c in wins)
    gross_loss = abs(sum(c["pnl"] for c in losses))
    fees = {
        "commission": sum(float(f.get("commission") or 0) for f in fills),
        "stamp_duty": sum(float(f.get("stamp_duty") or 0) for f in fills),
        "transfer_fee": sum(float(f.get("transfer_fee") or 0) for f in fills),
    }
    fees["total"] = sum(fees.values())

    # 已实现盈亏曲线 → 最大回撤
    cum, peak, dd = 0.0, 0.0, 0.0
    for c in sorted(closed, key=lambda x: x["close_date"]):
        cum += c["pnl"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)

    dates = sorted({f["date"] for f in fills})
    span_days = (datetime.strptime(dates[-1], "%Y-%m-%d") - datetime.strptime(dates[0], "%Y-%m-%d")).days if len(dates) > 1 else 1
    span_weeks = max(span_days / 7, 1)

    by_strategy: dict[str, list[dict]] = defaultdict(list)
    for c in closed:
        by_strategy[c["strategy"]].append(c)

    by_month: dict[str, float] = defaultdict(float)
    for c in closed:
        by_month[c["close_date"][:7]] += c["pnl"]

    return {
        "fills": len(fills),
        "symbols": len({f["code"] for f in fills}),
        "closed": len(closed),
        "open": len(open_pos),
        "fees": fees,
        "pnl": sum(c["pnl"] for c in closed),
        "win_rate": len(wins) / len(closed) if closed else 0.0,
        "avg_win": gross_win / len(wins) if wins else 0.0,
        "avg_loss": -gross_loss / len(losses) if losses else 0.0,
        "payoff": (gross_win / len(wins)) / (gross_loss / len(losses)) if wins and losses else 0.0,
        "expectancy": sum(c["pnl"] for c in closed) / len(closed) if closed else 0.0,
        "profit_factor": gross_win / gross_loss if gross_loss else float("inf") if gross_win else 0.0,
        "max_dd": dd,
        "hold_win": sum(c["hold_days"] for c in wins) / len(wins) if wins else 0.0,
        "hold_loss": sum(c["hold_days"] for c in losses) / len(losses) if losses else 0.0,
        "by_strategy": by_strategy,
        "by_month": dict(sorted(by_month.items())),
        "turnover": round(len(fills) / span_weeks, 1),
        "adds_at_loss": sum(c["adds_at_loss"] for c in closed),
        "concentration": (max((c["buy_amount"] for c in closed), default=0.0)
                          / max(sum(c["buy_amount"] for c in closed), 1e-9)),
        "fee_ratio": fees["total"] / max(sum(c["buy_amount"] for c in closed), 1e-9),
    }


def flags(s: dict) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if s["closed"] == 0:
        return [("提示", "还没有已平仓的完整交易，先记几笔再来复盘")]
    if s["hold_win"] and s["hold_loss"] and s["hold_win"] < s["hold_loss"] * 0.7:
        out.append(("处置效应", f"盈利单平均持有 {s['hold_win']:.0f} 个交易日、亏损单 {s['hold_loss']:.0f} 个——"
                              "拿不住盈利、死扛亏损，这是最贵的习惯"))
    if s["adds_at_loss"]:
        out.append(("亏损加仓", f"出现 {s['adds_at_loss']} 次跌破成本后加仓，摊薄成本不等于降低风险"))
    if s["turnover"] > 10:
        out.append(("过度交易", f"周均 {s['turnover']} 笔成交，佣金和印花税会吃掉边际优势"))
    if s["concentration"] > 0.5:
        out.append(("过度集中", f"单票买入额占总买入 {pct(s['concentration'])}，一次黑天鹅就够呛"))
    if s["fee_ratio"] > 0.01:
        out.append(("费用偏高", f"总费用占买入额 {pct(s['fee_ratio'])}，超过 1% 说明交易过碎或仓位过小"))
    if s["payoff"] and s["payoff"] < 1 and s["win_rate"] < 0.5:
        out.append(("负期望", "胜率与盈亏比同时不占优，先别加资金"))
    if s["max_dd"] > abs(s["pnl"]) and s["max_dd"] > 0:
        out.append(("回撤偏大", f"已实现最大回撤 {money(s['max_dd'])} 超过当前净盈利"))
    return out


# ── rendering ─────────────────────────────────────────────────────────
def render_stats(s: dict, title: str = "A股交易日志 · 绩效概览") -> str:
    L: list[str] = []
    bar = "─" * 62
    L.append("")
    L.append(bar)
    L.append(f"  {title}")
    L.append(bar)
    L.append(f"  成交笔数 {s['fills']:<6} 标的 {s['symbols']:<4} 已平仓 {s['closed']:<4} 未平仓 {s['open']}")
    L.append(f"  已实现盈亏  {money(s['pnl']):>12} 元      胜率 {pct(s['win_rate'])}")
    L.append(f"  平均盈利   {money(s['avg_win']):>12} 元      平均亏损 {money(s['avg_loss']):>10} 元")
    L.append(f"  盈亏比     {s['payoff']:>12.2f}          期望/笔 {money(s['expectancy']):>9} 元")
    pf = "∞" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
    L.append(f"  盈利因子   {pf:>12}          最大回撤 {money(s['max_dd']):>9} 元")
    L.append(f"  盈利单持有 {s['hold_win']:>9.0f} 交易日   亏损单持有 {s['hold_loss']:.0f} 交易日")
    L.append(bar)
    L.append(f"  费用：佣金 {money(s['fees']['commission'])} + 印花税 {money(s['fees']['stamp_duty'])}"
             f" + 过户费 {money(s['fees']['transfer_fee'])} = {money(s['fees']['total'])} 元")
    L.append(f"  费用占买入额 {pct(s['fee_ratio'])}    周均成交 {s['turnover']} 笔")
    L.append(bar)

    if s["by_strategy"]:
        L.append(f"  按策略")
        L.append("    " + pad("策略", 12) + pad("笔数", 6) + pad("胜率", 8) + pad("盈亏", 14) + "均持有")
        for name, rows in sorted(s["by_strategy"].items(), key=lambda kv: -sum(r["pnl"] for r in kv[1])):
            w = sum(1 for r in rows if r["pnl"] > 0)
            L.append("    " + pad(name, 12) + pad(len(rows), 6)
                     + pad(pct(w / len(rows)), 8)
                     + pad(money(sum(r["pnl"] for r in rows)), 14)
                     + f"{sum(r['hold_days'] for r in rows) / len(rows):.0f}日")
        L.append(bar)

    if s["by_month"]:
        L.append("  按月已实现盈亏")
        items = list(s["by_month"].items())
        for i in range(0, len(items), 4):
            L.append("    " + "   ".join(f"{m} {money(v):>10}" for m, v in items[i:i + 4]))
        L.append(bar)

    fl = flags(s)
    if fl:
        L.append("  行为提示")
        for tag, msg in fl:
            L.append(f"    [{tag}] {msg}")
        L.append(bar)
    return "\n".join(L)


def render_report(s: dict, fills: list[dict], closed: list[dict]) -> str:
    md = [f"# A股交易复盘报告", "",
          f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M}　数据源：{s['fills']} 笔成交", "",
          "## 一、总览", "",
          "| 指标 | 数值 | 指标 | 数值 |", "|---|---|---|---|",
          f"| 已实现盈亏 | {money(s['pnl'])} 元 | 胜率 | {pct(s['win_rate'])} |",
          f"| 盈亏比 | {s['payoff']:.2f} | 期望/笔 | {money(s['expectancy'])} 元 |",
          f"| 最大回撤 | {money(s['max_dd'])} 元 | 盈利因子 | "
          + ("∞" if s['profit_factor'] == float('inf') else f"{s['profit_factor']:.2f}") + " |",
          f"| 总费用 | {money(s['fees']['total'])} 元 | 费用占买入额 | {pct(s['fee_ratio'])} |",
          f"| 盈利单持有 | {s['hold_win']:.0f} 交易日 | 亏损单持有 | {s['hold_loss']:.0f} 交易日 |", "",
          "## 二、费用结构", "",
          "| 项目 | 金额（元） |", "|---|---|",
          f"| 佣金 | {money(s['fees']['commission'])} |",
          f"| 印花税（卖出） | {money(s['fees']['stamp_duty'])} |",
          f"| 过户费 | {money(s['fees']['transfer_fee'])} |", ""]

    if closed:
        md += ["## 三、逐笔明细", "",
               "| 代码 | 名称 | 策略 | 建仓 | 清仓 | 持有 | 买入均价 | 卖出均价 | 盈亏 |",
               "|---|---|---|---|---|---|---|---|---|"]
        for c in sorted(closed, key=lambda x: x["close_date"]):
            md.append(f"| {c['code']} | {c['name']} | {c['strategy']} | {c['open_date']} | "
                      f"{c['close_date']} | {c['hold_days']}日 | {c['avg_buy']:.2f} | "
                      f"{c['avg_sell']:.2f} | {money(c['pnl'])} |")
        md.append("")

    fl = flags(s)
    if fl:
        md += ["## 四、行为提示", ""]
        for tag, msg in fl:
            md.append(f"- **{tag}**：{msg}")
        md.append("")
    md += ["---", "", "*本报告由 a-share-trade-journal 生成，仅用于个人复盘，不构成投资建议。*"]
    return "\n".join(md)


# ── commands ──────────────────────────────────────────────────────────
def cmd_add(args) -> None:
    path = Path(args.file)
    rows = load(path)
    row = {
        "date": args.date or datetime.now().strftime("%Y-%m-%d"),
        "time": args.time or datetime.now().strftime("%H:%M:%S"),
        "code": str(args.code).zfill(6),
        "name": args.name or "",
        "side": args.side,
        "price": f"{args.price:.3f}".rstrip("0").rstrip("."),
        "qty": str(int(args.qty)),
        "commission": "", "stamp_duty": "", "transfer_fee": "",
        "strategy": args.strategy or "", "tag": args.tag or "", "note": args.note or "",
    }
    err = check_lot(row["code"], int(row["qty"]), row["side"])
    if err and not args.force:
        raise SystemExit(f"✗ {err}（确认无误可加 --force）")

    if row["side"] == "sell":
        held, sellable = position_of(rows, row["code"], row["date"])
        qty = int(row["qty"])
        if qty > held:
            if not args.force:
                raise SystemExit(f"✗ 卖出 {qty} 股超过 {row['code']} 当前持仓 {held} 股"
                                 f"（确认无误可加 --force）")
        elif qty > sellable and not args.force:
            raise SystemExit(f"✗ T+1：{row['date']} 可卖 {sellable} 股，"
                             f"当日买入的部分不可卖（持仓 {held} 股）")

    fill_fees(row)
    rows.append(row)
    rows.sort(key=lambda r: (r["date"], r.get("time") or "00:00:00"))
    save(path, rows)
    print(f"✓ 已记录 {row['date']} {row['side']} {row['code']} {row['name']} "
          f"{row['qty']}股 @ {row['price']}，费用 "
          f"{float(row['commission']) + float(row['stamp_duty']) + float(row['transfer_fee']):.2f} 元")
    print(f"  文件：{path.resolve()}")


def cmd_list(args) -> None:
    rows = load(Path(args.file))
    if not rows:
        print("（空）先用 add 记一笔，或跑 demo")
        return
    if args.cycles:
        closed, open_pos = build_cycles(rows)
        print(pad("代码", 9) + pad("名称", 12) + pad("策略", 12) + pad("建仓", 12)
              + pad("清仓", 12) + pad("持有", 6) + "盈亏")
        for c in sorted(closed, key=lambda x: x["close_date"]):
            print(pad(c["code"], 9) + pad(c["name"], 12) + pad(c["strategy"], 12)
                  + pad(c["open_date"], 12) + pad(c["close_date"], 12)
                  + pad(f"{c['hold_days']}日", 6) + money(c["pnl"]))
        for p in open_pos.values():
            print(pad(p["code"], 9) + pad(p["name"], 12) + pad(p["strategy"] or "-", 12)
                  + pad(p["open_date"], 12) + pad("持仓中", 12) + pad("-", 6)
                  + f"成本 {money(p['cost'] / p['qty'])}")
        return
    print(pad("日期", 12) + pad("代码", 9) + pad("名称", 12) + pad("方向", 6)
          + pad("价格", 10) + pad("数量", 8) + pad("策略", 12) + "备注")
    for r in rows:
        print(pad(r["date"], 12) + pad(r["code"], 9) + pad(r.get("name", ""), 12)
              + pad("买入" if r["side"] == "buy" else "卖出", 6)
              + pad(r["price"], 10) + pad(r["qty"], 8)
              + pad(r.get("strategy", ""), 12) + r.get("note", ""))


def cmd_stats(args) -> None:
    rows = load(Path(args.file))
    if not rows:
        raise SystemExit("✗ 没有数据，先跑 demo 或 add 记一笔")
    closed, open_pos = build_cycles(rows)
    if args.strategy:
        closed = [c for c in closed if c["strategy"] == args.strategy]
    s = compute_stats(rows, closed, open_pos)
    if args.json:
        out = {k: v for k, v in s.items() if k != "by_strategy"}
        out["by_strategy"] = {k: [c["pnl"] for c in v] for k, v in s["by_strategy"].items()}
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return
    print(render_stats(s, "A股交易日志 · 绩效概览" + (f"（{args.strategy}）" if args.strategy else "")))


def cmd_report(args) -> None:
    rows = load(Path(args.file))
    if not rows:
        raise SystemExit("✗ 没有数据")
    closed, open_pos = build_cycles(rows)
    s = compute_stats(rows, closed, open_pos)
    md = render_report(s, rows, closed)
    out = Path(args.out)
    out.write_text(md, encoding="utf-8")
    print(f"✓ 报告已写入 {out.resolve()}")


def cmd_validate(args) -> None:
    rows = load(Path(args.file))
    problems: list[str] = []
    for i, r in enumerate(rows, 1):
        try:
            int(float(r["qty"]))
            float(r["price"])
        except Exception:
            problems.append(f"第{i}行 数量/价格无法解析：{r}")
            continue
        err = check_lot(r["code"], int(float(r["qty"])), r["side"])
        if err:
            problems.append(f"第{i}行 {err}")
        if r["side"] == "sell" and float(r.get("stamp_duty") or 0) == 0:
            problems.append(f"第{i}行 卖出未计印花税")
    try:
        build_cycles(rows)
    except SystemExit as exc:
        problems.append(str(exc))
    if problems:
        print(f"发现 {len(problems)} 处问题：")
        for p in problems:
            print("  ✗", p)
    else:
        print(f"✓ {len(rows)} 笔成交，校验通过")


def cmd_demo(args) -> None:
    here = Path(__file__).resolve().parent.parent
    src = here / "examples" / "demo_fills.csv"
    dst = Path(args.file)
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    rows = load(dst)
    closed, open_pos = build_cycles(rows)
    s = compute_stats(rows, closed, open_pos)
    print(render_stats(s))
    print(f"  示例数据已写入 {dst.resolve()}\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="A股交易日志：成交级记账 → 持仓周期 → 绩效与行为分析")
    ap.add_argument("--file", default=str(DEFAULT_PATH), help="数据文件（默认 fills.csv）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="记录一笔成交")
    a.add_argument("--code", required=True)
    a.add_argument("--name", default="")
    a.add_argument("--side", required=True, choices=["buy", "sell"])
    a.add_argument("--price", required=True, type=float)
    a.add_argument("--qty", required=True, type=int)
    a.add_argument("--date", default=None)
    a.add_argument("--time", default=None)
    a.add_argument("--strategy", default="")
    a.add_argument("--tag", default="")
    a.add_argument("--note", default="")
    a.add_argument("--force", action="store_true", help="跳过整手等规则校验")
    a.set_defaults(func=cmd_add)

    l = sub.add_parser("list", help="列出成交或持仓周期")
    l.add_argument("--cycles", action="store_true")
    l.set_defaults(func=cmd_list)

    st = sub.add_parser("stats", help="绩效统计")
    st.add_argument("--strategy", default=None)
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_stats)

    rp = sub.add_parser("report", help="导出 Markdown 复盘报告")
    rp.add_argument("--out", default="复盘报告.md")
    rp.set_defaults(func=cmd_report)

    v = sub.add_parser("validate", help="数据校验")
    v.set_defaults(func=cmd_validate)

    d = sub.add_parser("demo", help="生成示例数据并分析")
    d.set_defaults(func=cmd_demo)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
