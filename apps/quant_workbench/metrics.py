"""Account-based, net-of-cost performance statistics."""
import numpy as np
import pandas as pd


def analyze(report: pd.DataFrame, initial: float, benchmark: pd.Series | None = None):
    result = report.copy()
    result["nav"] = result["account"] / initial
    previous = result.account.shift(1, fill_value=initial)
    returns = result.account / previous - 1
    result["daily_return"] = returns
    result["drawdown"] = result.nav / result.nav.cummax().clip(lower=1) - 1
    total = float(result.nav.iloc[-1] - 1)
    std = float(returns.std(ddof=1))
    stats = {"total_return": total, "annual_return": (1 + total) ** (252 / len(result)) - 1,
             "volatility": std * np.sqrt(252) if np.isfinite(std) else None,
             "sharpe": float(returns.mean() / std * np.sqrt(252)) if std > 1e-12 else None,
             "max_drawdown": float(result.drawdown.min()), "trading_days": len(result)}
    if benchmark is not None:
        aligned = benchmark.reindex(result.index)
        if aligned.isna().any():
            raise ValueError("基准在回测区间存在缺失行情")
        result["benchmark_daily_return"] = aligned
        result["benchmark_nav"] = (1 + aligned).cumprod()
        stats["benchmark_return"] = float(result.benchmark_nav.iloc[-1] - 1)
        stats["excess_return"] = total - stats["benchmark_return"]
    return result, stats
