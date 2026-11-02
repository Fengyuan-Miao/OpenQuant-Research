"""Behavioral checks for the research baseline, including real Qlib fills."""
from copy import deepcopy
import logging
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from apps.quant_workbench.strategies import stock_momentum_base as strategy
from apps.quant_workbench.data import scan, freeze_manifest, snapshot
from apps.quant_workbench.templates import TEMPLATES


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    from qlib.config import C
    from qlib.data.cache import H
    previous = deepcopy(C)
    monkeypatch.setattr(strategy, 'log', logging.getLogger('base_test'), raising=False)
    yield
    H.clear()
    C.set_conf_from_C(previous)
    if previous.registered:
        C.register()


def context(**params):
    ctx = SimpleNamespace(params=params, universe=['SZ000001', 'SH600000'],
        current_dt=pd.Timestamp('2025-02-03'), previous_dt=pd.Timestamp('2025-01-31'),
        portfolio=SimpleNamespace(total_value=100000, cash=100000, available_cash=100000, positions={}))
    strategy.initialize(ctx)
    return ctx


def metadata(**overrides):
    return dict(effective_date='2020-01-01', known_at='2020-01-01',
                is_st=False, is_delisting=False, industry='银行', **overrides)


def prices_frame(codes, n=252):
    dates = pd.bdate_range(end='2025-01-31', periods=n)
    return pd.concat([pd.DataFrame({'datetime': dates, 'instrument': code,
        'close': np.linspace(10, 30 + i, n), 'amount': 1000000 + i * 10000})
        for i, code in enumerate(codes)]).set_index(['datetime', 'instrument']).sort_index()


def test_skip_window_and_liquidity_before_trend():
    ctx = context()
    prices = prices_frame(ctx.universe)
    data = {code: SimpleNamespace(volume=100) for code in ctx.universe}
    candidates, ranked, _ = strategy.rank_candidates(ctx, prices, data, ctx.previous_dt)
    close = prices.xs('SZ000001', level='instrument').close
    assert candidates['SZ000001']['momentum'] == pytest.approx(close.iloc[-22] / close.iloc[-148] - 1)
    changed = prices.copy()
    idx = changed.index.get_level_values('datetime') > close.index[-22]
    changed.loc[idx, 'close'] *= 1.2
    after, _, _ = strategy.rank_candidates(ctx, changed, data, ctx.previous_dt)
    assert after['SZ000001']['momentum'] == candidates['SZ000001']['momentum']
    ctx.liquidity_topn = 1
    # Most liquid stock fails the trend; don't backfill with a less liquid stock.
    changed.loc[(ctx.previous_dt, 'SH600000'), 'close'] = 1
    _, ranked, diagnostics = strategy.rank_candidates(ctx, changed, data, ctx.previous_dt)
    assert ranked == []
    assert diagnostics['流动性入围数'] == 1


def test_metadata_visible_dates_st_and_unknown_policy():
    base = metadata()
    future = {**base, 'effective_date': '2025-01-01', 'known_at': '2025-03-01', 'is_st': True}
    states = strategy.prepare_metadata({'000001.SZ': [future, base]})
    assert not strategy.metadata_asof(states['SZ000001'], pd.Timestamp('2025-01-31'))['is_st']
    assert strategy.metadata_asof(states['SZ000001'], pd.Timestamp('2025-03-01'))['is_st']
    cfg = {'000001.SZ': [base]}
    ctx = context(metadata_policy='strict', security_metadata=cfg)
    frame = prices_frame(ctx.universe)
    data = {code: SimpleNamespace(volume=1) for code in ctx.universe}
    candidates, ranked, counts = strategy.rank_candidates(ctx, frame, data, ctx.previous_dt)
    assert ranked == ['SZ000001']
    assert counts['状态缺失数'] == 1
    ctx.metadata = strategy.prepare_metadata({'000001.SZ': [{**base, 'is_st': True}]})
    assert strategy.rank_candidates(ctx, frame, data, ctx.previous_dt)[1] == []
    with pytest.raises(ValueError, match='历史状态'):
        context(metadata_policy='strict')
    with pytest.raises(ValueError, match='布尔'):
        strategy.prepare_metadata({'000001.SZ': [{**base, 'is_st': 'false'}]})


def test_missing_history_and_paused_are_ineligible():
    ctx = context()
    frame = prices_frame(ctx.universe)
    first_date = frame.index.get_level_values('datetime').min()
    frame = frame.drop(index=(first_date, 'SZ000001'))
    candidates, ranked, _ = strategy.rank_candidates(ctx, frame,
        {'SZ000001': SimpleNamespace(volume=1), 'SH600000': SimpleNamespace(volume=0)}, ctx.previous_dt)
    assert not candidates
    assert not ranked


def test_buffer_uses_holdings_and_does_not_expand_entry_rank():
    ranked = ['A', 'B', 'C', 'D', 'E']
    assert strategy.select_with_buffer(ranked, {'C'}, 2, 3) == ['C', 'A']
    assert strategy.select_with_buffer(ranked, {'D'}, 2, 3) == ['A', 'B']
    assert strategy.select_with_buffer(ranked, set(), 2, 3) == ['A', 'B']
    assert strategy.select_with_buffer([], {'A'}, 2, 3) == []


def test_inverse_volatility_caps_and_unknown_industry():
    candidates = {'A': dict(volatility=.2, industry='银行'), 'B': dict(volatility=.4, industry='科技')}
    assert strategy.allocate_weights(['A', 'B'], candidates, .9, 1, 1) == pytest.approx({'A': .6, 'B': .3})
    weights = strategy.allocate_weights(['A', 'B'], candidates, .9, .05, .2)
    assert weights == pytest.approx({'A': .05, 'B': .05})
    unknown = {str(i): dict(volatility=.2, industry=strategy.UNKNOWN_INDUSTRY) for i in range(20)}
    weights = strategy.allocate_weights(list(unknown), unknown, .9, .05, .2)
    assert sum(weights.values()) == pytest.approx(.2)
    assert max(weights.values()) <= .05
    assert strategy.allocate_weights([], {}, .9, .05, .2) == {}


def test_month_boundary_no_midmonth_start_and_liquidation(monkeypatch):
    ctx = context()
    ctx.portfolio.positions = {'SZ000001': SimpleNamespace(total_amount=100, price=10, value=1000)}
    calls = []
    monkeypatch.setattr(strategy, 'record', lambda **kw: None, raising=False)
    monkeypatch.setattr(strategy, 'history', lambda *a: prices_frame(ctx.universe), raising=False)
    monkeypatch.setattr(strategy, 'order_target_value', lambda *a: calls.append(a), raising=False)
    monkeypatch.setattr(strategy, 'rank_candidates', lambda *a: ({}, [], {'状态缺失数': 0}))
    ctx.current_dt, ctx.previous_dt = pd.Timestamp('2025-01-15'), pd.Timestamp('2025-01-14')
    strategy.handle_data(ctx, {})
    assert calls == []
    ctx.current_dt, ctx.previous_dt = pd.Timestamp('2025-02-03'), pd.Timestamp('2025-01-31')
    strategy.handle_data(ctx, {})
    assert calls == [('SZ000001', 0.0)]
    strategy.handle_data(ctx, {})
    assert len(calls) == 1  # duplicate callback cannot submit twice


@pytest.mark.parametrize('params', [{'topk': 0}, {'skip': -1}, {'topk': 20, 'buffer': 10},
                                    {'stock_cap': float('nan')}, {'vol_floor': 0}, {'topk': 2.5}])
def test_invalid_parameters(params):
    with pytest.raises(ValueError):
        context(**params)


def test_default_baseline_real_qlib_monthly_and_future_independence(tmp_path):
    from apps.quant_workbench.engine import run_backtest
    directory = tmp_path / 'market'
    directory.mkdir()
    dates = pd.bdate_range('2024-01-02', '2025-03-31')
    codes = ['000001.SZ', '600000.SH']
    for i, date in enumerate(dates):
        values = np.array([10 * np.exp(.0015 * i + .01 * np.sin(i)), 15 * np.exp(.001 * i + .02 * np.cos(i))])
        pd.DataFrame([dict(ts_code=code, trade_date=f'{date:%Y%m%d}', open=value, close=value,
                          high=value * 1.01, low=value * .99, vol=100000., amount=100000., adj_factor=1.)
                      for code, value in zip(codes, values)]).to_parquet(directory / f'{date:%Y%m%d}.parquet')
    cfg = dict(start='2025-01-02', end='2025-03-31', capital=10000., symbols=['SZ000001', 'SH600000'],
               benchmark=None, open_cost=.0005, close_cost=.0015, min_cost=5., params={})
    def run():
        manifest = freeze_manifest(scan(directory), cfg['start'], cfg['end'], cfg['symbols'])
        provider = snapshot(manifest, tmp_path / 'snapshots')
        return run_backtest(TEMPLATES['A股中期动量基线'], provider, cfg)
    before, stats = run()
    # Jan 1 exists in this synthetic calendar: Jan 2 is NOT the month's first market day.
    records = before['records']
    days = records.loc[records['月度调仓'] == 1, 'datetime'].dt.strftime('%Y-%m-%d').tolist()
    assert days == ['2025-02-03', '2025-03-03']
    assert before['trades'].amount.gt(0).any()
    assert set(before['trades'].datetime.dt.strftime('%Y-%m-%d')) <= set(days)
    assert records['目标股票仓位'].dropna().le(.6 + 1e-10).all()
    assert before['report'].cash.ge(0).all()
    plan = before['rebalance_plan']
    buys = plan[plan.action == '买入']
    assert not buys.empty
    assert (buys.trade_shares % 100).abs().lt(1e-3).all()
    assert buys.target_weight.le(.3 + 1e-10).all()
    assert (plan.signal_date < plan.datetime).all()
    assert before['trades'].cost.gt(0).any()
    assert len(records) == len(before['report'])
    # Change a later day's price and corporate-action factor: earlier decisions stay identical.
    file = directory / '20250331.parquet'
    frame = pd.read_parquet(file)
    frame[['open', 'high', 'low', 'close']] *= 3
    frame.adj_factor *= 2
    frame.to_parquet(file)
    after, _ = run()
    pd.testing.assert_frame_equal(before['trades'], after['trades'])
    pd.testing.assert_frame_equal(before['records'], after['records'])
    pd.testing.assert_frame_equal(before['rebalance_plan'], after['rebalance_plan'])


def small_context(**params):
    ctx = context(**params)
    ctx.portfolio = SimpleNamespace(total_value=10000., cash=10000., available_cash=10000., positions={})
    return ctx


def candidates_for(prices, industries=None):
    return {code: dict(price=price, volatility=.2, industry=(industries or {}).get(code, strategy.UNKNOWN_INDUSTRY))
            for code, price in prices.items()}


def add_holding(ctx, code, shares, price):
    ctx.portfolio.positions[code] = SimpleNamespace(total_amount=shares, price=price, value=shares * price)
    ctx.portfolio.cash -= shares * price
    ctx.portfolio.available_cash = ctx.portfolio.cash


def test_small_account_defaults_and_high_price_backfill():
    ctx = small_context()
    assert (ctx.topk, ctx.buffer, ctx.risk, ctx.stock_cap) == (3, 5, .8, .3)
    candidates = candidates_for({'A': 1450, 'B': 10, 'C': 12, 'D': 15})
    selected, weights, skipped = strategy.select_affordable(ctx, candidates, list(candidates), set())
    assert selected == ['B', 'C', 'D']
    assert '买不起' in skipped['A']
    assert sum(weights.values()) == pytest.approx(.8)  # unknown no longer stuck at 20%
    rows, targets, cash = strategy.plan_rebalance(ctx, {}, candidates, list(candidates), selected, weights, skipped)
    assert all(qty % 100 == 0 for qty in targets.values())
    assert sum(row['target_value'] for row in rows) <= 8000
    assert cash >= 2000
    assert next(row for row in rows if row['instrument'] == 'A')['action'] == '跳过'


def test_allocation_infeasible_new_name_replaced_not_rounded_up():
    ctx = small_context()
    candidates = candidates_for({'A': 29, 'B': 10, 'C': 10, 'D': 10})
    # A can afford one lot under its 30% cap, but not under its inverse-volatility allocation.
    selected, _, skipped = strategy.select_affordable(ctx, candidates, list(candidates), set())
    assert selected == ['B', 'C', 'D']
    assert '分配权重后' in skipped['A']


def test_known_and_unknown_industry_have_distinct_caps():
    ctx = small_context()
    candidates = candidates_for({'A': 10, 'B': 10, 'C': 10}, {'A': '银行', 'B': '银行'})
    selected, weights, _ = strategy.select_affordable(ctx, candidates, list(candidates), set())
    assert weights['A'] + weights['B'] == pytest.approx(.5)
    assert weights['C'] > .2
    ctx.unknown_industry_cap = .1
    _, weights, _ = strategy.select_affordable(ctx, candidates, list(candidates), set())
    assert weights.get('C', 0) <= .1


def test_skip_small_reduction_but_never_skip_liquidation_or_cap_reduction():
    ctx = small_context(min_trade_value=1500, price_buffer=0)
    add_holding(ctx, 'A', 200, 10)
    candidates = candidates_for({'A': 10})
    rows, targets, _ = strategy.plan_rebalance(ctx, {}, candidates, ['A'], ['A'], {'A': .1}, {})
    assert targets['A'] == 200
    assert '保留旧仓' in rows[0]['reason']
    # Same 1000-yuan sale is mandatory when the stock exceeds the configured cap.
    ctx.stock_cap = .15
    rows, targets, _ = strategy.plan_rebalance(ctx, {}, candidates, ['A'], ['A'], {'A': .1}, {})
    assert targets['A'] == 100 and rows[0]['action'] == '卖出'
    # Exit an odd lot even though the amount is below the minimum trade value.
    ctx.portfolio.positions['A'].total_amount = 25
    ctx.portfolio.positions['A'].value = 250
    rows, targets, _ = strategy.plan_rebalance(ctx, {}, {}, [], [], {}, {})
    assert targets['A'] == 0
    assert rows[0]['trade_shares'] == -25


def test_preserved_small_position_consumes_budget_for_new_buys():
    ctx = small_context(risk=.5, unknown_industry_cap=.5, min_trade_value=1500, price_buffer=0)
    add_holding(ctx, 'A', 200, 10)
    add_holding(ctx, 'B', 200, 10)
    candidates = candidates_for({'A': 10, 'B': 10, 'C': 10})
    rows, targets, cash = strategy.plan_rebalance(ctx, {}, candidates, list(candidates), list(candidates),
                                                {'A': .1, 'B': .2, 'C': .2}, {})
    assert targets == {'A': 200, 'B': 200}
    assert cash == 6000
    assert next(row for row in rows if row['instrument'] == 'C')['action'] == '跳过'


def test_cash_fees_gap_reserve_and_industry_limits():
    ctx = small_context(price_buffer=.02, industry_cap=.3)
    # High configured commission must affect the plan, not only the final backtest.
    ctx.fees = dict(open_cost=.003, close_cost=.003, min_cost=15)
    candidates = candidates_for({'A': 10, 'B': 10}, {'A': '银行', 'B': '银行'})
    rows, targets, cash = strategy.plan_rebalance(ctx, {}, candidates, ['A', 'B'], ['A', 'B'], {'A': .3, 'B': .3}, {})
    # Small allocations fail the 0.5% fee threshold; neither gets a fictitious fill.
    assert not targets
    ctx.max_trade_cost_ratio = .02
    rows, targets, cash = strategy.plan_rebalance(ctx, {}, candidates, ['A', 'B'], ['A', 'B'], {'A': .3, 'B': .3}, {})
    assert sum(qty * 10 for qty in targets.values()) <= 3000
    buy_value = sum(r['trade_shares'] * r['reference_price'] for r in rows if r['action'] == '买入')
    assert cash == pytest.approx(10000 - buy_value * 1.02 - sum(r['estimated_cost'] for r in rows))
    assert cash >= 2000


def test_full_cash_deployment_keeps_fee_budget():
    ctx = small_context(risk=1., stock_cap=1., unknown_industry_cap=1., price_buffer=0)
    candidates = candidates_for({'A': 10})
    rows, targets, cash = strategy.plan_rebalance(ctx, {}, candidates, ['A'], ['A'], {'A': 1.}, {})
    assert targets['A'] == 900  # cannot spend all 10000 on shares and leave no commission
    assert cash == 995


def test_existing_fractional_shares_only_change_by_lots_except_exit():
    ctx = small_context(price_buffer=0, min_trade_value=0, rebalance_band=0, stock_cap=.15)
    add_holding(ctx, 'A', 205.00001, 10)
    candidates = candidates_for({'A': 10})
    rows, targets, _ = strategy.plan_rebalance(ctx, {}, candidates, ['A'], ['A'], {'A': .1}, {})
    assert rows[0]['trade_shares'] == pytest.approx(-100)
    assert targets['A'] == pytest.approx(105.00001)


@pytest.mark.parametrize('params', [{'price_buffer': 1}, {'min_trade_value': -1},
                                   {'rebalance_band': float('nan')}, {'max_trade_cost_ratio': 0},
                                   {'unknown_industry_cap': 1.1}])
def test_invalid_small_account_parameters(params):
    with pytest.raises(ValueError):
        small_context(**params)


def test_fee_estimate_uses_run_configuration():
    from apps.quant_workbench.engine import StrategyAdapter
    cfg = dict(start='2025-02-03', end='2025-02-03', symbols=['SZ000001'],
               open_cost=.002, close_cost=.004, min_cost=12., params={})
    adapter = StrategyAdapter(TEMPLATES['A股中期动量基线'], prices_frame(cfg['symbols']), cfg)
    adapter.initialize(adapter.context)
    assert strategy.estimate_cost(adapter.context, 1000, True) == 12
    assert strategy.estimate_cost(adapter.context, 10000, False) == 40


def test_plan_view_date_filter_csv_and_archive(tmp_path, monkeypatch):
    import io
    import json
    import zipfile
    from streamlit.testing.v1 import AppTest
    from apps.quant_workbench.store import Store
    from apps.quant_workbench.metrics import analyze

    root = tmp_path / 'ui'
    monkeypatch.setenv('QLIB_WORKBENCH_HOME', str(root))
    store = Store(root)
    run_id = 'a' * 32
    folder = store.directory(run_id)
    folder.mkdir(parents=True)
    cfg = dict(start='2025-02-03', end='2025-03-03', capital=10000, symbols=['SZ000001'],
               benchmark=None, open_cost=.0005, close_cost=.0015, min_cost=5, params={})
    dates = pd.to_datetime(['2025-02-03', '2025-03-03'])
    report, stats = analyze(pd.DataFrame({'account': [10000., 10010.]}, index=dates), 10000)
    stats['timings'] = dict(snapshot_seconds=1., prepare_seconds=2., strategy_seconds=3.,
        comparison_seconds=.1, total_seconds=5.1, worker_seconds=6.1, comparison_cache_hit=True)
    report.rename_axis('datetime').reset_index().to_parquet(folder / 'report.parquet', index=False)
    (folder / 'metrics.json').write_text(json.dumps(stats))
    (folder / 'strategy.py').write_text(TEMPLATES['A股中期动量基线'])
    (folder / 'config.json').write_text(json.dumps(cfg))
    for name in ('holdings', 'trades', 'records'):
        pd.DataFrame().to_parquet(folder / f'{name}.parquet')
    ctx = small_context(price_buffer=0)
    candidates = candidates_for({'SZ000001': 10})
    rows, _, _ = strategy.plan_rebalance(ctx, {}, candidates, ['SZ000001'], ['SZ000001'], {'SZ000001': .3}, {})
    plan = pd.concat([pd.DataFrame(rows).assign(datetime=date) for date in dates], ignore_index=True)
    plan.to_parquet(folder / 'rebalance_plan.parquet', index=False)
    with store.connect() as db:
        db.execute('INSERT INTO runs (id,name,status,created,updated,config,progress) VALUES (?,?,?,?,?,?,?)',
                   (run_id, '小资金计划测试', 'success', 1, 1, json.dumps(cfg), 1))
    app = AppTest.from_file(str(Path('apps/quant_workbench/app.py').resolve()), default_timeout=30).run()
    app.sidebar.radio[0].set_value('回测结果').run()
    assert not app.exception
    assert '调仓计划' in [tab.label for tab in app.tabs]
    assert any('耗时 6.1s' in caption.value and '对照已复用缓存' in caption.value for caption in app.caption)
    day = next(widget for widget in app.selectbox if widget.label == '计划日期')
    assert pd.Timestamp(day.value) == dates[-1]
    day.select(dates[0].to_datetime64()).run()
    assert not app.exception
    shown = next(frame.value for frame in app.dataframe if 'trade_shares' in frame.value.columns)
    assert shown.datetime.tolist() == [dates[0]]
    assert any(widget.label == '导出调仓计划 CSV' for widget in app.get('download_button'))
    with zipfile.ZipFile(io.BytesIO(store.archive(run_id))) as archive:
        exported = pd.read_csv(archive.open('rebalance_plan.csv'))
        assert len(exported) == 2
        assert exported.trade_shares.tolist() == [300, 300]


def test_dividend_odd_shares_do_not_create_an_unnecessary_sale():
    ctx = small_context()
    add_holding(ctx, 'A', 204.405359, 11.57)
    candidates = candidates_for({'A': 11.57})
    rows, targets, cash = strategy.plan_rebalance(ctx, {}, candidates, ['A'], ['A'], {'A': .3}, {})
    assert targets['A'] == pytest.approx(204.405359)
    assert rows[0]['action'] == '持有'
    assert rows[0]['estimated_cost'] == 0
    assert cash == ctx.portfolio.available_cash


def test_cap_correction_bypasses_small_delta_and_charges_only_one_fee():
    ctx = small_context(price_buffer=0, min_trade_value=5000)
    add_holding(ctx, 'A', 305, 10)
    candidates = candidates_for({'A': 10})
    rows, targets, cash = strategy.plan_rebalance(ctx, {}, candidates, ['A'], ['A'], {'A': .3}, {})
    assert targets['A'] == 205
    assert rows[0]['trade_shares'] == -100
    assert rows[0]['estimated_cost'] == 5
    assert '超限减仓' in rows[0]['reason']
    assert cash == pytest.approx(ctx.portfolio.available_cash + 1000 - 5)
