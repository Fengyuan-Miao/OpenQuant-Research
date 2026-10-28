"""Launch: streamlit run apps/quant_workbench/app.py"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from streamlit_ace import st_ace

from apps.quant_workbench.data import scan, freeze_manifest, symbol
from apps.quant_workbench.store import Store, LABELS, ACTIVE, validate
from apps.quant_workbench.templates import TEMPLATES, API_HELP, ASSUMPTIONS

st.set_page_config(page_title="Qlib 策略工作台", page_icon="📈", layout="wide")
st.markdown("""<style>
.block-container {padding-top: 2rem; max-width: 1600px;}
[data-testid="stMetric"] {background: white; border: 1px solid #e4eaf3; border-radius: 10px; padding: 16px;}
[data-testid="stSidebar"] {border-right: 1px solid #e4eaf3;}
h1 {font-size: 1.9rem !important;} h3 {font-size: 1.1rem !important;}
</style>""", unsafe_allow_html=True)
store = Store()
store.reconcile()
TABLE_LABELS = {
    "datetime": "日期", "instrument": "股票代码", "account": "总资产（元）",
    "cash": "现金（元）", "value": "市值/成交额（元）", "amount": "股数",
    "price": "价格（元）", "direction": "方向", "requested": "委托股数",
    "status": "状态", "reason": "说明", "nav": "净值（扣费）",
    "daily_return": "日收益率（扣费）", "drawdown": "回撤", "return": "日收益率（扣费前）",
    "cost": "费用", "total_cost": "累计费用（元）", "turnover": "换手率",
    "total_turnover": "累计成交额（元）", "benchmark_nav": "基准净值",
    "benchmark_daily_return": "基准日收益率",
}


def navigate(page, strategy_id=None, run_id=None):
    st.session_state.pending_page = page
    if strategy_id:
        st.session_state.selected_strategy = strategy_id
    if run_id:
        st.session_state.selected_run = run_id
    st.rerun()


def catalog(refresh=False):
    path = st.session_state.get("data_path", os.environ.get("QLIB_WORKBENCH_DATA", str(ROOT / "db/data/market_daily")))
    if refresh or "catalog" not in st.session_state or st.session_state.catalog["directory"] != str(Path(path).expanduser().resolve()):
        with st.spinner("正在扫描已完成的行情文件…"):
            st.session_state.catalog = scan(path)
    return st.session_state.catalog


def timestamp(value):
    return pd.Timestamp(value, unit="s", tz="UTC").tz_convert("Asia/Shanghai").strftime("%m-%d %H:%M:%S")


def strategies_page():
    st.title("我的策略")
    st.caption("从一个想法开始，编写策略，用历史数据验证。")
    with st.expander("＋ 新建策略", expanded=not store.strategies()):
        with st.form("create_strategy"):
            cols = st.columns([2, 1])
            name = cols[0].text_input("策略名称", value="我的新策略")
            template = cols[1].selectbox("策略模板", list(TEMPLATES))
            if st.form_submit_button("创建并编辑", type="primary"):
                try:
                    strategy_id = store.save_strategy(name, TEMPLATES[template])
                    navigate("策略编辑", strategy_id=strategy_id)
                except ValueError as exc:
                    st.error(str(exc))
    strategies = store.strategies()
    if not strategies:
        st.info("选择模板创建第一个策略。模板可直接运行，也可以自由修改。")
    query = st.text_input("搜索策略", placeholder="输入策略名称")
    for strategy in strategies:
        if query.lower() not in strategy["name"].lower():
            continue
        with st.container(border=True):
            cols = st.columns([4, 2, 1, 1])
            cols[0].markdown(f"**{strategy['name']}**")
            cols[1].caption("更新于 " + timestamp(strategy["updated"]))
            if cols[2].button("编辑", key="edit_" + strategy["id"]):
                navigate("策略编辑", strategy_id=strategy["id"])
            if cols[3].button("复制", key="copy_" + strategy["id"]):
                sid = store.save_strategy(strategy["name"] + " 副本", strategy["source"])
                navigate("策略编辑", strategy_id=sid)
            with st.expander("删除策略"):
                st.caption("删除策略不会删除已完成的回测和源码快照。")
                if st.button("确认删除", key="delete_" + strategy["id"]):
                    store.delete_strategy(strategy["id"])
                    st.rerun()


def editor_page():
    strategies = store.strategies()
    if not strategies:
        st.title("策略编辑")
        st.info("请先在“我的策略”中新建策略。")
        return
    current = next((s for s in strategies if s["id"] == st.session_state.get("selected_strategy")), strategies[0])
    st.session_state.selected_strategy = current["id"]
    st.title("策略编辑")
    st.caption("编辑源码 → 检查语法 → 设置参数 → 运行回测")
    left, right = st.columns([2.4, 1], gap="large")
    sid = current["id"]
    with left:
        name = st.text_input("策略名称", current["name"], key="name_" + sid)
        draft_key = "draft_" + sid
        initial = st.session_state.get(draft_key, current["source"])
        plain = st.toggle("使用普通文本编辑器", help="适合无障碍操作或代码编辑器无法加载的浏览器。")
        if plain:
            source = st.text_area("Python 策略", initial, height=490, key="plain_" + sid)
        else:
            st.caption("Python · 自动同步编辑内容 · 仅运行可信代码")
            source = st_ace(initial, language="python", theme="chrome", height=490, font_size=14,
                            show_gutter=True, wrap=True, auto_update=True, key="ace_" + sid)
            if source is None:
                source = initial
        st.session_state[draft_key] = source
        buttons = st.columns(3)
        if buttons[0].button("保存策略", type="primary", width="stretch"):
            try:
                store.save_strategy(name, source, sid)
                st.success("策略已保存")
            except ValueError as exc:
                st.error(str(exc))
        if buttons[1].button("检查语法", width="stretch"):
            try:
                compile(source, "strategy.py", "exec")
                st.success("Python 语法检查通过")
            except SyntaxError as exc:
                st.error(f"第 {exc.lineno} 行，第 {exc.offset} 列：{exc.msg}")
        if buttons[2].button("另存为副本", width="stretch"):
            try:
                new_id = store.save_strategy(name + " 副本", source)
                navigate("策略编辑", strategy_id=new_id)
            except ValueError as exc:
                st.error(str(exc))
        with st.expander("策略 API 帮助"):
            st.markdown(API_HELP)
    with right:
        st.subheader("回测设置")
        cat = catalog()
        if not cat["segments"]:
            st.warning("没有可用行情，请在数据管理中选择目录并刷新。")
            return
        copied = st.session_state.pop("copied_config", None)
        if copied:
            st.session_state.backtest_defaults = copied
            for key in [k for k in st.session_state if k.startswith("cfg_")]:
                del st.session_state[key]
        defaults = st.session_state.get("backtest_defaults", {})
        latest = cat["segments"][-1]
        start_default = defaults.get("start", latest[max(1, len(latest) - 60)] if len(latest) > 1 else latest[0])
        end_default = defaults.get("end", latest[-1])
        start = st.date_input("开始日期", pd.Timestamp(start_default).date(), key="cfg_start")
        end = st.date_input("结束日期", pd.Timestamp(end_default).date(), key="cfg_end")
        capital = st.number_input("初始资金（元）", min_value=100.0, value=float(defaults.get("capital", 1000000)), step=10000.0, key="cfg_capital")
        preferred = [s for s in ["SZ000001", "SH600000", "SH600519"] if s in cat["symbols"]] or cat["symbols"][:3]
        universe = st.text_area("股票池", value=", ".join(defaults.get("symbols", preferred)),
                                help="用逗号、空格或换行分隔。支持 SZ000001、000001.SZ、000001.XSHE。", key="cfg_symbols")
        benchmark = st.text_input("基准代码（可留空）", value=defaults.get("benchmark") or "", key="cfg_benchmark")
        with st.expander("交易费用与策略参数"):
            buy = st.number_input("买入费率", min_value=0.0, max_value=0.99, value=float(defaults.get("open_cost", 0.0005)), format="%.5f", key="cfg_buy")
            sell = st.number_input("卖出费率", min_value=0.0, max_value=0.99, value=float(defaults.get("close_cost", 0.0015)), format="%.5f", key="cfg_sell")
            minimum = st.number_input("单笔最低费用（元）", min_value=0.0, value=float(defaults.get("min_cost", 5)), key="cfg_min")
            params = st.text_area("策略参数（JSON）", json.dumps(defaults.get("params", {}), ensure_ascii=False), key="cfg_params")
        st.caption(ASSUMPTIONS)
        active = next((r for r in store.runs() if r["status"] in ACTIVE), None)
        if st.button("▶ 运行回测", type="primary", width="stretch", disabled=active is not None):
            try:
                codes = [symbol(s) for s in universe.replace(",", " ").replace("，", " ").split()]
                config = validate(source, {"start": str(start), "end": str(end), "capital": capital,
                    "symbols": codes, "benchmark": benchmark.strip(), "open_cost": buy,
                    "close_cost": sell, "min_cost": minimum, "params": json.loads(params)})
                with st.spinner("固定本次数据清单…"):
                    cat = catalog(refresh=True)
                    manifest = freeze_manifest(cat, config["start"], config["end"],
                                               codes + ([config["benchmark"]] if config["benchmark"] else []))
                    run_id = store.submit(sid, name, source, config, manifest)
                navigate("回测结果", run_id=run_id)
            except SyntaxError as exc:
                st.error(f"第 {exc.lineno} 行：{exc.msg}")
            except (ValueError, OSError) as exc:
                st.error(str(exc))
        if active:
            st.info("已有任务运行中。")
            if st.button("查看运行任务"):
                navigate("回测结果", run_id=active["id"])


def chart(frame, fields, title, percent=False):
    figure = go.Figure()
    for field, label in fields.items():
        if field in frame:
            figure.add_trace(go.Scatter(x=frame.datetime, y=frame[field], name=label, mode="lines"))
    figure.update_layout(title=title, height=340, margin=dict(l=15, r=15, t=45, b=15),
                         paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="white", hovermode="x unified",
                         legend=dict(orientation="h", y=-0.18))
    if percent:
        figure.update_yaxes(tickformat=".1%")
    st.plotly_chart(figure, width="stretch")


def run_view(run_id):
    store.reconcile()
    run = store.get_run(run_id)
    if run["status"] in ACTIVE:
        st.session_state.polled_active = run_id
    elif st.session_state.get("polled_active") == run_id:
        del st.session_state.polled_active
        st.rerun()  # Stop auto-polling after the terminal transition.
    folder = store.directory(run_id)
    cols = st.columns([5, 1])
    cols[0].markdown(f"**{run['name']}** · {LABELS[run['status']]} · {timestamp(run['created'])}")
    if run["status"] in ACTIVE:
        st.progress(min(max(run["progress"], 0), 1), text=run["message"] or "准备中…")
        if cols[1].button("停止任务", disabled=run["cancel_at"] is not None):
            store.cancel(run_id)
            st.rerun(scope="fragment")
    elif run["status"] != "success":
        st.warning(run["message"])
    st.caption(ASSUMPTIONS)
    config = json.loads(run["config"])
    if run["status"] == "success":
        stats = json.loads((folder / "metrics.json").read_text())
        columns = st.columns(5)
        for col, (key, label) in zip(columns, [("total_return", "累计收益"), ("annual_return", "年化收益"),
                                                ("max_drawdown", "最大回撤"), ("sharpe", "夏普比率"), ("volatility", "年化波动")]):
            value = stats[key]
            col.metric(label, "不可计算" if value is None else (f"{value:.2f}" if key == "sharpe" else f"{value:.2%}"))
        if "benchmark_return" in stats:
            st.caption(f"基准收益 {stats['benchmark_return']:.2%} · 超额收益 {stats['excess_return']:.2%}（策略减基准）")
        report = pd.read_parquet(folder / "report.parquet").drop(columns=["bench"], errors="ignore")
        # Plot the initial cash point as well, so first-day losses are visible.
        initial = {"datetime": pd.Timestamp(config["start"]) - pd.Timedelta(days=1), "nav": 1, "drawdown": 0}
        if "benchmark_nav" in report:
            initial["benchmark_nav"] = 1
        plotted = pd.concat([pd.DataFrame([initial]), report], ignore_index=True)
        chart(plotted, {"nav": "策略净值（扣费）", "benchmark_nav": "基准净值"}, "净值走势")
        tabs = st.tabs(["回撤与每日收益", "每日持仓", "成交明细", "自定义指标", "运行日志", "源码与参数"])
        with tabs[0]:
            chart(plotted, {"drawdown": "回撤"}, "回撤", percent=True)
            st.dataframe(report, width="stretch", hide_index=True, column_config={**TABLE_LABELS, "cost": "费用比例"})
        for tab, filename, label in [(tabs[1], "holdings", "持仓"), (tabs[2], "trades", "成交"), (tabs[3], "records", "自定义指标")]:
            with tab:
                frame = pd.read_parquet(folder / f"{filename}.parquet")
                if frame.empty:
                    st.info(f"本次回测没有{label}记录。")
                else:
                    if filename == "holdings":
                        day = st.selectbox("查看日期", sorted(frame.datetime.unique(), reverse=True), format_func=lambda d: str(pd.Timestamp(d).date()))
                        st.dataframe(frame[frame.datetime == day], hide_index=True, width="stretch", column_config=TABLE_LABELS)
                    else:
                        st.dataframe(frame, hide_index=True, width="stretch", column_config=TABLE_LABELS)
                    if filename == "records":
                        chart(frame, {c: c for c in frame if c != "datetime"}, "策略记录")
                    st.download_button(f"导出{label} CSV", frame.to_csv(index=False).encode("utf-8-sig"), f"{filename}.csv", mime="text/csv")
        log_container, source_container = tabs[4], tabs[5]
        st.download_button("导出每日收益 CSV", report.to_csv(index=False).encode("utf-8-sig"), "report.csv", mime="text/csv")
        st.download_button("下载完整回测 ZIP", store.archive(run_id), f"backtest-{run_id[:8]}.zip", mime="application/zip")
    else:
        log_container = st.container()
        source_container = st.expander("本次源码与参数")
    with log_container:
        logfile = folder / "run.log"
        if logfile.exists():
            with logfile.open("rb") as stream:
                stream.seek(max(0, logfile.stat().st_size - 30000))
                log = stream.read().decode("utf-8", errors="replace")
            st.code(log or "等待日志…", language="text")
            if run["status"] not in ACTIVE:
                st.download_button("下载完整日志", logfile.read_bytes(), "run.log", mime="text/plain")
    with source_container:
        st.json(config)
        st.code((folder / "strategy.py").read_text(), language="python")


def results_page():
    st.title("回测结果")
    runs = store.runs()
    if not runs:
        st.info("运行一个策略后，收益曲线和交易记录会显示在这里。")
        return
    selected = st.session_state.get("selected_run", runs[0]["id"])
    refresh = "2s" if store.get_run(selected)["status"] in ACTIVE else None
    st.fragment(run_every=refresh)(run_view)(selected)


def history_page():
    st.title("回测历史")
    st.caption("每次运行均保留独立源码、参数和数据清单。复制配置会使用刷新后的数据创建新任务。")
    runs = store.runs()
    c1, c2 = st.columns(2)
    name = c1.selectbox("策略筛选", ["全部"] + sorted({r["name"] for r in runs}))
    state = c2.selectbox("状态筛选", ["全部"] + list(LABELS.values()))
    for run in runs:
        if (name != "全部" and run["name"] != name) or (state != "全部" and LABELS[run["status"]] != state):
            continue
        with st.container(border=True):
            a, b, c = st.columns([4, 1, 1])
            a.markdown(f"**{run['name']}** · {LABELS[run['status']]} · {timestamp(run['created'])}")
            a.caption(run["message"])
            if b.button("查看结果", key="view_" + run["id"]):
                navigate("回测结果", run_id=run["id"])
            if c.button("复制配置", key="repeat_" + run["id"]):
                source = (store.directory(run["id"]) / "strategy.py").read_text()
                sid = store.save_strategy(run["name"] + " 复现", source)
                st.session_state.copied_config = json.loads(run["config"])
                navigate("策略编辑", strategy_id=sid)
    if not runs:
        st.info("还没有回测记录。")


def data_page():
    st.title("数据管理")
    st.caption("下载可以持续进行。刷新后，新完成的文件将用于后续回测，已运行的快照保持不变。")
    st.text_input("Parquet 数据目录", value=os.environ.get("QLIB_WORKBENCH_DATA", str(ROOT / "db/data/market_daily")), key="data_path")
    refresh = st.button("重新扫描数据", type="primary")
    cat = catalog(refresh=refresh)
    a, b, c = st.columns(3)
    a.metric("有效日线文件", len(cat["files"]))
    b.metric("股票数量", len(cat["symbols"]))
    c.metric("暂不可读文件", len(cat["errors"]))
    if cat["segments"]:
        st.dataframe(pd.DataFrame([{"开始日期": s[0], "结束日期": s[-1], "数据日数": len(s)} for s in cat["segments"]]),
                     hide_index=True, width="stretch")
        st.caption("相邻文件间隔超过20个自然日时分段；文件日期不是经过完整校验的交易所日历。个股缺行情不自动填零。")
        with st.expander("股票代码"):
            st.dataframe(pd.DataFrame({"股票代码": cat["symbols"]}), hide_index=True)
    else:
        st.warning("该目录尚未发现完整的行情文件。")
    if cat["errors"]:
        st.dataframe(pd.DataFrame(cat["errors"]), hide_index=True, width="stretch")


if "pending_page" in st.session_state:
    st.session_state.page = st.session_state.pop("pending_page")
with st.sidebar:
    st.markdown("## Qlib 策略工作台")
    st.caption("QUANT RESEARCH · 本地研究")
    page = st.radio("工作区", ["我的策略", "策略编辑", "回测结果", "回测历史", "数据管理"], key="page")
    st.divider()
    st.caption("A 股日线 · Qlib 回测引擎")
    st.caption("个人本机模式，策略以当前用户权限执行。")
    st.caption("时间显示：北京时间")

{"我的策略": strategies_page, "策略编辑": editor_page, "回测结果": results_page,
 "回测历史": history_page, "数据管理": data_page}[page]()
