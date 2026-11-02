"""Run one trusted strategy outside Streamlit's rerun process."""
import json
import logging
import sys
import time
import traceback

from .data import snapshot
from .store import Store


class Cancelled(Exception):
    pass


def main(root, run_id):
    store = Store(root)
    folder = store.directory(run_id)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", force=True)
    def check_cancel():
        if (folder / "cancel").exists():
            raise Cancelled()
    def progress(value, message):
        check_cancel()
        store.update(run_id, progress=value, message=message)
    try:
        started = time.perf_counter()
        check_cancel()
        manifest = json.loads((folder / "manifest.json").read_text())
        config = json.loads((folder / "config.json").read_text())
        store.update(run_id, message="正在准备固定数据快照…")
        data_dir = snapshot(manifest, store.root / "snapshots", check_cancel)
        snapshot_seconds = time.perf_counter() - started
        check_cancel()
        from .engine import run_backtest
        store.update(run_id, status="running", message="开始 Qlib 回测")
        tables, metrics = run_backtest((folder / "strategy.py").read_text(), data_dir, config, progress, check_cancel)
        check_cancel()
        for name, frame in tables.items():
            frame.to_parquet(folder / f"{name}.parquet", index=False)
        metrics["timings"].update(snapshot_seconds=snapshot_seconds,
                                  worker_seconds=time.perf_counter() - started)
        logging.info("任务总耗时 %.2f 秒（数据快照 %.2f 秒）",
                     metrics["timings"]["worker_seconds"], snapshot_seconds)
        (folder / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, allow_nan=False))
        check_cancel()
        store.update(run_id, status="success", progress=1, message="回测完成")
    except Cancelled:
        store.update(run_id, status="cancelled", message="已停止；部分结果不作为完整回测")
    except BaseException as exc:
        traceback.print_exc()
        store.update(run_id, status="failed", message=f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main(*sys.argv[1:])
