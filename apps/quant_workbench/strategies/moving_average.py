"""第 2 课：双均线择时。学习 history、groupby、均值和条件判断。
参数示例：{"fast": 5, "slow": 20, "risk": 0.95}
短均线高于长均线时持有，否则清仓；这是均线状态规则，不要求当天刚好金叉。
每日调整到股票池固定等权份额，未触发的份额留作现金。
"""


def initialize(context):
    context.fast = int(context.params.get("fast", 5))
    context.slow = int(context.params.get("slow", 20))
    context.risk = float(context.params.get("risk", 0.95))
    assert 1 <= context.fast < context.slow, "要求 1 <= fast < slow"
    assert 0 < context.risk <= 1, "risk 必须在 (0, 1] 内"
    context.long = dict.fromkeys(context.universe, False)


def handle_data(context, data):
    prices = history(context.slow, "close")
    for stock, group in prices.groupby(level="instrument"):
        # 历史不足或昨天没有有效成交时，保留之前的信号状态。
        if len(group) < context.slow or stock not in data or data[stock].volume <= 0:
            continue
        close = group["close"]
        fast_mean = close.tail(context.fast).mean()
        slow_mean = close.mean()
        new_signal = fast_mean > slow_mean
        if new_signal != context.long[stock]:
            log.info("%s 短均线 %.2f，长均线 %.2f，持有信号 %s",
                     stock, fast_mean, slow_mean, new_signal)
        context.long[stock] = new_signal

    # 始终提交完整目标组合，避免部分调整与剩余持仓合计超过 100%。
    weight = context.risk / len(context.universe)
    for stock in context.universe:
        order_target_percent(stock, weight if context.long[stock] else 0)
    record(持有信号数=sum(context.long.values()), 决策前资产=context.portfolio.total_value)
