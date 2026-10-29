"""第 4 课：短期反转。与动量策略比较，只改变排名方向和时间窗口。
参数示例：{"lookback": 5, "topk": 1, "rebalance": 5, "risk": 0.95}
每隔 rebalance 个交易日，等权买入过去 lookback 日收益最低的 topk 只。
若全部上涨，会买涨得最少的；这只是反转假设，持续下跌时可能继续亏损。
"""


def initialize(context):
    context.lookback = int(context.params.get("lookback", 5))
    context.topk = int(context.params.get("topk", 1))
    context.rebalance = int(context.params.get("rebalance", 5))
    context.risk = float(context.params.get("risk", 0.95))
    assert min(context.lookback, context.topk, context.rebalance) >= 1, "窗口、持股数、调仓间隔必须 >= 1"
    assert 0 < context.risk <= 1, "risk 必须在 (0, 1] 内"
    context.step = 0


def handle_data(context, data):
    record(决策前资产=context.portfolio.total_value)
    context.step += 1
    if (context.step - 1) % context.rebalance != 0:
        return

    prices = history(context.lookback + 1, "close")
    scores = {}
    for stock, group in prices.groupby(level="instrument"):
        if len(group) < context.lookback + 1 or stock not in data or data[stock].volume <= 0:
            continue
        close = group["close"]
        # 负号将“跌得更多”变成“评分更高”。
        scores[stock] = -(close.iloc[-1] / close.iloc[0] - 1)
    if not scores:
        log.info("历史不足或没有候选，本次保留原持仓")
        return

    selected = sorted(scores, key=lambda stock: (-scores[stock], stock))[:context.topk]
    weight = context.risk / len(selected)
    for stock in context.universe:
        order_target_percent(stock, weight if stock in selected else 0)
    log.info("反转选股：%s", {stock: round(scores[stock], 4) for stock in selected})
    record(入选平均反转分数=sum(scores[stock] for stock in selected) / len(selected))
