"""Persistent local strategies, immutable runs and owned subprocess lifecycle."""
from __future__ import annotations

import ast
import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import psutil
from filelock import FileLock

from .data import symbol

ACTIVE = ("preparing", "running")
LABELS = {"preparing": "准备中", "running": "运行中", "success": "成功", "failed": "失败", "cancelled": "已取消"}
PROJECT = Path(__file__).resolve().parents[2]
_CHILDREN = {}


def validate(source, config):
    tree = ast.parse(source, filename="strategy.py")
    if not any(isinstance(n, ast.FunctionDef) and n.name == "handle_data" for n in tree.body):
        raise ValueError("请定义 handle_data(context, data)")
    cfg = dict(config)
    for field in ("capital", "open_cost", "close_cost", "min_cost"):
        cfg[field] = float(cfg[field])
        if not np.isfinite(cfg[field]) or cfg[field] < 0:
            raise ValueError(f"{field} 必须为非负有限值")
    if cfg["capital"] <= 0 or max(cfg["open_cost"], cfg["close_cost"]) >= 1:
        raise ValueError("本金必须大于零，费率必须小于 1")
    cfg["start"], cfg["end"] = str(pd.Timestamp(cfg["start"]).date()), str(pd.Timestamp(cfg["end"]).date())
    if cfg["start"] > cfg["end"]:
        raise ValueError("开始日期晚于结束日期")
    cfg["symbols"] = list(dict.fromkeys(symbol(s) for s in cfg["symbols"]))
    if not cfg["symbols"]:
        raise ValueError("股票池不能为空")
    cfg["benchmark"] = symbol(cfg["benchmark"]) if cfg.get("benchmark") else None
    if not isinstance(cfg.get("params", {}), dict):
        raise ValueError("策略参数必须为 JSON 对象")
    json.dumps(cfg, allow_nan=False)
    return cfg


class Store:
    def __init__(self, root=None):
        self.root = Path(root or os.environ.get("QLIB_WORKBENCH_HOME", PROJECT / ".qlib_workbench")).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "runs").mkdir(exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS strategies (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, source TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, strategy_id TEXT, name TEXT NOT NULL, status TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, progress REAL DEFAULT 0,
                message TEXT DEFAULT '', pid INTEGER, process_start REAL, cancel_at REAL,
                config TEXT NOT NULL);
            """)

    def connect(self):
        db = sqlite3.connect(self.root / "index.sqlite", timeout=20)
        db.row_factory = sqlite3.Row
        return db

    def strategies(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM strategies ORDER BY updated DESC")]

    def save_strategy(self, name, source, strategy_id=None):
        name = name.strip()
        if not name:
            raise ValueError("策略名称不能为空")
        strategy_id = strategy_id or uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO strategies VALUES (?, ?, ?, ?)", (strategy_id, name, source, time.time()))
        return strategy_id

    def delete_strategy(self, strategy_id):
        with self.connect() as db:
            db.execute("DELETE FROM strategies WHERE id = ?", (strategy_id,))

    def runs(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM runs ORDER BY created DESC")]

    def get_run(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("回测记录不存在")
        return dict(row)

    def directory(self, run_id):
        if not isinstance(run_id, str) or not all(c in "0123456789abcdef" for c in run_id) or len(run_id) != 32:
            raise ValueError("无效任务编号")
        return self.root / "runs" / run_id

    def update(self, run_id, **fields):
        if set(fields) - {"status", "progress", "message", "pid", "process_start", "cancel_at"}:
            raise ValueError("无效任务字段")
        fields["updated"] = time.time()
        with self.connect() as db:
            db.execute(f"UPDATE runs SET {', '.join(k + ' = ?' for k in fields)} WHERE id = ?",
                       (*fields.values(), run_id))

    def submit(self, strategy_id, name, source, config, manifest):
        config = validate(source, config)
        needed = set(config["symbols"]) | ({config["benchmark"]} if config["benchmark"] else set())
        if not needed.issubset(manifest["symbols"]):
            raise ValueError("快照不包含全部股票池和基准证券")
        with FileLock(str(self.root / "submission.lock")):
            self.reconcile()
            if any(r["status"] in ACTIVE for r in self.runs()):
                raise ValueError("已有回测正在运行，请等待完成或停止任务")
            run_id = uuid.uuid4().hex
            directory = self.directory(run_id)
            directory.mkdir()
            (directory / "strategy.py").write_text(source, encoding="utf-8")
            for file, content in (("config.json", config), ("manifest.json", manifest)):
                (directory / file).write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
            now = time.time()
            with self.connect() as db:
                db.execute("INSERT INTO runs (id,strategy_id,name,status,created,updated,config) VALUES (?,?,?,?,?,?,?)",
                           (run_id, strategy_id, name, "preparing", now, now, json.dumps(config)))
            try:
                with (directory / "run.log").open("ab") as output:
                    process = subprocess.Popen([sys.executable, "-u", "-m", "apps.quant_workbench.worker",
                                                str(self.root), run_id], cwd=PROJECT,
                                               stdout=output, stderr=subprocess.STDOUT,
                                               start_new_session=True)
                _CHILDREN[run_id] = process
                self.update(run_id, pid=process.pid, process_start=psutil.Process(process.pid).create_time())
            except Exception:
                self.update(run_id, status="failed", message="无法启动回测子进程")
                raise
            return run_id

    @staticmethod
    def owned_process(run):
        try:
            process = psutil.Process(run["pid"])
            if abs(process.create_time() - run["process_start"]) < 0.01 and process.status() != psutil.STATUS_ZOMBIE:
                return process
        except (psutil.Error, TypeError):
            pass
        return None

    def reconcile(self):
        for key, child in list(_CHILDREN.items()):
            if child.poll() is not None:
                del _CHILDREN[key]
        for run in self.runs():
            if run["status"] not in ACTIVE:
                continue
            process = self.owned_process(run)
            if process is None and (run["pid"] or time.time() - run["created"] > 30):
                self.update(run["id"], status="cancelled" if run["cancel_at"] else "failed",
                            message="任务已停止" if run["cancel_at"] else "子进程异常退出；请查看日志")
            elif process and run["cancel_at"] and time.time() - run["cancel_at"] >= 5:
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        for child in process.children(recursive=True):
                            child.kill()
                        process.kill()
                    process.wait(timeout=2)
                except (ProcessLookupError, psutil.Error):
                    pass
                self.update(run["id"], status="cancelled", message="任务已强制停止，部分结果不作为完整回测")

    def cancel(self, run_id):
        run = self.get_run(run_id)
        if run["status"] not in ACTIVE or run["cancel_at"]:
            return
        (self.directory(run_id) / "cancel").touch()
        self.update(run_id, cancel_at=time.time(), message="正在停止…")
        def enforce():
            time.sleep(5.1)
            self.reconcile()
        threading.Thread(target=enforce, daemon=True).start()

    def archive(self, run_id):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in self.directory(run_id).iterdir():
                if path.is_file() and path.name != "cancel":
                    archive.write(path, path.name)
                    if path.suffix == ".parquet":
                        archive.writestr(path.stem + ".csv", pd.read_parquet(path).to_csv(index=False))
        return buffer.getvalue()
