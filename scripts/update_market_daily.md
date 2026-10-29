# 每日 A 股数据更新

`scripts/update_market_daily.py` 沿用 `temp/download.py` 的三个接口：日行情 `daily`、每日指标 `daily_basic`、复权因子 `adj_factor`。保留原下载脚本及历史下载进程。

默认目录是项目根目录下的 `data/market_daily/`，与现有下载脚本一致，不依赖启动时的工作目录。
工作台、历史下载脚本和每日更新任务统一使用此目录。下载后在工作台「数据管理」中刷新即可纳入新增文件；如果此前手动指定了其他目录，请改回此目录。

## 已配置的定时方式

容器内没有 cron，使用 Python 常驻任务。默认每天**北京时间 09:00**更新上一个交易日，周末、节假日通过交易所日历确定日期，不简单减一天。
例如周一取上周五；即使晚上手动执行，也只取严格早于当天的最近交易日。

```bash
# 在项目根目录启动后台任务（启动时先更新一次）
.venv/bin/python scripts/update_market_daily.py --start-scheduler

# 手动更新一次
.venv/bin/python scripts/update_market_daily.py

# 修改执行时间；同一目录已有定时进程时，先停止旧进程
.venv/bin/python scripts/update_market_daily.py --start-scheduler --at 09:30

# 补齐指定日期以来的数据，到上一个交易日为止
.venv/bin/python scripts/update_market_daily.py --start-date 20261001

# 查看日志（时间为北京时间）
tail -n 50 .qlib_workbench/daily_download/download.log

# 查看定时进程 PID，停止时对相应 PID 执行 kill
cat .qlib_workbench/daily_download/*.pid
```

后台进程不依赖 SSH 连接或 Streamlit，但**容器重启会结束进程**。重启后重新执行 `--start-scheduler`，或者将前台命令 `.venv/bin/python scripts/update_market_daily.py --schedule` 加入宿主机已有的容器启动/进程管理配置。当前未修改宿主机或 Docker 的启动设置。
重复启动同一目录的定时任务会因文件锁退出，不会启动两个下载循环。日志按日滚动保留14份。

## Token 与数据保护

Token 优先从 `TUSHARE_TOKEN` 环境变量读取，否则读取 `.qlib_workbench/secrets/tushare_token`。
本机文件权限设为600，父目录700，所在目录已被 Git 忽略。不要将 Token 写进源码或命令行参数。

- 有效 Parquet 已存在时直接跳过；不可读或字段不完整的文件重新下载、校验后替换。
- 请求分页获取；网络失败最多尝试4次，定时任务本轮失败后15分钟重试。
- 行情、每日指标、复权因子均就绪才生成文件；缺失估值数值保留空值，缺失复权因子拒绝写入。
- 临时文件写完、重新读取校验后原子替换，避免工作台读到写了一半的文件。
- 第一次执行只更新最近一个交易日；之后根据成功检查点自动补齐停机期间的交易日。更早的历史空缺用 `--start-date` 补齐。
- 文件锁协调此脚本的实例；原来的历史脚本不使用该锁，尽量避免主动让两者下载同一天。
- 保留原始单位（成交量为手、成交额为千元），工作台接入时负责转换。
- 当前下载 A 股股票，不包含 ETF；ETF 需要另外接入基金数据接口。

接口说明：[交易日历](https://tushare.pro/document/2?doc_id=26)、[日行情](https://tushare.pro/document/2?doc_id=27)、[每日指标](https://tushare.pro/document/2?doc_id=32)、[复权因子](https://tushare.pro/document/2?doc_id=28)。

测试：`.venv/bin/python -m pytest scripts/tests/test_update_market_daily.py -q`
