"""Incremental A-share download and daily scheduler; see update_market_daily.md."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import hashlib
import json
import logging
from logging.handlers import TimedRotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

from filelock import FileLock, Timeout
import numpy as np
import pandas as pd
import tushare as ts

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.qlib_workbench' / 'daily_download'
TOKEN_FILE = ROOT / '.qlib_workbench' / 'secrets' / 'tushare_token'
TZ = ZoneInfo('Asia/Shanghai')
KEYS = ['ts_code', 'trade_date']
VALUATION = ['pe_ttm', 'pb', 'total_mv', 'turnover_rate']
REQUIRED = KEYS + ['open', 'high', 'low', 'close', 'vol', 'amount', 'adj_factor'] + VALUATION
LOG = logging.getLogger('daily_download')


def request(pro, name, **kwargs):
    for attempt in range(4):
        try:
            frame = getattr(pro, name)(**kwargs)
            if not isinstance(frame, pd.DataFrame):
                raise ValueError('API did not return a table')
            return frame
        except Exception as exc:
            # Do not put response bodies, credentials or request URLs in logs.
            LOG.warning('%s 请求失败 (%s)，第 %s/4 次', name, type(exc).__name__, attempt + 1)
            if attempt == 3:
                raise RuntimeError(f'{name} 请求失败，请检查网络、Token 或接口权限') from None
            time.sleep(2 ** (attempt + 1))
        finally:
            time.sleep(0.5)


def daily_table(pro, name, day, fields=''):
    chunks = []
    # Paginate to avoid silently truncating a growing market at the API row limit.
    for offset in range(0, 100000, 1000):
        part = request(pro, name, trade_date=day, fields=fields, limit=1000, offset=offset)
        if part.empty:
            break
        if not set(KEYS).issubset(part.columns):
            raise ValueError(f'{name} 缺少股票代码或日期')
        chunks.append(part)
        if len(part) < 1000:
            break
    else:
        raise ValueError(f'{name} 分页未结束')
    if not chunks:
        raise ValueError(f'{day} {name} 返回空数据，暂不生成文件')
    frame = pd.concat(chunks, ignore_index=True)
    if frame.duplicated(KEYS).any() or not frame.trade_date.astype(str).eq(day).all():
        raise ValueError(f'{name} 返回重复记录或错误日期')
    return frame


def validate_frame(frame, day):
    if frame.empty or not set(REQUIRED).issubset(frame.columns):
        raise ValueError('行情为空或缺少必需字段')
    if frame[KEYS].isna().any().any() or frame.duplicated(KEYS).any():
        raise ValueError('股票日期为空或重复')
    if not frame.trade_date.astype(str).eq(day).all():
        raise ValueError('文件日期与行情日期不一致')
    positive = ['open', 'high', 'low', 'close', 'adj_factor']
    numeric = frame[positive + ['vol', 'amount']].apply(pd.to_numeric, errors='coerce')
    if not np.isfinite(numeric.to_numpy()).all() or (numeric[positive] <= 0).any().any():
        raise ValueError('无效价格或缺失复权因子')
    if (numeric[['vol', 'amount']] < 0).any().any():
        raise ValueError('成交量或成交额小于零')
    # Valuation nulls are intentional (e.g. unavailable PE); do not fill with zero.


def atomic_write(frame, output):
    fd, name = tempfile.mkstemp(prefix=output.stem + '.', suffix='.tmp', dir=output.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        frame.to_parquet(temporary, index=False)
        validate_frame(pd.read_parquet(temporary), output.stem)
        with temporary.open('rb') as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def download_day(pro, directory, day):
    output = directory / f'{day}.parquet'
    if output.exists():
        try:
            validate_frame(pd.read_parquet(output), day)
        except (ValueError, OSError):
            LOG.warning('%s 已有文件不完整，重新下载后再替换', day)
        else:
            LOG.info('%s 已存在有效文件，跳过', day)
            return
    prices = daily_table(pro, 'daily', day)
    valuation = daily_table(pro, 'daily_basic', day, ','.join(KEYS + VALUATION))
    factors = daily_table(pro, 'adj_factor', day, ','.join(KEYS + ['adj_factor']))
    frame = prices.merge(valuation, on=KEYS, how='left', validate='one_to_one', indicator=True)
    if not frame['_merge'].eq('both').all():
        raise ValueError(f'{day} 部分股票的每日指标尚未就绪')
    frame = frame.drop(columns='_merge').merge(factors, on=KEYS, how='left', validate='one_to_one')
    validate_frame(frame, day)
    atomic_write(frame, output)
    LOG.info('%s 下载完成：%s 行 → %s', day, len(frame), output)


def trading_days(pro, start, end):
    # Chunk calendars by year to stay below endpoint response limits during recovery.
    days = []
    cursor = pd.Timestamp(start)
    finish = pd.Timestamp(end)
    while cursor <= finish:
        stop = min(finish, pd.Timestamp(year=cursor.year, month=12, day=31))
        frame = request(pro, 'trade_cal', exchange='SSE', start_date=cursor.strftime('%Y%m%d'),
                        end_date=stop.strftime('%Y%m%d'))
        expected = set(pd.date_range(cursor, stop).strftime('%Y%m%d'))
        if not {'cal_date', 'is_open'}.issubset(frame.columns) or set(frame.cal_date.astype(str)) != expected:
            raise ValueError('交易日历不完整，拒绝猜测交易日')
        days.extend(frame.loc[frame.is_open.astype(str).eq('1'), 'cal_date'].astype(str))
        cursor = stop + pd.Timedelta(days=1)
    return sorted(set(days))


def update(pro, directory, state_file, today=None, start_date=None):
    today = today or datetime.now(TZ).date()
    yesterday = today - timedelta(days=1)
    # Strictly earlier than today's Shanghai date, even when run after market close.
    days = trading_days(pro, today - timedelta(days=60), yesterday)
    if not days:
        raise ValueError('最近60天没有交易日，请检查交易日历')
    target = days[-1]
    previous = json.loads(state_file.read_text()).get('last_success') if state_file.exists() else None
    start = start_date or previous or target
    if start > target:
        return
    pending = trading_days(pro, start, target) if start < days[0] else [d for d in days if d >= start]
    LOG.info('上一个交易日：%s；待检查 %s 个交易日', target, len(pending))
    for day in pending:
        download_day(pro, directory, day)
        temporary = state_file.with_suffix('.tmp')
        temporary.write_text(json.dumps({'last_success': day, 'directory': str(directory)}))
        os.replace(temporary, state_file)


def next_run(now, at):
    hour, minute = map(int, at.split(':'))
    target = now.astimezone(TZ).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


def run(args):
    directory = args.data_dir.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(str(directory).encode()).hexdigest()[:12]
    state_file = RUNTIME / f'{key}.json'
    token = os.environ.get('TUSHARE_TOKEN') or args.token_file.read_text().strip()
    if not token:
        raise ValueError('请设置 TUSHARE_TOKEN 或本机 Token 文件')
    pro = ts.pro_api(token)
    def once():
        with FileLock(str(directory / '.daily_download.lock'), timeout=0):
            update(pro, directory, state_file, start_date=args.start_date)
    if not args.schedule:
        once()
        return
    with FileLock(str(RUNTIME / f'{key}.scheduler.lock'), timeout=0):
        pid_file = RUNTIME / f'{key}.pid'
        pid_file.write_text(str(os.getpid()))
        try:
            while True:
                try:
                    once()
                    wake = next_run(datetime.now(TZ), args.at)
                except Exception as exc:
                    LOG.error('更新失败 (%s)，15分钟后重试；已有文件保持不变', type(exc).__name__)
                    wake = datetime.now(TZ) + timedelta(minutes=15)
                LOG.info('下次执行：%s', wake.isoformat())
                while datetime.now(TZ) < wake:
                    time.sleep(min(30, max(0, (wake - datetime.now(TZ)).total_seconds())))
        finally:
            pid_file.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data/market_daily')
    parser.add_argument('--token-file', type=Path, default=TOKEN_FILE)
    parser.add_argument('--start-date', help='补齐指定日期起的交易日，格式 YYYYMMDD；默认从上次成功日期恢复')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--schedule', action='store_true', help='前台常驻，每日运行；启动时立即更新一次')
    mode.add_argument('--start-scheduler', action='store_true', help='在后台启动每日任务')
    parser.add_argument('--at', default='09:00', help='北京时间 HH:MM，默认09:00')
    args = parser.parse_args()
    try:
        datetime.strptime(args.at, '%H:%M')
        if args.start_date:
            datetime.strptime(args.start_date, '%Y%m%d')
    except ValueError:
        parser.error('时间必须为 HH:MM，日期必须为 YYYYMMDD')
    RUNTIME.mkdir(parents=True, exist_ok=True)
    if args.start_scheduler:
        forwarded = [a for a in sys.argv[1:] if a != '--start-scheduler']
        with (RUNTIME / 'launcher.log').open('ab') as log:
            process = subprocess.Popen([sys.executable, '-u', str(Path(__file__).resolve()), '--schedule', *forwarded],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        time.sleep(1)
        if process.poll() is not None:
            print('启动失败或已有任务，请查看 .qlib_workbench/daily_download/launcher.log', file=sys.stderr)
            return 1
        print(f'后台任务已启动，PID={process.pid}，每天北京时间 {args.at}；日志：{RUNTIME / "download.log"}')
        return 0
    handler = TimedRotatingFileHandler(RUNTIME / 'download.log', when='midnight', backupCount=14, encoding='utf-8')
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    formatter.converter = lambda seconds: datetime.fromtimestamp(seconds, TZ).timetuple()
    handler.setFormatter(formatter)
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    if not args.schedule:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        LOG.addHandler(console)
    try:
        run(args)
    except Timeout:
        LOG.error('同一数据目录已有下载/定时任务运行，本次退出')
        return 1
    except Exception as exc:
        LOG.error('任务失败 (%s)，详见上面的接口或数据检查信息', type(exc).__name__)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
