"""Content-keyed cache for the independent, fixed buy-and-hold account only."""
from contextlib import contextmanager
import hashlib
import json
import logging
from pathlib import Path
import platform
import shutil
import tempfile

from filelock import FileLock, Timeout
import numpy as np
import pandas as pd
import qlib

TABLES = ('report', 'trades', 'holdings', 'records')
FILES = tuple(name + '.parquet' for name in TABLES) + ('metrics.json',)
LOGGER = logging.getLogger(__name__)


def cache_identity(snapshot_dir, config, source):
    """Parameters unrelated to the passive account deliberately do not enter this key."""
    snapshot_dir = Path(snapshot_dir).resolve()
    ready = snapshot_dir / 'ready.json'
    if not ready.exists():
        return None  # Unversioned/custom providers must be recomputed.
    qlib_root = Path(qlib.__file__).resolve().parent
    local = Path(__file__).parent
    files = sorted(qlib_root.rglob('*.py')) + [
        local / 'engine.py', local / 'metrics.py', local / 'data.py', Path(__file__)]
    implementation = hashlib.sha256()
    for path in files:
        implementation.update(str(path).encode())
        implementation.update(path.read_bytes())
    payload = dict(version=1, snapshot=hashlib.sha256(ready.read_bytes()).hexdigest(),
        provider=str(snapshot_dir), source=source, implementation=implementation.hexdigest(),
        # Qlib sums positions in set iteration order. Different Python hash seeds can
        # change final floating-point bits; never mix those execution environments.
        runtime=[platform.python_version(), qlib.__version__, np.__version__, pd.__version__,
                 hash('qlib_workbench_position_order')],
        config={key: config[key] for key in ('start', 'end', 'symbols', 'capital', 'open_cost', 'close_cost', 'min_cost')})
    key = hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return snapshot_dir.parent.parent / 'comparison_cache' / key


@contextmanager
def cancellable_lock(path, check_cancel):
    lock = FileLock(str(path))
    while True:
        check_cancel()
        try:
            lock.acquire(timeout=.2)
            break
        except Timeout:
            continue
    try:
        yield
    finally:
        lock.release()


def _read(directory):
    meta = json.loads((directory / 'complete.json').read_text())
    if meta['key'] != directory.name:
        raise ValueError('comparison cache key mismatch')
    for name in FILES:
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != meta['sha256'][name]:
            raise ValueError(f'comparison cache checksum mismatch: {name}')
    tables = {name: pd.read_parquet(directory / f'{name}.parquet') for name in TABLES}
    for frame in tables.values():
        if not len(frame.columns):
            # Parquet loses the column index type of an empty DataFrame().
            frame.columns = pd.RangeIndex(0)
    return tables, json.loads((directory / 'metrics.json').read_text())


def load_or_compute(directory, compute, check_cancel):
    """Return (tables, metrics, hit). Cancellation/failure can never publish a partial entry."""
    if directory is None:
        tables, stats = compute()
        return tables, stats, False
    directory.parent.mkdir(parents=True, exist_ok=True)
    with cancellable_lock(directory.with_suffix('.lock'), check_cancel):
        check_cancel()
        if directory.exists():
            try:
                tables, stats = _read(directory)
            except (OSError, ValueError, KeyError, TypeError):
                LOGGER.warning('买入持有缓存不完整或已损坏，重新计算：%s', directory.name)
                shutil.rmtree(directory)
            else:
                check_cancel()
                LOGGER.info('复用买入持有对照缓存：%s', directory.name)
                return tables, stats, True
        tables, stats = compute()
        check_cancel()
        temporary = Path(tempfile.mkdtemp(prefix='building-', dir=directory.parent))
        try:
            for name in TABLES:
                check_cancel()
                tables[name].to_parquet(temporary / f'{name}.parquet', index=False)
            (temporary / 'metrics.json').write_text(json.dumps(stats, ensure_ascii=False, allow_nan=False))
            meta = dict(key=directory.name, sha256={
                name: hashlib.sha256((temporary / name).read_bytes()).hexdigest() for name in FILES})
            (temporary / 'complete.json').write_text(json.dumps(meta))
            check_cancel()
            temporary.rename(directory)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return tables, stats, False
