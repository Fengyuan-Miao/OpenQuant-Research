"""Small JoinQuant-style API implemented on Qlib's actual executor/account."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import numpy as np
import pandas as pd

from qlib.backtest.decision import Order, TradeDecisionWO
from qlib.backtest.exchange import Exchange
from qlib.strategy.base import BaseStrategy

from .data import PRICE_FIELDS, symbol


class WorkbenchExchange(Exchange):
    def _get_buy_amount_by_cash_limit(self, trade_price, cash, cost_ratio):
        # Upstream divides by cost_ratio, which is undefined for zero-cost experiments.
        if cost_ratio == 0:
            return max(0, cash - self.min_cost) / trade_price
        return super()._get_buy_amount_by_cash_limit(trade_price, cash, cost_ratio)


class StrategyAdapter(BaseStrategy):
    def __init__(self, source, raw, config, progress=lambda *a: None, check_cancel=lambda: None, calendar=None):
        super().__init__()
        self.raw = raw.sort_index()
        self.config = config
        self.progress = progress
        self.check_cancel = check_cancel
        self.dates = pd.DatetimeIndex(calendar if calendar is not None else
                                      sorted(raw.index.get_level_values("datetime").unique()))
        self.run_dates = self.dates[(self.dates >= config["start"]) & (self.dates <= config["end"])]
        self.targets = {}
        self.trades, self.holdings, self.records = [], [], []
        self.log = logging.getLogger("strategy")
        self.context = SimpleNamespace(params=config.get("params", {}), universe=config["symbols"],
                                       current_dt=None, portfolio=None)
        namespace = {"__name__": "user_strategy", "history": self.history,
                     "order_target_value": self.order_target_value,
                     "order_target_percent": self.order_target_percent,
                     "record": self.record, "log": self.log}
        exec(compile(source, "strategy.py", "exec"), namespace)
        self.initialize = namespace.get("initialize", lambda context: None)
        self.handle_data = namespace.get("handle_data")
        if not callable(self.initialize) or not callable(self.handle_data):
            raise ValueError("策略必须定义 handle_data(context, data)，initialize(context) 可选")
        self.initialized = False

    def history(self, count, fields, securities=None):
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            raise ValueError("history 的 count 必须为正整数")
        fields = [fields] if isinstance(fields, str) else list(fields)
        allowed = set(PRICE_FIELDS + ["volume", "amount", "pe_ttm", "pb", "total_mv", "turnover_rate", "adj_factor"])
        if set(fields) - allowed or set(fields) - set(self.raw.columns):
            raise ValueError(f"不支持的行情字段：{set(fields) - (allowed & set(self.raw.columns))}")
        codes = self.context.universe if securities is None else securities
        codes = [codes] if isinstance(codes, str) else codes
        codes = [symbol(x) for x in codes]
        if set(codes) - set(self.context.universe):
            raise ValueError("history 只能访问本次股票池内证券")
        past = self.dates[self.dates < self.context.current_dt]
        if not len(past):
            return pd.DataFrame(columns=fields, index=pd.MultiIndex.from_arrays([[], []], names=["datetime", "instrument"]))
        index_dates = self.raw.index.get_level_values("datetime")
        visible = self.raw[(index_dates <= past[-1]) & self.raw.index.get_level_values("instrument").isin(codes)]
        cutoff_factors = visible.groupby(level="instrument").adj_factor.last()
        frame = visible[
            visible.index.get_level_values("datetime").isin(past[-count:])].copy()
        factors = frame.adj_factor / frame.index.get_level_values("instrument").map(cutoff_factors)
        for field in set(fields) & set(PRICE_FIELDS):
            frame[field] *= factors
        if "volume" in fields:
            frame["volume"] /= factors
        return frame[fields].copy()

    def order_target_value(self, security, value):
        code = symbol(security)
        value = float(value)
        if code not in self.context.universe:
            raise ValueError(f"{code} 不在股票池中")
        if not np.isfinite(value) or value < 0:
            raise ValueError("目标金额必须是非负有限数")
        self.targets[code] = value

    def order_target_percent(self, security, weight):
        weight = float(weight)
        if not np.isfinite(weight) or not 0 <= weight <= 1:
            raise ValueError("目标权重必须在 0 到 1 之间")
        self.order_target_value(security, self.context.portfolio.total_value * weight)

    def record(self, **values):
        row = {"datetime": self.context.current_dt}
        for key, value in values.items():
            if key == "datetime" or not np.isfinite(float(value)):
                raise ValueError("record 只接受有限数值，datetime 为保留字段")
            row[key] = float(value)
        self.records.append(row)

    def _latest(self, code, before):
        try:
            stock = self.raw.xs(code, level="instrument")
            return stock.loc[stock.index < before].iloc[-1]
        except (KeyError, IndexError):
            return None

    def _base_factor(self, code):
        return self.raw.xs(code, level="instrument").adj_factor.iloc[0]

    def generate_trade_decision(self, execute_result=None):
        self.check_cancel()
        start, end = self.trade_calendar.get_step_time()
        self.context.current_dt = start.normalize()
        position = self.trade_position
        positions = {}
        for code in position.get_stock_list():
            row = self._latest(code, self.context.current_dt)
            factor = row.adj_factor / self._base_factor(code)
            positions[code] = SimpleNamespace(total_amount=position.get_stock_amount(code) * factor,
                                             price=position.get_stock_price(code) / factor,
                                             value=position.get_stock_amount(code) * position.get_stock_price(code))
        self.context.portfolio = SimpleNamespace(cash=position.get_cash(), available_cash=position.get_cash(),
                                                total_value=position.calculate_value(), positions=positions)
        self.context.cash = self.context.portfolio.cash
        self.context.total_value = self.context.portfolio.total_value
        self.context.positions = positions
        self.targets = {}
        if not self.initialized:
            self.initialize(self.context)
            self.initialized = True
        previous = self.history(1, [f for f in ("open", "high", "low", "close", "volume", "amount") if f in self.raw])
        data = {code: SimpleNamespace(**row.to_dict()) for (_, code), row in previous.iterrows()}
        self.handle_data(self.context, data)
        # Holdings not mentioned by the strategy retain their existing allocation.
        desired_value = sum(self.targets.values()) + sum(p.value for c, p in positions.items() if c not in self.targets)
        if desired_value > self.context.total_value + max(0.01, self.context.total_value * 1e-8):
            raise ValueError("目标仓位与未调整持仓合计超过账户总资产（100%）")
        orders = []
        for code, target in self.targets.items():
            previous = self._latest(code, self.context.current_dt)
            if previous is None:
                self.trades.append({"datetime": start, "instrument": code, "status": "未成交", "reason": "没有历史价格"})
                self.log.warning("%s 无历史价格，跳过订单", code)
                continue
            factor = previous.adj_factor / self._base_factor(code)
            desired = target / (previous.close * factor)
            difference = desired - position.get_stock_amount(code)
            if abs(difference) < 1e-8:
                continue
            orders.append(Order(code, abs(difference), Order.BUY if difference > 0 else Order.SELL, start, end))
        orders.sort(key=lambda o: (o.direction, o.stock_id))
        return TradeDecisionWO(orders, self)

    def post_exe_step(self, execute_result):
        date = self.context.current_dt
        for order, value, cost, price in execute_result:
            try:
                row = self.raw.loc[(date, order.stock_id)]
                factor = row.adj_factor / self._base_factor(order.stock_id)
                paused = row.volume <= 0
            except KeyError:
                factor, paused = 1, True
            dealt = order.deal_amount
            status = "已成交" if np.isclose(dealt, order.amount) else ("部分成交" if dealt > 0 else "未成交")
            reason = "" if status == "已成交" else ("缺行情或零成交量" if paused else "资金、持仓或整数手限制")
            self.trades.append({"datetime": date, "instrument": order.stock_id,
                                "direction": "买入" if order.direction == Order.BUY else "卖出",
                                "requested": order.amount * factor, "amount": dealt * factor,
                                "price": price / factor if dealt else None,
                                "value": value, "cost": cost, "status": status, "reason": reason})
            if reason:
                self.log.info("%s %s：%s", order.stock_id, status, reason)
        for code in self.trade_position.get_stock_list():
            row = self._latest(code, date + pd.Timedelta(days=1))
            factor = row.adj_factor / self._base_factor(code)
            amount = self.trade_position.get_stock_amount(code)
            price = self.trade_position.get_stock_price(code)
            self.holdings.append({"datetime": date, "instrument": code, "amount": amount * factor,
                                  "price": price / factor, "value": amount * price})
        completed = int(self.run_dates.get_loc(date)) + 1
        self.progress(completed / len(self.run_dates), f"已完成 {completed}/{len(self.run_dates)} 个交易日 · {date:%Y-%m-%d}")
        self.check_cancel()


def run_backtest(source, snapshot_dir, config, progress=lambda *a: None, check_cancel=lambda: None):
    import qlib
    from qlib.backtest import backtest
    from .metrics import analyze

    raw = pd.read_parquet(snapshot_dir / "raw.parquet")
    qlib.init(provider_uri=str(snapshot_dir / "provider"), region="cn", kernels=1,
              expression_cache=None, dataset_cache=None)
    calendar = pd.to_datetime((snapshot_dir / "provider/calendars/day.txt").read_text().splitlines())
    strategy = StrategyAdapter(source, raw, config, progress, check_cancel, calendar)
    # Qlib's None benchmark becomes its default CSI300 in create_account_instance.
    # Supply an explicit zero series; compute an optional user benchmark ourselves.
    benchmark_zero = pd.Series(0.0, index=strategy.dates)
    exchange = WorkbenchExchange(freq="day", start_time=config["start"], end_time=config["end"],
                                 codes=config["symbols"], deal_price="$open",
                                 limit_threshold=("$paused", "$paused"), trade_unit=100,
                                 open_cost=config["open_cost"], close_cost=config["close_cost"],
                                 min_cost=config["min_cost"])
    portfolio, _ = backtest(start_time=config["start"], end_time=config["end"], strategy=strategy,
        executor={"class": "SimulatorExecutor", "module_path": "qlib.backtest.executor",
                  "kwargs": {"time_per_step": "day", "generate_portfolio_metrics": True}},
        benchmark=benchmark_zero, account=config["capital"],
        exchange_kwargs={"exchange": exchange})
    report, _ = portfolio["1day"]
    report = report.drop(columns=["bench"], errors="ignore")
    benchmark = None
    if config.get("benchmark"):
        stock = raw.xs(config["benchmark"], level="instrument").reindex(strategy.dates)
        benchmark = (stock.close * stock.adj_factor).pct_change(fill_method=None)
    report, stats = analyze(report, config["capital"], benchmark)
    tables = {"report": report.reset_index().rename(columns={"index": "datetime"}),
              "trades": pd.DataFrame(strategy.trades), "holdings": pd.DataFrame(strategy.holdings),
              "records": pd.DataFrame(strategy.records)}
    if not tables["records"].empty:
        tables["records"] = tables["records"].groupby("datetime", as_index=False).last()
    return tables, stats
