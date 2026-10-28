# Qlib 策略工作台

本机、单用户的中文 Streamlit 工作台。使用 Qlib 的 Exchange、SimulatorExecutor 和账户系统执行真实日线回测，提供聚宽风格的精简 Python 策略接口。

## 安装和启动

在仓库根目录执行，建议 Python 3.10 及以上：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[ui]'
streamlit run apps/quant_workbench/app.py
```

打开 <http://127.0.0.1:8501>。根目录 `.streamlit/config.toml` 设置本机监听和浅色主题；从其他目录启动时应显式加 `--server.address 127.0.0.1`。
Qlib 源码安装需要 C++ 编译器以构建现有 Cython 扩展。UI 依赖是可选依赖，不影响原有命令行入口。

1. 在「我的策略」选择买入持有、双均线或动量轮动模板，创建策略。
2. 在编辑页修改代码、保存，设置日期、股票池、本金和费用，点击「运行回测」。
3. 查看进度、停止任务，完成后查看净值、每日持仓、成交、日志及自定义指标。
4. 「回测历史」可打开旧结果或复制代码和配置；复制后运行会使用当时重新扫描的数据。
5. 「数据管理」中刷新正在下载的行情文件，无需重启服务。

代码编辑器自动同步；网络较慢时待右上角运行指示结束再提交。普通文本编辑器可用于无障碍操作或嵌入式编辑器不可用的浏览器。

## 本地数据与持久化

默认行情目录为 `db/data/market_daily/`，可在界面修改或设置 `QLIB_WORKBENCH_DATA`。
输入是每日一个 `YYYYMMDD.parquet`，包含 Tushare 字段：

| 字段 | 单位/约定 |
| --- | --- |
| ts_code、trade_date | 股票代码、YYYYMMDD |
| open、high、low、close | 未复权价格，元；必需且大于零 |
| vol、amount | 手、千元；必需且非负 |
| adj_factor | 正数复权因子；必需 |
| pre_close | 可选，昨收价 |
| pe_ttm、pb、total_mv、turnover_rate | 可选估值数据，缺失保留 NaN；市值万元、换手率百分数 |

扫描跳过暂不可读/正在写入的文件。回测固定内容 SHA-256 清单，构建只含本次股票池、基准和所需日期的 Qlib 快照；文件在构建前改变会报错，重新提交即可。原始文件不被修改。
历史读取范围为所选数据段起点到回测结束，允许策略选择任意历史窗口，内存和准备时间随股票池及区间增加。

相邻文件日期间隔超过 20 个自然日时分段，单次回测不能跨段；这只是长缺口检测，不保证所有交易日齐全。数据补齐后刷新会重新分段。首次回测需要至少一个早于开始日的行情日。默认选择最新段的最近 60 个行情日。

运行数据保存在 `.qlib_workbench/`（已忽略版本控制），或由 `QLIB_WORKBENCH_HOME` 指定：

- `index.sqlite`：策略、任务、状态和进度。
- `runs/<id>/`：源码、参数、数据清单、日志、Parquet 结果和 JSON 指标。
- `snapshots/<hash>/`：固定原始行情表及 Qlib 二进制数据，可复用。

一次运行一个任务；重新加载页面不会重复提交。进程异常退出会在下一次查询时标为失败。停止先发取消请求，5 秒后强制终止任务进程组。取消或失败的结果不展示为完整回测。删除策略保留其历史任务。
CSV/ZIP 可导出收益、持仓、成交、日志和源码参数；ZIP 不包含体积较大的行情快照，原始快照在本地保留。

## 策略接口

```python
def initialize(context):
    context.lookback = int(context.params.get("lookback", 20))

def handle_data(context, data):
    df = history(context.lookback, ["close", "volume"], ["000001.SZ"])
    prices = df.xs("SZ000001", level="instrument").close
    if len(prices) >= context.lookback:
        weight = 0.5 if prices.iloc[-1] > prices.mean() else 0
        order_target_percent("000001.XSHE", weight)
    log.info("资产：%.2f", context.portfolio.total_value)
    record(资产=context.portfolio.total_value)
```

| 接口 | 行为 |
| --- | --- |
| initialize(context) | 可选；第一次交易日前调用一次，context 可保存自定义属性 |
| handle_data(context, data) | 必需；每日开盘前调用，data 仅含上一交易日有行情的股票 |
| history(count, fields, securities=None) | 正整数窗口、字段名或字段列表；默认股票池，返回升序 `(datetime, instrument)` 双层索引 DataFrame |
| order_target_value(security, value) | 非负目标金额；同股同日最后一次覆盖 |
| order_target_percent(security, weight) | 0 到 1 的目标仓位，按决策时总资产换算金额 |
| record(**values) | 记录有限数值；同日同名保留最后一个值 |
| log.info/warning/error | 标准 logging 格式及参数，写入本次日志 |

`context.current_dt` 为交易日，`context.params` 为界面填写的 JSON 对象，`context.universe` 为标准化股票池。`context.portfolio` 包含 `cash`、`available_cash`、`total_value`、`positions`；持仓提供实际股数 `total_amount`、价格 `price`、市值 `value`。另提供 `context.cash/total_value/positions` 快捷属性。

代码支持 `SZ000001`、`000001.SZ`、`000001.XSHE` 等形式，接口结果统一采用 Qlib 形式。只能查询和交易本次股票池，基准不会自动加入股票池。

历史字段：open/high/low/close/pre_close、volume（股）、amount（元）、adj_factor，以及输入中存在的估值字段。history 截止上一交易日，价格按可见的最新复权因子调整，成交量反向调整，金额保持不变；不对停牌、未上市和缺失估值填零。请求不存在的字段会报错。窗口按行情日历计算，历史不足时返回已有行。

## 回测假设和指标

- 策略只使用上一交易日及更早的数据；当日开盘撮合、收盘估值。目标数量按前一可见价格估计，开盘跳空可能导致偏离目标仓位。
- 先卖后买，仅做多，不融资；目标加未调整持仓合计不得超过总资产。同日每只股票只形成一笔净调整订单，不能当日往返交易。
- 简化统一采用买入 100 股整数手、卖出清仓允许零股；不刻画板块差异。现金、手续费、持仓和交易手数由 Qlib 约束，无法成交的订单当日失效。
- 没有行情或成交量为零不交易。持有证券缺行情时沿用 Qlib 的已有估值，退市清算未单独建模。
- 默认买入费率 0.0005、卖出费率 0.0015、最低费用 5 元，是可编辑的固定回测假设，不是历史税费表。零费率也可运行。
- 无滑点、冲击及成交量容量约束，不模拟准确涨跌停、历史 ST 或新股特殊规则。
- 使用复权收益口径，复权持仓映射至实际股数；分红/拆股不单列现金流水。该口径不能替代逐笔公司行动账本。
- 默认无基准。选择基准时必须有完整对齐的本地价格及前一交易日数据；不会自动生成指数行情。
- 累计收益按扣费后净资产/初始资金计算；年化按 252 个交易日复利；波动与夏普用日收益样本标准差，无风险利率为零。
- 回撤峰值包含初始净值 1；零波动或样本不足时夏普显示不可计算；超额收益为策略累计收益减基准累计收益。

这是精简接口，不提供完整 jqdata、财务查询 DSL、分钟线、实盘或模型训练入口。子进程用于任务控制，不是安全沙箱；只运行本人可信策略，不要将服务公开给不可信用户。

## 验证

```bash
python -m pip install pytest
python -m pytest tests/workbench -q
```

测试使用临时目录和可手算行情，覆盖实际 Qlib 成交、费用、复权、数据快照、任务恢复/取消、导出与 Streamlit 表单，不下载外部数据。
