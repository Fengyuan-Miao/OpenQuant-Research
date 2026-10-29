"""第 1 课：买入持有。学习初始化、股票池、目标仓位和账户记录。
参数示例：{"risk": 0.95}
第一次有候选股票时等权提交一次买单，此后不再主动调仓。
未成交订单不会自动补单；可对照成交记录学习现金、手数和停牌限制。
"""


def initialize(context):
    context.risk = float(context.params.get("risk", 0.95))
    assert 0 < context.risk <= 1, "risk 必须在 (0, 1] 内"
    context.submitted = False


def handle_data(context, data):
    # record 在每日决策前执行，记录的不是当日收盘后的资产。
    record(决策前资产=context.portfolio.total_value)
    if context.submitted:
        return

    # data 只包含上一交易日有行情的股票；volume 是成交股数。
    candidates = [stock for stock in context.universe
                  if stock in data and data[stock].volume > 0]
    if not candidates:
        log.info("没有可选股票，等待下一个交易日")
        return

    weight = context.risk / len(candidates)
    for stock in context.universe:
        # 这是目标占比，不是本次额外买入的占比。
        order_target_percent(stock, weight if stock in candidates else 0)

    context.submitted = True
    log.info("一次性建仓：%s，每只目标仓位 %.2f%%", candidates, weight * 100)
