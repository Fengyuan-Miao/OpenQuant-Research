"""Exact regression oracles for speedups; no timing thresholds in unit tests."""
from copy import deepcopy
import json
import logging
import math
import time
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from apps.quant_workbench import comparison_cache, engine
from apps.quant_workbench.data import scan, freeze_manifest, snapshot
from apps.quant_workbench.strategies import stock_momentum_base as strategy

metadata_asof = strategy.metadata_asof
UNKNOWN_INDUSTRY = strategy.UNKNOWN_INDUSTRY

# Frozen pre-optimization implementation: evaluate every stock before the liquidity cut.
def reference_rank_candidates(context, prices, data, cutoff):
    """Eligibility → liquidity top N → trend and momentum → deterministic ranking."""
    candidates = {}
    diagnostics = dict(历史不足数=0, 状态缺失数=0, 状态剔除数=0, 流动性入围数=0)
    for code, group in prices.groupby(level="instrument", sort=True):
        group = group.sort_index(level="datetime")
        # Require a full market-day window; never treat missing dates as adjacent returns.
        if (len(group) != context.window or
                group.index.get_level_values("datetime")[-1] != cutoff or
                code not in data or data[code].volume <= 0):
            diagnostics["历史不足数"] += 1
            continue
        close = group.close
        amount = group.amount.tail(context.liquidity_window)
        if (not close.map(math.isfinite).all() or (close <= 0).any() or
                not amount.map(math.isfinite).all() or (amount < 0).any()):
            diagnostics["历史不足数"] += 1
            continue
        state = metadata_asof(context.metadata.get(code, []), cutoff)
        if state is None:
            diagnostics["状态缺失数"] += 1
            if context.metadata_policy == "strict":
                continue
        elif state["is_st"] or state["is_delisting"]:
            diagnostics["状态剔除数"] += 1
            continue
        liquidity = float(amount.mean())  # history.amount is already in yuan.
        if liquidity <= 0:
            continue
        # t is yesterday: -22 is t-21; -148 is t-147 (126-day return, skipping 21 days).
        momentum = float(close.iloc[-context.skip - 1] / close.iloc[-context.skip - context.lookback - 1] - 1)
        volatility = float(close.tail(context.vol_window + 1).pct_change(fill_method=None).dropna().std(ddof=1) * math.sqrt(252))
        if not math.isfinite(volatility):
            continue
        candidates[code] = dict(price=float(close.iloc[-1]), momentum=momentum, volatility=max(volatility, context.vol_floor),
            liquidity=liquidity, trend=bool(close.iloc[-1] > close.tail(context.trend_window).mean()),
            industry=state["industry"] if state else UNKNOWN_INDUSTRY)
    liquid = sorted(candidates, key=lambda code: (-candidates[code]["liquidity"], code))[:context.liquidity_topn]
    diagnostics["流动性入围数"] = len(liquid)
    eligible = {code: candidates[code] for code in liquid
                if candidates[code]["momentum"] > 0 and candidates[code]["trend"]}
    ranked = sorted(eligible, key=lambda code: (-eligible[code]["momentum"], code))
    return eligible, ranked, diagnostics


@pytest.fixture(autouse=True)
def restore_environment(monkeypatch):
    from qlib.config import C
    from qlib.data.cache import H
    previous = deepcopy(C)
    monkeypatch.setattr(strategy, 'log', logging.getLogger('performance_test'), raising=False)
    yield
    H.clear()
    C.set_conf_from_C(previous)
    if previous.registered:
        C.register()


@pytest.mark.parametrize('strict', [False, True])
@pytest.mark.parametrize('liquidity_topn', [1, 12, 100])
def test_ranking_exact_against_original(strict, liquidity_topn):
    codes = [f'SZ{i:06d}' for i in range(1, 41)]
    dates = pd.bdate_range(end='2025-01-31', periods=252)
    rng = np.random.default_rng(2026)
    frames, data, metadata = [], {}, {}
    for i, code in enumerate(codes):
        close = 10 * np.exp(np.cumsum(rng.normal(.001, .02, len(dates))))
        if i == 0:  # Positive prices but overflowing volatility: must backfill the quota.
            close[-2:] = [1e-250, 1e250]
        if i == 1:
            close[-1] = np.nan
        if i == 2:
            close[-1] = -1
        if i in (10, 11):  # Exact factor/liquidity ties keep code ordering deterministic.
            close = np.linspace(10, 30, len(dates))
        frame = pd.DataFrame(dict(datetime=dates, instrument=code, close=close,
                                  amount=float(10000000 if i == 0 else (i // 3 + 1) * 100000)))
        if i == 3:
            frame = frame.iloc[1:]
        if i == 4:
            frame.loc[frame.index[-1], 'amount'] = -1
        frames.append(frame)
        data[code] = SimpleNamespace(volume=0 if i == 5 else 100)
        if i % 4:
            metadata[code] = [dict(effective_date='2020-01-01', known_at='2020-01-01',
                industry='银行', is_st=i == 6, is_delisting=i == 7)]
            metadata[code].append(dict(effective_date='2025-01-01', known_at='2025-03-01',
                industry='未来行业', is_st=True, is_delisting=False))
    ctx = SimpleNamespace(universe=codes, params=dict(liquidity_topn=liquidity_topn,
        metadata_policy='strict' if strict else 'research', security_metadata=metadata))
    strategy.initialize(ctx)
    prices = pd.concat(frames).set_index(['datetime', 'instrument'])
    # Both sorted history output and unsorted notebook input must give identical values.
    expected = reference_rank_candidates(ctx, prices, data, dates[-1])
    assert strategy.rank_candidates(ctx, prices.sort_index(), data, dates[-1]) == expected
    assert strategy.rank_candidates(ctx, prices.sample(frac=1, random_state=9), data, dates[-1]) == expected


def test_indexed_lookup_and_asof_history():
    dates = pd.to_datetime(['2025-01-02', '2025-01-03', '2025-01-06'])
    raw = pd.DataFrame(dict(datetime=dates, instrument='SZ000001', close=[10., 6., 7.],
                           volume=[100., 200., 200.], adj_factor=[1., 2., 2.], amount=[1000., 1200., 1400.]))
    raw = raw.set_index(['datetime', 'instrument'])
    cfg = dict(start='2025-01-02', end='2025-01-06', symbols=['SZ000001'],
               open_cost=.0005, close_cost=.0015, min_cost=5.)
    adapter = engine.StrategyAdapter('def handle_data(context, data): pass', raw, cfg)
    assert adapter._latest('SZ000001', dates[0]) is None
    assert adapter._latest('SZ000002', dates[-1]) is None
    assert adapter._latest('SZ000001', dates[1]).close == 10
    assert adapter._latest('SZ000001', dates[-1]).close == 6
    assert adapter._base_factor('SZ000001') == 1
    adapter.context.current_dt = dates[1]
    assert adapter.history(3, ['close']).close.tolist() == [10.]
    adapter.context.current_dt = dates[-1]
    visible = adapter.history(3, ['close', 'volume', 'amount'])
    assert visible.close.tolist() == [5., 6.]
    assert visible.volume.tolist() == [200., 200.]
    assert visible.amount.tolist() == [1000., 1200.]
    visible.iloc[0, 0] = 999
    assert adapter.history(3, ['close']).close.tolist() == [5., 6.]
    pd.testing.assert_frame_equal(adapter.raw, raw, check_exact=True)


@pytest.fixture
def provider(tmp_path):
    market = tmp_path / 'market'
    market.mkdir()
    dates = pd.bdate_range('2025-01-02', periods=7)
    for i, date in enumerate(dates):
        price = 10. + i
        pd.DataFrame([dict(ts_code=code, trade_date=f'{date:%Y%m%d}', open=price,
            close=price + .1, high=price + 1., low=price - 1., vol=100000.,
            amount=10000., adj_factor=1.) for code in ['000001.SZ', '600000.SH']]
        ).to_parquet(market / f'{date:%Y%m%d}.parquet')
    cfg = dict(start=str(dates[1].date()), end=str(dates[-1].date()), symbols=['SZ000001', 'SH600000'],
               capital=10000., open_cost=.0005, close_cost=.0015, min_cost=5., params={}, benchmark=None)
    manifest = freeze_manifest(scan(market), cfg['start'], cfg['end'], cfg['symbols'])
    return snapshot(manifest, tmp_path / 'snapshots'), cfg


def test_shared_inputs_and_cache_match_independent_accounts(provider, monkeypatch):
    path, cfg = provider
    source = '''
def handle_data(context, data):
    if context.current_dt.day == 3:
        order_target_percent('SZ000001', .5)
    else:
        order_target_percent('SZ000001', 0)
    record(cash=context.cash)
'''
    # Reference calls prepare separate exchanges/accounts, as before the optimization.
    independent, expected_stats = engine._run_backtest(source, path, deepcopy(cfg))
    independent_hold, _ = engine._run_backtest(engine.HOLD_SOURCE, path, deepcopy(cfg))
    original = engine._run_backtest
    calls = []
    def observed(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(engine, '_run_backtest', observed)
    cold, cold_stats = engine.run_backtest(source, path, cfg)
    assert calls == [source, engine.HOLD_SOURCE]
    calls.clear()
    warm, warm_stats = engine.run_backtest(source, path, cfg)
    assert calls == [source]
    assert cold_stats['timings']['comparison_cache_hit'] is False
    assert warm_stats['timings']['comparison_cache_hit'] is True
    for name in cold:
        pd.testing.assert_frame_equal(cold[name], warm[name], check_exact=True)
    for name, frame in independent.items():
        pd.testing.assert_frame_equal(frame, cold[name][frame.columns], check_exact=True)
    for name in ('report', 'holdings', 'trades'):
        pd.testing.assert_frame_equal(independent_hold[name], cold['hold_' + name], check_exact=True)
    assert {key: cold_stats[key] for key in expected_stats} == expected_stats
    assert {key: value for key, value in warm_stats.items() if key != 'timings'} == {
        key: value for key, value in cold_stats.items() if key != 'timings'}
    # Editing a strategy still runs it, while reusing only the independent passive account.
    changed, _ = engine.run_backtest('def handle_data(context, data): pass', path, cfg)
    assert changed['trades'].empty
    assert not warm['trades'].empty
    pd.testing.assert_frame_equal(changed['hold_report'], warm['hold_report'], check_exact=True)
    # Disabling the cache remains useful for verification and special research runs.
    uncached, _ = engine.run_backtest(source, path, cfg, use_comparison_cache=False)
    for name in cold:
        pd.testing.assert_frame_equal(uncached[name], cold[name], check_exact=True)


def test_cache_key_covers_execution_inputs(provider):
    path, cfg = provider
    identity = lambda config, source=engine.HOLD_SOURCE: comparison_cache.cache_identity(path, config, source)
    original = identity(cfg)
    for key, value in dict(capital=12000., open_cost=.003, close_cost=.004, min_cost=10.,
                           start='2025-01-06', end='2025-01-09', symbols=list(reversed(cfg['symbols']))).items():
        assert identity({**cfg, key: value}) != original
    assert identity({**cfg, 'symbols': cfg['symbols'][:1]}) != original
    assert identity({**cfg, 'params': {'lookback': 90}, 'benchmark': 'SZ000001'}) == original
    assert identity(cfg, engine.HOLD_SOURCE + '\n# execution changed') != original
    ready = path / 'ready.json'
    ready.write_text(ready.read_text() + '\n')
    assert identity(cfg) != original
    ready.unlink()
    assert identity(cfg) is None


def test_corruption_and_cancellation_never_publish_partial_cache(tmp_path):
    folder = tmp_path / 'cache' / 'abc'
    tables = {name: pd.DataFrame() for name in comparison_cache.TABLES}
    tables['report'] = pd.DataFrame(dict(nav=[1., 1.1]))
    count = 0
    def compute():
        nonlocal count
        count += 1
        return tables, {'total_return': .1}
    no_cancel = lambda: None
    cold = comparison_cache.load_or_compute(folder, compute, no_cancel)
    warm = comparison_cache.load_or_compute(folder, compute, no_cancel)
    assert cold[2] is False and warm[2] is True and count == 1
    for name in tables:
        pd.testing.assert_frame_equal(cold[0][name], warm[0][name], check_exact=True)
    (folder / 'report.parquet').write_bytes(b'incomplete')
    assert comparison_cache.load_or_compute(folder, compute, no_cancel)[2] is False
    assert count == 2
    class Cancelled(Exception):
        pass
    def cancel():
        raise Cancelled()
    with pytest.raises(Cancelled):
        comparison_cache.load_or_compute(folder, compute, cancel)
    assert count == 2
    missing = folder.with_name('cancelled')
    def fail():
        raise Cancelled()
    with pytest.raises(Cancelled):
        comparison_cache.load_or_compute(missing, fail, no_cancel)
    assert not missing.exists()
    assert not list(folder.parent.glob('building-*'))
    # Stop after a completed computation, before publication.
    def cancel_after_compute():
        if count > 2:
            cancel()
    with pytest.raises(Cancelled):
        comparison_cache.load_or_compute(missing, compute, cancel_after_compute)
    assert not missing.exists()


def test_cache_reused_by_separate_worker_processes(provider, tmp_path, monkeypatch):
    from apps.quant_workbench.store import Store, ACTIVE
    monkeypatch.delenv('PYTHONHASHSEED', raising=False)
    path, cfg = provider
    manifest = json.loads((path / 'ready.json').read_text())
    store = Store(tmp_path / 'workbench')
    source = 'def handle_data(context, data):\n    order_target_percent("SZ000001", .5)'
    results = []
    for i in range(2):
        run_id = store.submit(None, f'cache check {i}', source, cfg, manifest)
        deadline = time.monotonic() + 30
        while store.get_run(run_id)['status'] in ACTIVE and time.monotonic() < deadline:
            time.sleep(.05)
        run = store.get_run(run_id)
        assert run['status'] == 'success', (store.directory(run_id) / 'run.log').read_text()
        results.append(store.directory(run_id))
    first, second = [json.loads((folder / 'metrics.json').read_text()) for folder in results]
    assert first['timings']['comparison_cache_hit'] is False
    assert second['timings']['comparison_cache_hit'] is True
    assert second['timings']['snapshot_seconds'] >= 0
    assert second['timings']['worker_seconds'] >= second['timings']['total_seconds']
    for file in results[0].glob('*.parquet'):
        pd.testing.assert_frame_equal(pd.read_parquet(file), pd.read_parquet(results[1] / file.name), check_exact=True)
