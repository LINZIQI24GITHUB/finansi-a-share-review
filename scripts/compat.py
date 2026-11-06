"""薄兼容层：把旧交易日志用到的四个工具函数映射到 ashare_core.rules。

只做转接，不重复实现规则——费率、最小申报量、涨跌停比例都以内核为准。
"""
from __future__ import annotations

import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
for candidate in (SKILL_DIR.parents[1] / "lib" if len(SKILL_DIR.parents) > 1 else None,
                  SKILL_DIR / "lib"):
    if candidate and Path(candidate).is_dir() and (Path(candidate) / "ashare_core").is_dir():
        sys.path.insert(0, str(candidate))
        break

from ashare_core import rules  # noqa: E402


def calc_fees(amount: float, side: str):
    """返回带 commission / stamp_duty / transfer_fee / total 的对象。"""
    return rules.calc_fees(amount, side)


def min_lot(code: str) -> int:
    return rules.min_order_qty(code)


def limit_pct(code: str, is_st: bool = False) -> float:
    return rules.limit_ratio(code, is_st)


def check_lot(code: str, qty: int, side: str = "buy") -> str | None:
    """与旧版签名一致：返回错误字符串或 None。"""
    if side == "sell":
        return None
    if qty <= 0:
        return "数量必须为正"
    need = rules.min_order_qty(code)
    if qty < need:
        return f"{code} 最小买入 {need} 股，实际 {qty} 股"
    step = rules.lot_step(code)
    if qty % step:
        return f"{code} 买入须为 {step} 股整数倍，实际 {qty} 股"
    return None
