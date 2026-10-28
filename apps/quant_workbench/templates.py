TEMPLATES = {
    "买入持有": '''def initialize(context):
    context.bought = False

def handle_data(context, data):
    if not context.bought:
        for stock in context.universe:
            if stock in data:
                order_target_percent(stock, 0.95 / len(context.universe))
        context.bought = True
    record(总资产=context.portfolio.total_value)
''',
    "双均线": '''def initialize(context):
    context.fast = int(context.params.get("fast", 5))
    context.slow = int(context.params.get("slow", 20))

def handle_data(context, data):
    prices = history(context.slow, "close")
    for stock in context.universe:
        try:
            close = prices.xs(stock, level="instrument")["close"]
        except KeyError:
            continue
        if len(close) < context.slow:
            continue
        fast, slow = close.tail(context.fast).mean(), close.mean()
        weight = 0.95 / len(context.universe) if fast > slow else 0
        order_target_percent(stock, weight)
    record(总资产=context.portfolio.total_value)
''',
    "动量轮动": '''def initialize(context):
    context.lookback = int(context.params.get("lookback", 20))
    context.step = 0

def handle_data(context, data):
    context.step += 1
    if context.step % 5 != 1:
        return
    prices = history(context.lookback + 1, "close")
    scores = {}
    for stock, group in prices.groupby(level="instrument"):
        if len(group) >= context.lookback + 1 and stock in data:
            scores[stock] = group.close.iloc[-1] / group.close.iloc[0] - 1
    if not scores:
        return
    winner = max(scores, key=scores.get)
    for stock in context.universe:
        order_target_percent(stock, 0.95 if stock == winner else 0)
    log.info("本期选择 %s，动量 %.2f%%", winner, scores[winner] * 100)
    record(总资产=context.portfolio.total_value)
'''
}

API_HELP = '''
### 策略接口
```python
def initialize(context): ...       # 只调用一次，可保存自定义状态
def handle_data(context, data): ... # 每日开盘前调用
```
`context.current_dt` 是当前交易日；`context.params` 是参数字典，
`context.universe` 是标准化股票代码列表。
`context.portfolio` 包含 `cash`、`available_cash`、`total_value`、`positions`。
持仓对象包含 `total_amount`（实际股数）、`price` 和 `value`。

`data[股票代码].close` 等字段仅来自**上一交易日**；该日缺行情的股票不在 data 中。

```python
df = history(20, ["close", "volume"], ["000001.SZ"])
close = df.xs("SZ000001", level="instrument")["close"]
order_target_value("000001.SZ", 100000)
order_target_percent("000001.XSHE", 0.3)
log.info("当前资产 %s", context.portfolio.total_value)
record(仓位=0.3)
```
`history` 返回 `(datetime, instrument)` 双层索引 DataFrame，升序排列。
窗口按市场交易日计数，停牌/未上市日期不填零；历史不足时返回已有行。
支持 open/high/low/close/pre_close、volume（股）、amount（元）、
pe_ttm、pb、total_mv（万元）、turnover_rate（百分数）、adj_factor。
价格与成交量按可见截止日复权，估值缺失保留 NaN。

目标订单同日同股最后一次覆盖；未指定的已有持仓保留。
目标合计超过总资产会报错；下单只允许本次股票池。
函数提交的是目标，不保证成交；下一日开盘撮合、先卖后买、整数手、现金约束。
`record` 接受自定义名称的有限数值，重复名称同日保留最后一次。

这是精简 API，不提供 jqdata、run_daily 或财务查询 DSL。
Python 策略以本机权限执行，仅运行自己信任的代码。
'''

ASSUMPTIONS = "日线 · 前一日信号/当日开盘成交 · 收盘估值 · 仅做多 · 买入100股整数手 · 固定费率 · 无滑点和成交量冲击 · 不模拟涨跌停、历史ST及板块差异 · 复权收益口径"
