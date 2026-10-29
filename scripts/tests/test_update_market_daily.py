from datetime import date, datetime
from pathlib import Path
import importlib.util

import pandas as pd
import pytest

spec = importlib.util.spec_from_file_location('daily_download', Path(__file__).parents[1] / 'update_market_daily.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def frame(day='20261028'):
    return pd.DataFrame([dict(ts_code='000001.SZ', trade_date=day, open=10., high=11., low=9., close=10.,
                             vol=100., amount=100., adj_factor=1., pe_ttm=None, pb=1., total_mv=100., turnover_rate=1.)])


def test_next_run_uses_shanghai_timezone():
    now = datetime.fromisoformat('2026-10-29T00:30:00+00:00')
    assert m.next_run(now, '09:00').isoformat() == '2026-10-29T09:00:00+08:00'
    assert m.next_run(datetime.fromisoformat('2026-10-29T01:00:00+00:00'), '09:00').day == 30


@pytest.mark.parametrize('today,expected', [(date(2026, 10, 29), '20261028'), (date(2026, 11, 2), '20261030'),
                                          (date(2026, 10, 5), '20260930')])
def test_previous_trading_day_weekend_and_holiday(tmp_path, monkeypatch, today, expected):
    def calendar(pro, start, end):
        return [d.strftime('%Y%m%d') for d in pd.bdate_range(start, end)
                if not date(2026, 10, 1) <= d.date() <= date(2026, 10, 7)]
    monkeypatch.setattr(m, 'trading_days', calendar)
    saved = []
    monkeypatch.setattr(m, 'download_day', lambda pro, directory, day: saved.append(day))
    m.update(None, tmp_path, tmp_path / 'state.json', today=today)
    assert saved == [expected]


def test_outage_catchup_and_failure_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(m, 'trading_days', lambda pro, start, end: pd.bdate_range(start, end).strftime('%Y%m%d').tolist())
    state = tmp_path / 'state.json'
    state.write_text('{"last_success": "20261026"}')
    saved = []
    def download(pro, directory, day):
        if day == '20261028':
            raise ValueError('not ready')
        saved.append(day)
    monkeypatch.setattr(m, 'download_day', download)
    with pytest.raises(ValueError):
        m.update(None, tmp_path, state, today=date(2026, 10, 29))
    assert saved == ['20261026', '20261027']
    assert '20261027' in state.read_text()


def test_validation_atomic_write_and_skip(tmp_path):
    output = tmp_path / '20261028.parquet'
    m.atomic_write(frame(), output)
    m.download_day(None, tmp_path, '20261028')  # no API call for a valid existing file
    original = output.read_bytes()
    with pytest.raises(ValueError):
        m.atomic_write(frame().assign(adj_factor=None), output)
    assert output.read_bytes() == original
    assert not list(tmp_path.glob('*.tmp'))
    with pytest.raises(ValueError):
        m.validate_frame(pd.concat([frame(), frame()]), '20261028')


def test_partial_response_not_written(tmp_path, monkeypatch):
    def daily(pro, name, day, fields=''):
        if name == 'daily':
            return frame().drop(columns=m.VALUATION + ['adj_factor'])
        if name == 'daily_basic':
            return frame()[m.KEYS + m.VALUATION]
        return frame()[m.KEYS + ['adj_factor']].assign(adj_factor=None)
    monkeypatch.setattr(m, 'daily_table', daily)
    with pytest.raises(ValueError):
        m.download_day(None, tmp_path, '20261028')
    assert not list(tmp_path.glob('*.parquet'))


def test_pagination_and_duplicate_rejection(monkeypatch):
    def query(pro, name, **kw):
        offset = kw['offset']
        return pd.DataFrame({'ts_code': [str(i) for i in range(offset, min(offset + 1000, 1002))],
                             'trade_date': '20261028'})
    monkeypatch.setattr(m, 'request', query)
    assert len(m.daily_table(None, 'daily', '20261028')) == 1002
    monkeypatch.setattr(m, 'request', lambda *a, **kw: pd.concat([frame(), frame()]))
    with pytest.raises(ValueError, match='重复'):
        m.daily_table(None, 'daily', '20261028')


def test_calendar_rejects_empty_and_retry(monkeypatch):
    monkeypatch.setattr(m.time, 'sleep', lambda *a: None)
    class Fake:
        calls = 0
        def trade_cal(self, **kwargs):
            self.calls += 1
            if self.calls < 3:
                raise ConnectionError()
            return pd.DataFrame()
    pro = Fake()
    with pytest.raises(ValueError, match='日历不完整'):
        m.trading_days(pro, '20261028', '20261028')
    assert pro.calls == 3
