"""Small interactive practice environment using the workbench's actual history API.

This is a single-day debugger. Orders are displayed, not filled, and cash stays fixed.
Recreate the environment to change its date or reset user-defined context state.
"""
from copy import deepcopy
import logging
from pathlib import Path
import sys
from types import SimpleNamespace

import pandas as pd

from .data import scan, freeze_manifest, snapshot, symbol
from .engine import StrategyAdapter
from .store import validate

ROOT = Path(__file__).resolve().parents[2]


class ResearchEnvironment:
    def __init__(self, symbols, *, trade_date=None, history_days=60, capital=1000000.,
                 params=None, data_dir=None, cache_dir=None):
        if isinstance(history_days, bool) or not isinstance(history_days, int) or history_days < 1:
            raise ValueError('history_days 必须为正整数')
        directory = Path(data_dir or ROOT / 'data/market_daily').expanduser().resolve()
        codes = [symbol(code) for code in symbols]
        if not codes:
            raise ValueError('请至少指定一只股票')
        cutoff = pd.Timestamp(trade_date).strftime('%Y%m%d') if trade_date is not None else None
        # Enumerate names cheaply, and only inspect the small requested date window.
        dates = sorted(p.stem for p in directory.glob('*.parquet')
                       if len(p.stem) == 8 and p.stem.isdigit() and (cutoff is None or p.stem <= cutoff))
        if not dates:
            raise ValueError(f'指定日期之前没有行情：{directory}')
        earliest = dates[max(0, len(dates) - history_days - 1)]
        catalog = scan(directory, start=earliest, end=cutoff)
        if not catalog['segments']:
            raise ValueError('所选窗口没有完整行情文件')
        segment = catalog['segments'][-1]
        day = str(pd.Timestamp(trade_date).date()) if trade_date is not None else segment[-1]
        source = 'def handle_data(context, data): pass'
        config = validate(source, dict(start=day, end=day, symbols=codes, capital=capital,
            params=deepcopy(params or {}), benchmark=None, open_cost=.0005, close_cost=.0015, min_cost=5.))
        manifest = freeze_manifest(catalog, day, day, codes)
        self.snapshot_dir = snapshot(manifest, Path(cache_dir or ROOT / '.qlib_workbench/notebooks/snapshots'))
        raw = pd.read_parquet(self.snapshot_dir / 'raw.parquet')
        calendar = pd.to_datetime(manifest['dates'])
        self.adapter = StrategyAdapter(source, raw, config, calendar=calendar)
        self.context = self.adapter.context
        self.context.current_dt = pd.Timestamp(day)
        self.context.previous_dt = calendar[calendar < day][-1]
        self.context.portfolio = SimpleNamespace(cash=config['capital'], available_cash=config['capital'],
            total_value=config['capital'], positions={})
        self.context.cash = self.context.portfolio.cash
        self.context.total_value = self.context.portfolio.total_value
        self.context.positions = self.context.portfolio.positions
        logger = logging.getLogger(f'qlib.notebook.{id(self)}')
        logger.setLevel(logging.INFO)
        logger.propagate = False
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter('%(levelname)s %(message)s'))
        logger.addHandler(handler)
        self.log = logger
        frame = self.history(1, ['open', 'high', 'low', 'close', 'volume', 'amount'])
        self.data = {code: SimpleNamespace(**row.to_dict()) for (_, code), row in frame.iterrows()}
        print(f'练习环境就绪 | 模拟交易日：{day} | 最新可见行情：{self.context.previous_dt.date()}')
        print(f'股票池：{", ".join(self.context.universe)} | 可查询历史：{len(calendar) - 1} 个市场交易日')
        print('下单仅显示目标，不撮合、不扣款；重新运行准备单元格会重置环境。')
        if catalog['errors']:
            print(f'本次跳过 {len(catalog["errors"])} 个不可读文件。')

    def history(self, count, fields, securities=None):
        return self.adapter.history(count, fields, securities)

    def order_target_value(self, security, value):
        self.adapter.order_target_value(security, value)
        code = symbol(security)
        print(f'[练习委托，未成交] {code} 目标市值：{self.adapter.targets[code]:,.2f} 元')

    def order_target_percent(self, security, weight):
        self.adapter.order_target_percent(security, weight)
        code = symbol(security)
        print(f'[练习委托，未成交] {code} 目标仓位：{float(weight):.2%}；目标市值：{self.adapter.targets[code]:,.2f} 元')

    def record(self, **values):
        self.adapter.record(**values)
        print('[自定义指标]', values)

    def orders(self):
        return pd.DataFrame([{'instrument': code, 'target_value': value,
                              'target_weight': value / self.context.portfolio.total_value}
                             for code, value in self.adapter.targets.items()],
                            columns=['instrument', 'target_value', 'target_weight'])

    def records(self):
        if not self.adapter.records:
            return pd.DataFrame(columns=['datetime'])
        return pd.DataFrame(self.adapter.records).groupby('datetime', as_index=False).last()

    def clear_orders(self):
        self.adapter.targets.clear()
        print('练习委托已清空。')
