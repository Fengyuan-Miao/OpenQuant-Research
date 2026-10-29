"""Run with: python -m pytest apps/quant_workbench/tests -q"""
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("streamlit")
pytest.importorskip("streamlit_ace")
pytest.importorskip("psutil")

from apps.quant_workbench.templates import TEMPLATES
from apps.quant_workbench.data import scan, freeze_manifest, snapshot


@pytest.fixture(autouse=True)
def restore_qlib_configuration():
    from qlib.config import C
    from qlib.data.cache import H
    previous = deepcopy(C)
    yield
    H.clear()
    C.set_conf_from_C(previous)
    if previous.registered:
        C.register()


@pytest.fixture
def market(tmp_path):
    directory = tmp_path / "market"
    directory.mkdir()
    dates = pd.bdate_range("2025-01-02", periods=30)
    for date in dates:
        pd.DataFrame([{
            "ts_code": code, "trade_date": date.strftime("%Y%m%d"),
            "open": 10., "high": 12., "low": 9., "close": 11.,
            "vol": 100000., "amount": 10000., "adj_factor": 1.,
        } for code in ("000001.SZ", "600000.SH")]).to_parquet(directory / f"{date:%Y%m%d}.parquet")
    return directory, dates


def config(dates, **kwargs):
    return {"start": str(dates[20].date()), "end": str(dates[-1].date()), "capital": 100000.,
            "symbols": ["SZ000001", "SH600000"], "open_cost": 0.001, "close_cost": 0.001,
            "min_cost": 5., "benchmark": None, "params": {}, **kwargs}


def prepare(market, tmp_path, cfg):
    manifest = freeze_manifest(scan(market[0]), cfg["start"], cfg["end"], cfg["symbols"])
    return snapshot(manifest, tmp_path / "snapshots"), cfg, manifest


@pytest.mark.parametrize("name", list(TEMPLATES))
def test_teaching_templates_with_actual_qlib(name, market, tmp_path):
    """Rising/falling stocks and a crash/recovery exercise actual fills, not just syntax."""
    from apps.quant_workbench.engine import run_backtest

    directory, dates = market
    for i, date in enumerate(dates):
        file = directory / f"{date:%Y%m%d}.parquet"
        frame = pd.read_parquet(file)
        prices = np.array([6.0 if i in (22, 23) else 10 + i * 0.1, 20 - i * 0.05])
        frame["open"] = prices
        frame["close"] = prices
        frame["high"] = prices + 0.05
        frame["low"] = prices - 0.05
        frame.to_parquet(file)
    cfg = config(dates, start=str(dates[20].date()), end=str(dates[-1].date()), capital=100000)
    path, cfg, _ = prepare(market, tmp_path, cfg)
    tables, stats = run_backtest(TEMPLATES[name], path, cfg)
    assert len(tables["report"]) == 10
    assert np.isfinite(tables["report"].account).all()
    assert tables["report"].cash.min() >= -0.01
    assert (tables["trades"].amount > 0).any(), f"{name} did not produce a fill"
    assert len(tables["records"]) == 10  # daily account logging even on non-rebalance days
    if name in ("双均线", "布林带均值回归", "唐奇安通道突破"):
        assert set(tables["trades"].query("amount > 0").direction) == {"买入", "卖出"}


def template_harness(name, params):
    """Drive visible historical bars and capture the intended portfolio before execution."""
    from types import SimpleNamespace
    import logging

    context = SimpleNamespace(params=params, universe=["SZ000001", "SH600000"],
                              portfolio=SimpleNamespace(total_value=100000))
    current = {"frame": None, "targets": {}}
    namespace = {"history": lambda count, fields: current["frame"],
                 "order_target_percent": lambda stock, weight: current["targets"].update({stock: weight}),
                 "record": lambda **values: None, "log": logging.getLogger("template_test")}
    exec(compile(TEMPLATES[name], "template.py", "exec"), namespace)
    namespace["initialize"](context)

    def step(prices_by_stock):
        rows = []
        for code, values in prices_by_stock.items():
            for i, value in enumerate(values):
                rows.append({"datetime": pd.Timestamp("2025-01-01") + pd.Timedelta(days=i),
                             "instrument": code, "close": value, "high": value + 0.1, "low": value - 0.1})
        current["frame"] = pd.DataFrame(rows).set_index(["datetime", "instrument"]).sort_index()
        current["targets"] = {}
        namespace["handle_data"](context, {code: SimpleNamespace(volume=100) for code in prices_by_stock})
        return current["targets"].copy()
    return step


def test_momentum_reversal_rank_and_rebalance():
    prices = {"SZ000001": [10, 11, 12], "SH600000": [10, 9, 8]}
    for name, winner in [("动量轮动", "SZ000001"), ("短期反转", "SH600000")]:
        step = template_harness(name, {"lookback": 2, "rebalance": 3, "topk": 1})
        assert step(prices)[winner] == 0.95
        assert step(prices) == {}
        assert step(prices) == {}
        assert step(prices)[winner] == 0.95


@pytest.mark.parametrize("name,params,observations", [
    ("布林带均值回归", {"window": 3, "num_std": 1}, [[10, 10, 5], [10, 5, 6], [5, 6, 10]]),
    ("唐奇安通道突破", {"entry": 2, "exit": 1}, [[9, 10, 12], [10, 12, 12], [12, 12, 10]]),
])
def test_entry_hold_and_exit(name, params, observations):
    step = template_harness(name, params)
    weights = [step({"SZ000001": prices})["SZ000001"] for prices in observations]
    assert weights == [0.475, 0.475, 0]


@pytest.mark.parametrize("name", list(TEMPLATES))
def test_templates_reject_invalid_risk(name):
    with pytest.raises(AssertionError, match="risk"):
        template_harness(name, {"risk": 1.1})


def test_teaching_template_selection_in_ui(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("QLIB_WORKBENCH_HOME", str(tmp_path / "teaching_ui"))
    app = AppTest.from_file(str(Path("apps/quant_workbench/app.py").resolve()), default_timeout=30).run()
    selector = next(s for s in app.selectbox if s.label == "策略模板")
    assert selector.options == list(TEMPLATES)
    selector.select("唐奇安通道突破").run()
    assert not app.exception
    assert "第 6 课" in app.info[0].value


@pytest.mark.parametrize("symbols,expected", [(["SZ000001"], 10891.), (["SZ000001", "SH600000"], 10790.)])
def test_passive_comparison_costs_and_no_rebalance(market, tmp_path, symbols, expected):
    from apps.quant_workbench.engine import run_backtest

    directory, dates = market
    # A split after entry must preserve passive wealth, without another buy.
    for date in dates[2:]:
        file = directory / f"{date:%Y%m%d}.parquet"
        frame = pd.read_parquet(file)
        frame[["open", "high", "low", "close"]] /= 2
        frame["adj_factor"] = 2.
        frame.to_parquet(file)
    cfg = config(dates, start=str(dates[1].date()), end=str(dates[3].date()), capital=10000., symbols=symbols)
    path, _, _ = prepare(market, tmp_path, cfg)
    progress = []
    tables, stats = run_backtest('def handle_data(context, data): pass', path, cfg,
                                 lambda value, message: progress.append(value))
    assert tables["report"].account.tolist() == [10000.] * 3
    assert tables["hold_report"].account.tolist() == pytest.approx([expected] * 3)
    assert len(tables["hold_trades"]) == len(symbols)
    assert stats["hold_metrics"]["total_return"] == pytest.approx(expected / 10000 - 1)
    assert stats["hold_return_difference"] == pytest.approx(1 - expected / 10000)
    assert tables["report"].hold_return_difference.tolist() == pytest.approx([1 - expected / 10000] * 3)
    assert progress == sorted(progress)
    assert progress[-1] == 1


def test_passive_paused_first_day_keeps_cash(market, tmp_path):
    from apps.quant_workbench.engine import run_backtest

    directory, dates = market
    file = directory / f"{dates[1]:%Y%m%d}.parquet"
    pd.read_parquet(file).assign(vol=0.).to_parquet(file)
    cfg = config(dates, start=str(dates[1].date()), end=str(dates[3].date()), symbols=["SZ000001"])
    path, _, _ = prepare(market, tmp_path, cfg)
    tables, stats = run_backtest('def handle_data(context, data): pass', path, cfg)
    assert stats["hold_metrics"]["total_return"] == 0
    assert len(tables["hold_trades"]) == 1
    assert tables["hold_trades"].amount.iloc[0] == 0
    assert tables["hold_holdings"].empty


def test_passive_can_cancel(market, tmp_path):
    from apps.quant_workbench.engine import run_backtest

    cfg = config(market[1])
    path, _, _ = prepare(market, tmp_path, cfg)
    state = {"cancel": False}
    def progress(value, message):
        if message.startswith("买入持有对照"):
            state["cancel"] = True
    def check_cancel():
        if state["cancel"]:
            raise RuntimeError("cancel comparison")
    with pytest.raises(RuntimeError, match="cancel comparison"):
        run_backtest('def handle_data(context, data): pass', path, cfg, progress, check_cancel)


def test_passive_results_ui_export_and_legacy(market, tmp_path, monkeypatch):
    import io
    import json
    import time
    import zipfile
    from streamlit.testing.v1 import AppTest
    from apps.quant_workbench.store import Store, ACTIVE

    home = tmp_path / "comparison_ui"
    monkeypatch.setenv("QLIB_WORKBENCH_HOME", str(home))
    monkeypatch.setenv("QLIB_WORKBENCH_DATA", str(market[0]))
    cfg = config(market[1], symbols=["SZ000001"], benchmark="SZ000001")
    _, _, manifest = prepare(market, tmp_path, cfg)
    store = Store(home)
    run_id = store.submit(None, "对照测试", 'def handle_data(context, data): pass', cfg, manifest)
    deadline = time.monotonic() + 40
    while store.get_run(run_id)["status"] in ACTIVE and time.monotonic() < deadline:
        time.sleep(0.1)
        store.reconcile()
    assert store.get_run(run_id)["status"] == "success"
    with zipfile.ZipFile(io.BytesIO(store.archive(run_id))) as archive:
        assert {"hold_report.csv", "hold_trades.csv", "hold_holdings.parquet"} <= set(archive.namelist())
    app = AppTest.from_file(str(Path("apps/quant_workbench/app.py").resolve()), default_timeout=30).run()
    app.sidebar.radio[0].set_value("回测结果").run()
    assert not app.exception
    assert any(m.label == "标的买入持有收益" for m in app.metric)
    assert any(m.label == "策略 − 买入持有" for m in app.metric)
    assert len(app.get("plotly_chart")) == 3
    assert any("基准收益" in c.value for c in app.caption)
    # Historical results created before the feature remain viewable.
    folder = store.directory(run_id)
    stats = json.loads((folder / "metrics.json").read_text())
    (folder / "metrics.json").write_text(json.dumps({k: v for k, v in stats.items() if not k.startswith("hold_")}))
    report = pd.read_parquet(folder / "report.parquet")
    report.drop(columns=[c for c in report if c.startswith("hold_")]).to_parquet(folder / "report.parquet")
    app.run()
    assert not app.exception
    assert any("复制配置" in info.value for info in app.info)
