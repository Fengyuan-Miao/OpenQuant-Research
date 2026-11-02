"""Point-in-time local universe selection and bounded warm-up data."""
import hashlib

import pandas as pd
import pytest

from apps.quant_workbench.data import scan, local_a_universe, freeze_manifest


@pytest.fixture
def market(tmp_path):
    directory = tmp_path / 'market'
    directory.mkdir()
    dates = pd.bdate_range('2024-12-16', periods=20)
    for i, date in enumerate(dates):
        # A delisted/absent later name must not be removed retroactively; new entrants stay out.
        codes = ['600000.SH', '920001.BJ', '510300.SH', '200001.SZ', '000001.SZ' if i < 12 else '000002.SZ']
        pd.DataFrame([dict(ts_code=code, trade_date=f'{date:%Y%m%d}', open=10., high=11., low=9.,
                           close=10., vol=1000., amount=100., adj_factor=1.) for code in codes]).to_parquet(
                               directory / f'{date:%Y%m%d}.parquet')
    return directory, dates


def test_pool_is_from_before_start_not_latest_survivors(market):
    directory, dates = market
    pool = local_a_universe(scan(directory), dates[12])
    assert pool['asof'] == str(dates[11].date())
    assert pool['symbols'] == ['BJ920001', 'SH600000', 'SZ000001']
    assert pool['sha256'] == hashlib.sha256((directory / f'{dates[11]:%Y%m%d}.parquet').read_bytes()).hexdigest()
    later = local_a_universe(scan(directory), dates[15])
    assert later['symbols'] == ['BJ920001', 'SH600000', 'SZ000002']


def test_new_download_does_not_change_old_pool_and_incomplete_file_is_skipped(market):
    directory, dates = market
    start = dates[12]
    before = local_a_universe(scan(directory), start)
    (directory / f'{dates[11]:%Y%m%d}.parquet').write_bytes(b'partial')
    after = local_a_universe(scan(directory), start)
    assert after['asof'] == str(dates[10].date())
    assert after['symbols'] == before['symbols']
    assert len(scan(directory)['errors']) == 1
    with pytest.raises(ValueError, match='开始前'):
        local_a_universe(scan(directory), dates[0])


def test_warmup_limits_only_prefix_and_preserves_run_dates(market):
    directory, dates = market
    catalog = scan(directory)
    first, last = str(dates[12].date()), str(dates[16].date())
    codes = local_a_universe(catalog, first)['symbols']
    manifest = freeze_manifest(catalog, first, last, codes, history_days=5)
    assert manifest['dates'] == [str(day.date()) for day in dates[7:17]]
    assert len(freeze_manifest(catalog, first, last, codes)['dates']) == 17
    assert len(freeze_manifest(catalog, first, last, codes, history_days=300)['dates']) == 17
    with pytest.raises(ValueError, match='正整数'):
        freeze_manifest(catalog, first, last, codes, history_days=0)
    with pytest.raises(ValueError, match='正整数'):
        freeze_manifest(catalog, first, last, codes, history_days=True)


def test_history_window_retains_adjustment_and_missing_stock_semantics():
    from apps.quant_workbench.engine import StrategyAdapter
    dates = pd.bdate_range('2025-01-02', periods=10)
    rows = []
    for i, day in enumerate(dates):
        for code in ['SZ000001', 'SH600000']:
            if code == 'SH600000' and i >= 7:
                continue
            rows.append(dict(datetime=day, instrument=code, close=10 + i,
                             volume=1000., adj_factor=2. if i >= 6 else 1.))
    raw = pd.DataFrame(rows).set_index(['datetime', 'instrument']).sort_index()
    cfg = dict(start=str(dates[0].date()), end=str(dates[-1].date()), symbols=['SZ000001', 'SH600000'],
               open_cost=.0005, close_cost=.0015, min_cost=5.)
    adapter = StrategyAdapter('def handle_data(context, data): pass', raw, cfg, calendar=dates)
    adapter.context.current_dt = dates[-1]
    visible = raw[raw.index.get_level_values('datetime') < dates[-1]]
    for count in [1, 3, 5, 100]:
        window = dates[:-1][-count:]
        expected = visible[visible.index.get_level_values('datetime').isin(window)].copy()
        factors = expected.adj_factor / expected.index.get_level_values('instrument').map(visible.groupby(level='instrument').adj_factor.last())
        expected['close'] *= factors
        expected['volume'] /= factors
        pd.testing.assert_frame_equal(adapter.history(count, ['close', 'volume']), expected[['close', 'volume']])


def test_editor_switches_between_local_and_custom_pool(market, tmp_path, monkeypatch):
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    from apps.quant_workbench.store import Store
    directory, dates = market
    monkeypatch.setenv('QLIB_WORKBENCH_HOME', str(tmp_path / 'ui'))
    monkeypatch.setenv('QLIB_WORKBENCH_DATA', str(directory))
    store = Store()
    store.save_strategy('股票池测试', 'def handle_data(context, data): pass')
    app = AppTest.from_file(str(Path('apps/quant_workbench/app.py').resolve()), default_timeout=30).run()
    app.sidebar.radio[0].set_value('策略编辑').run()
    assert not app.exception
    app.date_input[0].set_value(dates[12].date()).run()
    assert any('3 只' in message.value for message in app.info)
    assert any('2024-12-31' in message.value for message in app.info)
    source = next(widget for widget in app.selectbox if widget.label == '股票池来源')
    source.select('custom').run()
    next(widget for widget in app.text_area if widget.label == '股票池').set_value('SZ000001,SH600000').run()
    source = next(widget for widget in app.selectbox if widget.label == '股票池来源')
    source.select('local_a').run()
    assert not app.exception
    app.date_input[0].set_value(dates[15].date()).run()
    frame = next(widget.value for widget in app.dataframe if '股票代码' in widget.value.columns)
    assert frame['股票代码'].tolist() == ['BJ920001', 'SH600000', 'SZ000002']


def test_editor_restores_strategy_configuration_and_copied_custom_pool(market, tmp_path, monkeypatch):
    import json
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    from apps.quant_workbench.store import Store
    directory, dates = market
    monkeypatch.setenv('QLIB_WORKBENCH_HOME', str(tmp_path / 'ui'))
    monkeypatch.setenv('QLIB_WORKBENCH_DATA', str(directory))
    store = Store()
    sid = store.save_strategy('保存股票池', 'def handle_data(context, data): pass')
    config = dict(start=str(dates[12].date()), end=str(dates[16].date()), capital=10000., symbols=['SH600000'],
                  universe_mode='local_a', history_days=300, params={})
    with store.connect() as db:
        db.execute('INSERT INTO runs(id,strategy_id,name,status,created,updated,config) VALUES(?,?,?,?,?,?,?)',
                   ('b' * 32, sid, '已保存配置', 'success', 1, 1, json.dumps(config)))
    app = AppTest.from_file(str(Path('apps/quant_workbench/app.py').resolve()), default_timeout=30).run()
    app.sidebar.radio[0].set_value('策略编辑').run()
    assert not app.exception
    assert app.date_input[0].value == dates[12].date()
    assert next(widget for widget in app.number_input if widget.label == '初始资金（元）').value == 10000
    assert next(widget for widget in app.number_input if widget.label.startswith('预读历史')).value == 300
    assert any('3 只' in message.value for message in app.info)
    # Copying an old explicit pool must not silently expand or replace its codes.
    app.session_state['copied_config'] = dict(start=str(dates[12].date()), end=str(dates[16].date()),
                                              symbols=['SZ000001'], capital=20000.)
    app.run()
    assert not app.exception
    assert next(widget for widget in app.selectbox if widget.label == '股票池来源').value == 'custom'
    assert next(widget for widget in app.text_area if widget.label == '股票池').value == 'SZ000001'
