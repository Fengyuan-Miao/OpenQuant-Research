"""第 5 课：布林带均值回归。学习标准差、阈值和持有状态。
参数示例：{"window": 20, "num_std": 2.0, "risk": 0.95}
教学规则：收盘跌破下轨后持有；回到中轨或以上清仓；中间区域延续信号。
触及下轨本身不是必然的买入信号，本例用于检验“偏离均值会回归”的假设。
"""


def initialize(context):
    context.window = int(context.params.get("window", 20))
    context.num_std = float(context.params.get("num_std", 2.0))
    context.risk = float(context.params.get("risk", 0.95))
    assert context.window >= 2, "window 必须 >= 2"
    assert 0 < context.num_std < float("inf"), "num_std 必须为正有限数"
    assert 0 < context.risk <= 1, "risk 必须在 (0, 1] 内"
    context.long = dict.fromkeys(context.universe, False)


def handle_data(context, data):
    prices = history(context.window, "close")
    for stock, group in prices.groupby(level="instrument"):
        if len(group) < context.window or stock not in data or data[stock].volume <= 0:
            continue
        close = group["close"]
        middle = close.mean()
        # 使用总体标准差 ddof=0，明确不同软件可能使用不同约定。
        deviation = close.std(ddof=0)
        lower = middle - context.num_std * deviation
        last = close.iloc[-1]
        was_long = context.long[stock]
        if last >= middle:
            context.long[stock] = False
        elif deviation > 0 and last < lower:
            context.long[stock] = True
        # 既没跌破下轨，也没回到中轨时，不改信号。
        if context.long[stock] != was_long:
            log.info("%s 收盘 %.2f，下轨 %.2f，中轨 %.2f，持有信号 %s",
                     stock, last, lower, middle, context.long[stock])

        # 只画股票池第一只的指标，避免多只股票覆盖同名记录。
        if stock == context.universe[0]:
            record(昨日收盘=last, 中轨=middle, 下轨=lower,
                   上轨=middle + context.num_std * deviation)

    weight = context.risk / len(context.universe)
    for stock in context.universe:
        order_target_percent(stock, weight if context.long[stock] else 0)
    record(持有信号数=sum(context.long.values()), 决策前资产=context.portfolio.total_value)
