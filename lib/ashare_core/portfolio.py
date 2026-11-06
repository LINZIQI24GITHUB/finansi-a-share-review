"""组合层：多标的收益矩阵、风险指标、权重优化。

原则与单标的技能一致：
- 收益用后复权序列（口径与 a-share-backtest 相同）；
- 所有统计都从同一份日收益矩阵算出来，不在画图时重算；
- 优化器失败时**降级并说明原因**，不给一个假的权重。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


# ── 数据 ──────────────────────────────────────────────────────────────
def returns_matrix(close: pd.DataFrame) -> pd.DataFrame:
    """date × code 的收盘价矩阵 → 日收益矩阵（去掉全空行）。"""
    ret = close.sort_index().pct_change()
    return ret.dropna(how="all")


# ── 指标 ──────────────────────────────────────────────────────────────
def portfolio_returns(weights: np.ndarray, ret: pd.DataFrame) -> pd.Series:
    w = np.asarray(weights, dtype=float)
    return ret.fillna(0.0).values @ w


def max_drawdown(equity: pd.Series) -> float:
    return float((equity / equity.cummax() - 1).min())


def risk_stats(weights, ret: pd.DataFrame, rf: float = 0.0) -> dict:
    w = np.asarray(weights, dtype=float)
    pr = pd.Series(portfolio_returns(w, ret), index=ret.index)
    eq = (1 + pr).cumprod()

    mean_d = float(pr.mean())
    std_d = float(pr.std(ddof=1)) if len(pr) > 1 else 0.0
    ann_ret = (1 + mean_d) ** TRADING_DAYS - 1
    ann_vol = std_d * np.sqrt(TRADING_DAYS)
    sharpe = (ann_ret - rf) / ann_vol if ann_vol else 0.0

    q05 = float(pr.quantile(0.05))
    tail = pr[pr <= q05]
    var95 = -q05
    cvar95 = -float(tail.mean()) if len(tail) else var95

    w_abs = np.abs(w)
    return {
        "ann_return": float(ann_ret),
        "ann_vol": float(ann_vol),
        "sharpe": float(sharpe),
        "max_dd": max_drawdown(eq),
        "var95": var95 * np.sqrt(1),          # 单日
        "cvar95": cvar95,
        "hhi": float(np.sum(w_abs ** 2)),
        "top3": float(np.sort(w_abs)[-3:].sum()) if len(w_abs) >= 3 else float(w_abs.sum()),
        "total_return": float(eq.iloc[-1] - 1),
        "n_days": int(len(pr)),
    }


def correlation(ret: pd.DataFrame) -> pd.DataFrame:
    return ret.corr()


# ── 优化 ──────────────────────────────────────────────────────────────
def _bounds(n: int, max_weight: float, min_weight: float):
    return [(min_weight, min(max_weight, 1.0))] * n


def equal_weights(n: int) -> np.ndarray:
    return np.repeat(1.0 / n, n)


def inverse_vol_weights(cov: np.ndarray) -> np.ndarray:
    iv = 1.0 / np.sqrt(np.diag(cov))
    return iv / iv.sum()


def min_variance(cov: np.ndarray, max_weight: float, min_weight: float) -> tuple[np.ndarray, str]:
    from scipy.optimize import minimize

    n = cov.shape[0]
    x0 = equal_weights(n)
    cons = ({"type": "eq", "fun": lambda w: w.sum() - 1.0},)
    res = minimize(lambda w: float(w @ cov @ w), x0, method="SLSQP",
                   bounds=_bounds(n, max_weight, min_weight), constraints=cons,
                   options={"maxiter": 500})
    if not res.success:
        return x0, f"最小方差未收敛（{res.message}），已退回等权"
    return res.x, ""


def risk_parity(cov: np.ndarray, max_weight: float, min_weight: float) -> tuple[np.ndarray, str]:
    from scipy.optimize import minimize

    n = cov.shape[0]
    x0 = inverse_vol_weights(cov)

    def obj(w):
        port_var = float(w @ cov @ w)
        if port_var <= 0:
            return 1e6
        mrc = cov @ w
        rc = w * mrc / np.sqrt(port_var)      # 风险贡献
        return float(np.sum((rc - rc.mean()) ** 2)) * 1e4

    res = minimize(obj, x0, method="SLSQP",
                   bounds=_bounds(n, max_weight, min_weight),
                   constraints=({"type": "eq", "fun": lambda w: w.sum() - 1.0},),
                   options={"maxiter": 500})
    if not res.success:
        w = inverse_vol_weights(cov)
        w = np.minimum(w, max_weight)
        return w / w.sum(), f"风险平价未收敛（{res.message}），已退回波动率倒数权重"
    return res.x, ""


def optimize(ret: pd.DataFrame, method: str = "minvar",
             max_weight: float = 0.4, min_weight: float = 0.0) -> tuple[np.ndarray, str]:
    n = ret.shape[1]
    if n == 0:
        raise ValueError("没有标的")
    if n == 1:
        return np.array([1.0]), "只有一只标的，权重为 1"

    cov = ret.cov().values * TRADING_DAYS
    if np.any(~np.isfinite(cov)):
        return equal_weights(n), "协方差矩阵含无效值（样本太短或含停牌），已退回等权"
    if n > len(ret) / 2:
        return inverse_vol_weights(cov) / inverse_vol_weights(cov).sum(), \
            f"标的数 {n} 相对样本 {len(ret)} 过多，均值方差不可靠，已退回波动率倒数权重"
    if max_weight * n < 1:
        return equal_weights(n), \
            f"约束不可行：单票上限 {max_weight:.0%} × {n} 只 = {max_weight*n:.0%} < 100%，已退回等权"

    if method == "equal":
        return equal_weights(n), ""
    if method == "minvar":
        return min_variance(cov, max_weight, min_weight)
    if method == "riskparity":
        return risk_parity(cov, max_weight, min_weight)
    return equal_weights(n), f"未知方法 {method}，已退回等权"


def rebalance_plan(codes: list[str], target: np.ndarray, current: dict[str, float],
                   capital: float) -> list[dict]:
    """把目标权重换算成股数（整手）与差额。current 为当前市值权重。"""
    from . import rules

    rows = []
    for code, w in zip(codes, target):
        cur_w = float(current.get(code, 0.0))
        rows.append({
            "code": code,
            "target_weight": float(w),
            "current_weight": cur_w,
            "delta_weight": float(w - cur_w),
            "delta_amount": float((w - cur_w) * capital),
            "lot": rules.min_order_qty(code),
        })
    return rows
