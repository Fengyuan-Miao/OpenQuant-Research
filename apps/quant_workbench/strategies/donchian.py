"""第 6 课：唐奇安通道突破。学习滚动高低点、窗口切片和趋势持有。
参数示例：{"entry": 20, "exit": 10, "risk": 0.95}
昨日收盘突破此前 entry 日最高价后持有；跌破此前 exit 日最低价后清仓。
比较窗口必须排除被比较的昨日行情。这是通道教学版，不是完整海龟系统。
"""


def initialize(context):
    context.entry = int(context.params.get("entry", 20))
    context.exit = int(context.params.get("exit", 10))
    context.risk = float(context.params.get("risk", 0.95))
    assert 1 <= context.exit <= context.entry, "要求 1 <= exit <= entry"
    assert 0 < context.risk <= 1, "risk 必须在 (0, 1] 内"
    context.long = dict.fromkeys(context.universe, False)


def handle_data(context, data):
    prices = history(context.entry + 1, ["high", "low", "close"])
    for stock, group in prices.groupby(level="instrument"):
        if len(group) < context.entry + 1 or stock not in data or data[stock].volume <= 0:
            continue
        last = group["close"].iloc[-1]
        # iloc[:-1] 去掉昨日这行；否则昨日收盘不可能超过包含自身的最高价。
        previous = group.iloc[:-1]
        upper = previous["high"].tail(context.entry).max()
        lower = previous["low"].tail(context.exit).min()
        was_long = context.long[stock]
        if last > upper:
            context.long[stock] = True
        elif last < lower:
            context.long[stock] = False
        if context.long[stock] != was_long:
            log.info("%s 收盘 %.2f，入场上轨 %.2f，退出下轨 %.2f，持有信号 %s",
                     stock, last, upper, lower, context.long[stock])
        if stock == context.universe[0]:
            record(昨日收盘=last, 入场上轨=upper, 退出下轨=lower)

    weight = context.risk / len(context.universe)
    for stock in context.universe:
        order_target_percent(stock, weight if context.long[stock] else 0)
    record(持有信号数=sum(context.long.values()), 决策前资产=context.portfolio.total_value)
