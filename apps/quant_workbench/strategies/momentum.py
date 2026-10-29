"""第 3 课：动量轮动。学习收益率、字典评分、排序、定期调仓。
参数示例：{"lookback": 20, "topk": 1, "rebalance": 5, "risk": 0.95}
每隔 rebalance 个交易日，等权买入过去 lookback 日收益最高的 topk 只。
这是相对强弱排名：即使全部股票下跌，也会选择跌得较少的股票。
"""


def initialize(context):
    context.lookback = int(context.params.get("lookback", 20))
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

    # N 日涨幅需要 N+1 个收盘价；history 已按可见截止日复权。
    prices = history(context.lookback + 1, "close")
    scores = {}
    for stock, group in prices.groupby(level="instrument"):
        if len(group) < context.lookback + 1 or stock not in data or data[stock].volume <= 0:
            continue
        close = group["close"]
        scores[stock] = close.iloc[-1] / close.iloc[0] - 1
    if not scores:
        log.info("历史不足或没有候选，本次保留原持仓")
        return

    # 分数相同时按代码排序，确保每次运行的结果确定。
    selected = sorted(scores, key=lambda stock: (-scores[stock], stock))[:context.topk]
    weight = context.risk / len(selected)
    for stock in context.universe:
        order_target_percent(stock, weight if stock in selected else 0)
    log.info("动量选股：%s", {stock: round(scores[stock], 4) for stock in selected})
    record(入选平均动量=sum(scores[stock] for stock in selected) / len(selected))
