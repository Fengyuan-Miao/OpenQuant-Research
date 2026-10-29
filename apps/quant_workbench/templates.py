"""Standalone annotated strategies, loaded as source for the trusted-code editor."""
from pathlib import Path

STRATEGY_DIR = Path(__file__).with_name("strategies")
TEMPLATE_FILES = {
    "买入持有": "buy_and_hold.py",
    "双均线": "moving_average.py",
    "动量轮动": "momentum.py",
    "短期反转": "reversal.py",
    "布林带均值回归": "bollinger.py",
    "唐奇安通道突破": "donchian.py",
}
TEMPLATES = {name: (STRATEGY_DIR / file).read_text(encoding="utf-8")
             for name, file in TEMPLATE_FILES.items()}

TEMPLATE_LESSONS = {
    "买入持有": "第 1 课：一次等权建仓，理解 context、股票池和目标仓位。未成交部分不自动补单。",
    "双均线": "第 2 课：短均线高于长均线时持有，否则清仓。学习 history、groupby 和均值；震荡时可能反复交易。",
    "动量轮动": "第 3 课：定期买过去涨幅最高的股票。学习评分和排名；全部下跌时也会选相对强者。",
    "短期反转": "第 4 课：定期买过去收益最低的股票。与动量比较排名方向；持续下跌时反转可能不出现。",
    "布林带均值回归": "第 5 课：教学规则为跌破下轨持有、回到中轨退出。学习标准差和状态；下轨不保证反弹。",
    "唐奇安通道突破": "第 6 课：突破此前最高价持有、跌破此前最低价退出。学习窗口排除当前观察值；不包含完整海龟系统。",
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
