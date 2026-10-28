"""Read growing Tushare directories and build immutable Qlib providers."""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from filelock import FileLock

REQUIRED = {"ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount", "adj_factor"}
PRICE_FIELDS = ["open", "high", "low", "close", "pre_close"]


def symbol(value: str) -> str:
    value = value.strip().upper()
    match = re.fullmatch(r"(SH|SZ|BJ)(\d{6})", value)
    if match:
        return value
    match = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ|XSHG|XSHE|XBSE)", value)
    if not match:
        raise ValueError(f"无效股票代码：{value}")
    code, exchange = match.groups()
    return {"XSHG": "SH", "XSHE": "SZ", "XBSE": "BJ"}.get(exchange, exchange) + code


def scan(directory: str | Path) -> dict:
    directory = Path(directory).expanduser().resolve()
    files, errors, securities = [], [], set()
    for path in sorted(directory.glob("*.parquet")):
        try:
            date = pd.to_datetime(path.stem, format="%Y%m%d").strftime("%Y-%m-%d")
            before = path.stat()
            table = pq.read_table(path, columns=["ts_code"])
            missing = REQUIRED - set(pq.read_schema(path).names)
            if missing or not table.num_rows:
                raise ValueError(f"缺少字段 {sorted(missing)} 或文件为空")
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("文件正在写入")
            securities.update(symbol(x) for x in table.column(0).unique().to_pylist())
            files.append({"path": str(path), "date": date, "size": after.st_size, "mtime": after.st_mtime_ns})
        except Exception as exc:
            errors.append({"文件": path.name, "原因": str(exc)})
    segments = []
    for item in files:
        date = item["date"]
        if not segments or (pd.Timestamp(date) - pd.Timestamp(segments[-1][-1])).days > 20:
            segments.append([])
        segments[-1].append(date)
    return {"directory": str(directory), "files": files, "errors": errors,
            "symbols": sorted(securities), "segments": segments}


def freeze_manifest(catalog: dict, start: str, end: str, codes: list[str]) -> dict:
    start, end = str(start), str(end)
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    segment = next((s for s in catalog["segments"] if s[0] <= start <= end <= s[-1]), None)
    if segment is None:
        raise ValueError("请选择同一数据段内的日期，不能跨越超过 20 天的文件缺口")
    run_dates = [d for d in segment if start <= d <= end]
    if not run_dates or not any(d < run_dates[0] for d in segment):
        raise ValueError("回测至少需要一个交易日及一个更早的行情日")
    codes = sorted(set(symbol(s) for s in codes))
    if not codes or set(codes) - set(catalog["symbols"]):
        raise ValueError("股票池为空或包含数据目录中不存在的股票")
    entries = []
    for item in catalog["files"]:
        if segment[0] <= item["date"] <= end:
            path = Path(item["path"])
            before = path.stat()
            content = path.read_bytes()
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError(f"{path.name} 正在写入，请刷新后重试")
            entries.append({**item, "sha256": hashlib.sha256(content).hexdigest()})
    return {"version": 2, "files": entries, "symbols": codes, "dates": [x["date"] for x in entries]}


def snapshot(manifest: dict, cache_root: Path, check_cancel=lambda: None) -> Path:
    key = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    cache_root.mkdir(parents=True, exist_ok=True)
    target = cache_root / key
    with FileLock(str(cache_root / f"{key}.lock")):
        if (target / "ready.json").exists():
            return target
        temporary = Path(tempfile.mkdtemp(prefix="building-", dir=cache_root))
        try:
            frames = []
            for entry in manifest["files"]:
                check_cancel()
                content = Path(entry["path"]).read_bytes()
                if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                    raise ValueError(f"数据已变化：{entry['path']}，请重新提交任务")
                frame = pd.read_parquet(io.BytesIO(content))
                frame["instrument"] = frame.ts_code.map(symbol)
                frame = frame[frame.instrument.isin(manifest["symbols"])].copy()
                frame["datetime"] = pd.to_datetime(frame.trade_date.astype(str), format="%Y%m%d")
                if not frame.empty and not (frame.datetime == pd.Timestamp(entry["date"])).all():
                    raise ValueError(f"文件日期与数据日期不符：{entry['path']}")
                frames.append(frame)
            raw = pd.concat(frames, ignore_index=True)
            if raw.empty:
                raise ValueError("所选区间没有股票池行情")
            if raw.duplicated(["datetime", "instrument"]).any():
                raise ValueError("存在重复股票日期，请先清理数据")
            for field in REQUIRED - {"ts_code", "trade_date"}:
                raw[field] = pd.to_numeric(raw[field], errors="coerce")
                if not np.isfinite(raw[field]).all():
                    raise ValueError(f"{field} 存在缺失或非有限值")
            if (raw[["open", "high", "low", "close", "adj_factor"]] <= 0).any().any():
                raise ValueError("价格和复权因子必须大于零")
            if (raw[["vol", "amount"]] < 0).any().any():
                raise ValueError("成交量和成交额不能为负")
            if ((raw.high < raw[["open", "close", "low"]].max(axis=1)) |
                    (raw.low > raw[["open", "close", "high"]].min(axis=1))).any():
                raise ValueError("OHLC 价格范围不一致")
            raw["volume"] = raw.vol * 100
            raw["amount"] = raw.amount * 1000
            raw = raw.sort_values(["datetime", "instrument"]).set_index(["datetime", "instrument"])
            raw.to_parquet(temporary / "raw.parquet")
            dates = pd.DatetimeIndex(manifest["dates"])
            provider = temporary / "provider"
            (provider / "calendars").mkdir(parents=True)
            (provider / "instruments").mkdir()
            (provider / "calendars/day.txt").write_text("\n".join(manifest["dates"]) + "\n")
            # Qlib needs a right-hand endpoint for its final daily interval.
            # This sentinel is only in the future calendar, never a market-data day.
            endpoint = (dates[-1] + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            (provider / "calendars/day_future.txt").write_text("\n".join(manifest["dates"] + [endpoint]) + "\n")
            spans = []
            for code, stock in raw.groupby(level="instrument"):
                check_cancel()
                stock = stock.droplevel("instrument").reindex(dates)
                factor = stock.adj_factor / stock.adj_factor.dropna().iloc[0]
                stock["factor"] = factor
                for field in ("open", "high", "low", "close"):
                    stock[field] *= factor
                stock["volume"] /= factor
                stock["change"] = stock["close"].pct_change(fill_method=None)
                stock["paused"] = (stock.close.isna() | stock.volume.le(0)).astype(float)
                folder = provider / "features" / code.lower()
                folder.mkdir(parents=True)
                for field in ("open", "high", "low", "close", "volume", "factor", "change", "paused"):
                    np.concatenate(([0], stock[field].to_numpy())).astype("<f4").tofile(folder / f"{field}.day.bin")
                spans.append(f"{code}\t{dates[0]:%Y-%m-%d}\t{dates[-1]:%Y-%m-%d}\n")
            (provider / "instruments/all.txt").write_text("".join(spans))
            (temporary / "ready.json").write_text(json.dumps(manifest, ensure_ascii=False))
            temporary.rename(target)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return target
