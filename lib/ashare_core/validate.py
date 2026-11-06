"""样本外验证：滚动切分，比较样本内与样本外表现。

为什么需要它：任何在单一区间上调出来的参数，都会「刚好」适应那段历史。
把区间切成连续的训练/测试段，只在训练段看表现、在紧邻的测试段验证，
才能回答「这套参数是真的有边际，还是拟合了噪声」。
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from . import engine, metrics


@dataclass
class Fold:
    index: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    is_return: float
    oos_return: float
    is_sharpe: float
    oos_sharpe: float
    is_trades: int
    oos_trades: int


def walk_forward(df: pd.DataFrame, sig: pd.Series, code: str,
                 train: int = 252, test: int = 63, cash: float = 100_000.0) -> list[Fold]:
    """滚动切分：训练窗口定长，测试窗口紧邻其后，按 test 步长向前滚动。"""
    df = df.reset_index(drop=True)
    sig = pd.Series(sig).reset_index(drop=True)
    folds: list[Fold] = []
    i, k = 0, 0
    while i + train + test <= len(df):
        tr = df.iloc[i:i + train]
        te = df.iloc[i + train:i + train + test]
        r_tr = engine.run(tr, sig.iloc[i:i + train], code, cash=cash)
        r_te = engine.run(te, sig.iloc[i + train:i + train + test], code, cash=cash)
        m_tr = metrics.compute(r_tr.equity, r_tr.position, r_tr.account.trades_df(), r_tr.account.fees_paid)
        m_te = metrics.compute(r_te.equity, r_te.position, r_te.account.trades_df(), r_te.account.fees_paid)
        folds.append(Fold(
            index=k,
            train_start=str(tr["date"].iloc[0]), train_end=str(tr["date"].iloc[-1]),
            test_start=str(te["date"].iloc[0]), test_end=str(te["date"].iloc[-1]),
            is_return=m_tr["total_return"], oos_return=m_te["total_return"],
            is_sharpe=m_tr["sharpe"], oos_sharpe=m_te["sharpe"],
            is_trades=m_tr["trades"], oos_trades=m_te["trades"],
        ))
        k += 1
        i += test
    return folds


def verdict(folds: list[Fold]) -> tuple[str, str]:
    """返回 (结论, 理由)。结论只用样本外的表现说话。"""
    if not folds:
        return "样本不足", "区间太短，至少需要「训练窗口 + 测试窗口」两段"
    oos = [f.oos_return for f in folds]
    oos_sharpe = [f.oos_sharpe for f in folds]
    is_sharpe = [f.is_sharpe for f in folds]
    pos = sum(1 for x in oos if x > 0)
    mean_is = sum(is_sharpe) / len(is_sharpe)
    mean_oos = sum(oos_sharpe) / len(oos_sharpe)

    if pos == 0:
        return "样本外失效", f"{len(folds)} 段测试期全部亏损，训练段的收益完全没有延续"
    if mean_oos <= 0 and pos < len(folds) / 2:
        return "样本外失效", f"样本外夏普均值 {mean_oos:.2f} ≤ 0，且只有 {pos}/{len(folds)} 段为正"
    if mean_oos <= 0:
        return "不稳定", (f"{pos}/{len(folds)} 段样本外为正，但均值被少数大亏拉成 {mean_oos:.2f}"
                        f"——收益集中在个别段，不能当作稳定边际")
    if mean_is > 0 and mean_oos / mean_is < 0.5:
        return "疑似过拟合", f"样本外夏普仅为样本内的 {mean_oos/mean_is:.0%}（{mean_oos:.2f} vs {mean_is:.2f}）"
    if pos <= len(folds) / 2:
        return "不稳定", f"{len(folds)} 段里只有 {pos} 段样本外为正，胜率不过半"
    if mean_is > 0 and mean_oos / mean_is >= 0.7:
        return "相对稳健", f"样本外/样本内夏普 = {mean_oos/mean_is:.0%}，{pos}/{len(folds)} 段为正"
    return "存疑", f"样本外/样本内夏普 = {mean_oos/mean_is:.0%}，介于稳健与过拟合之间"
