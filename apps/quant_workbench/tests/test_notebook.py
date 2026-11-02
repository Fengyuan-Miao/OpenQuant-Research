"""Check interactive observations against fixed, hand-calculable local data."""
import pandas as pd
import pytest

from apps.quant_workbench.data import scan
from apps.quant_workbench.notebook import ResearchEnvironment


@pytest.fixture
def market(tmp_path):
    directory = tmp_path / 'market'
    directory.mkdir()
    for day in range(1, 7):
        pd.DataFrame([dict(ts_code='000001.SZ', trade_date=f'2025090{day}',
                          open=10., high=12., low=9., close=11., vol=100.,
                          amount=110., adj_factor=2. if day >= 4 else 1.)]).to_parquet(
                              directory / f'2025090{day}.parquet')
    return directory


def prepare(market, tmp_path, **kwargs):
    return ResearchEnvironment(['000001.XSHE'], data_dir=market,
                               cache_dir=tmp_path / 'cache', **kwargs)


def test_history_visibility_and_units(market, tmp_path):
    env = prepare(market, tmp_path, trade_date='2025-09-04')
    prices = env.history(10, ['close', 'volume', 'amount'])
    assert prices.index.names == ['datetime', 'instrument']
    assert prices.index.get_level_values('datetime').max() == pd.Timestamp('2025-09-03')
    assert prices.close.tolist() == [11.] * 3  # day 4's factor is still invisible
    assert prices.volume.tolist() == [10000.] * 3
    assert prices.amount.tolist() == [110000.] * 3
    assert env.data['SZ000001'].close == 11.
    later = prepare(market, tmp_path, trade_date='2025-09-05')
    adjusted = later.history(10, ['close', 'volume'])
    assert adjusted.close.tolist() == [5.5, 5.5, 5.5, 11.]
    assert (adjusted.close * adjusted.volume).tolist() == [110000.] * 4


def test_orders_records_and_logs_print_without_fills(market, tmp_path, capsys):
    env = prepare(market, tmp_path)
    env.order_target_percent('000001.SZ', .2)
    env.order_target_value('SZ000001', 100000.)
    assert env.orders().target_weight.tolist() == [.1]
    assert env.context.portfolio.cash == 1000000.
    assert env.context.positions == {}
    env.record(signal=1)
    env.record(signal=2, price=11)
    assert env.records().signal.tolist() == [2.]
    env.log.info('practice %s', 'visible')
    output = capsys.readouterr().out
    assert '未成交' in output and '自定义指标' in output and 'practice visible' in output
    env.clear_orders()
    assert env.orders().empty
    with pytest.raises(ValueError):
        env.order_target_percent('000001.SZ', 1.1)


def test_recent_window_unreadable_file_and_cached_snapshot(market, tmp_path, capsys):
    (market / '20250907.parquet').write_bytes(b'partial download')
    env = prepare(market, tmp_path, history_days=3)
    assert env.context.current_dt == pd.Timestamp('2025-09-06')
    assert env.context.previous_dt == pd.Timestamp('2025-09-05')
    assert len(env.history(100, ['close'])) == 2
    assert '跳过 1' in capsys.readouterr().out
    same = prepare(market, tmp_path, history_days=3)
    assert env.snapshot_dir == same.snapshot_dir


def test_scan_range_is_inclusive(market):
    catalog = scan(market, start='2025-09-02', end='2025-09-04')
    assert [f['date'] for f in catalog['files']] == ['2025-09-02', '2025-09-03', '2025-09-04']
    assert len(scan(market)['files']) == 6
