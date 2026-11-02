"""Launch: streamlit run apps/quant_workbench/app.py"""
from __future__ import annotations

import json
from html import escape
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

from apps.quant_workbench.data import scan, freeze_manifest, local_a_universe, symbol
from apps.quant_workbench.store import Store, LABELS, ACTIVE, validate
from apps.quant_workbench.templates import TEMPLATES, TEMPLATE_LESSONS, API_HELP, ASSUMPTIONS
from apps.quant_workbench.presentation import CSS, PALETTE, CHART_CONFIG, page_header, style_chart, performance_figure

st.set_page_config(page_title="Qlib 策略工作台", page_icon="📈", layout="wide")
st.markdown(CSS, unsafe_allow_html=True)
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
    "hold_nav": "买入持有净值（扣费）", "hold_drawdown": "买入持有回撤",
    "hold_daily_return": "买入持有日收益率（扣费）",
    "hold_return_difference": "策略与买入持有累计收益差",
}


def navigate(page, strategy_id=None, run_id=None):
    st.session_state.pending_page = page
    if strategy_id:
        st.session_state.selected_strategy = strategy_id
    if run_id:
        st.session_state.selected_run = run_id
    st.rerun()


def catalog(refresh=False):
    path = st.session_state.get("data_path", os.environ.get("QLIB_WORKBENCH_DATA", str(ROOT / "data/market_daily")))
    if refresh or "catalog" not in st.session_state or st.session_state.catalog["directory"] != str(Path(path).expanduser().resolve()):
        with st.spinner("正在扫描已完成的行情文件…"):
            st.session_state.catalog = scan(path)
    return st.session_state.catalog


def timestamp(value):
    return pd.Timestamp(value, unit="s", tz="UTC").tz_convert("Asia/Shanghai").strftime("%m-%d %H:%M:%S")


def strategies_page():
    page_header("我的策略", "从一个想法开始，编写策略，用历史数据验证。", "01")
    with st.expander("＋ 新建策略", expanded=not store.strategies()):
        template = st.selectbox("策略模板", list(TEMPLATES))
        st.info(TEMPLATE_LESSONS[template])
        st.caption("教学示例均只做多，使用昨日及更早数据，今日开盘撮合；参数说明在代码顶部。")
        with st.form("create_strategy"):
            name = st.text_input("策略名称", value="我的新策略")
            if st.form_submit_button("创建并编辑", type="primary"):
                try:
                    strategy_id = store.save_strategy(name, TEMPLATES[template])
                    if template == "A股中期动量基线":
                        st.session_state.copied_config = {"capital": 10000., "history_days": 300, "universe_mode": "local_a"}
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
        page_header("策略编辑", "将研究想法写成可验证的交易规则。", "02")
        st.info("请先在“我的策略”中新建策略。")
        return
    current = next((s for s in strategies if s["id"] == st.session_state.get("selected_strategy")), strategies[0])
    st.session_state.selected_strategy = current["id"]
    page_header("策略编辑", "编辑源码 → 检查语法 → 设置参数 → 运行回测", "02")
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
        if copied is not None or st.session_state.get("config_strategy_id") != sid:
            if copied is None:
                previous = next((run for run in store.runs() if run["strategy_id"] == sid), None)
                copied = json.loads(previous["config"]) if previous else {}
            st.session_state.backtest_defaults = copied
            st.session_state.config_strategy_id = sid
            for key in [k for k in st.session_state if k.startswith("cfg_")]:
                del st.session_state[key]
        defaults = st.session_state.get("backtest_defaults", {})
        latest = cat["segments"][-1]
        start_default = defaults.get("start", latest[max(1, len(latest) - 60)] if len(latest) > 1 else latest[0])
        end_default = defaults.get("end", latest[-1])
        start = st.date_input("开始日期", pd.Timestamp(start_default).date(), key="cfg_start")
        end = st.date_input("结束日期", pd.Timestamp(end_default).date(), key="cfg_end")
        capital = st.number_input("初始资金（元）", min_value=100.0, value=float(defaults.get("capital", 1000000)), step=10000.0, key="cfg_capital")
        mode_labels = {"local_a": "本地 A 股（起点前行情日）", "custom": "自定义股票池"}
        mode = defaults.get("universe_mode", "custom" if defaults.get("symbols") else "local_a")
        pool_mode = st.selectbox("股票池来源", list(mode_labels), index=list(mode_labels).index(mode),
                                 format_func=mode_labels.get, key="cfg_universe_mode")
        pool, universe = None, ""
        if pool_mode == "local_a":
            try:
                pool = local_a_universe(cat, start)
                st.info(f"股票池 {len(pool['symbols']):,} 只 · 来源日期 {pool['asof']}")
                st.caption("固定使用起点前最近完整行情日出现的 A 股；期间新股不自动加入，持仓数量由策略决定。"
                           "这不是交易所完整历史成分库，未自动排除 ST 或检查账户权限。")
                with st.expander("查看与导出股票池"):
                    st.dataframe(pd.DataFrame({"股票代码": pool["symbols"]}), hide_index=True, height=180)
                    st.download_button("下载股票池代码", "\n".join(pool["symbols"]).encode(), "universe.txt", mime="text/plain")
            except (ValueError, OSError) as exc:
                st.error(str(exc))
        else:
            universe = st.text_area("股票池", value=", ".join(defaults.get("symbols", [])),
                                    help="用逗号、空格或换行分隔。支持 SZ000001、000001.SZ、000001.XSHE。", key="cfg_symbols")
        benchmark = st.text_input("基准代码（可留空）", value=defaults.get("benchmark") or "", key="cfg_benchmark")
        st.caption("自动对比股票池的买入持有收益；这里可另外指定市场基准。")
        with st.expander("交易费用与策略参数"):
            buy = st.number_input("买入费率", min_value=0.0, max_value=0.99, value=float(defaults.get("open_cost", 0.0005)), format="%.5f", key="cfg_buy")
            sell = st.number_input("卖出费率", min_value=0.0, max_value=0.99, value=float(defaults.get("close_cost", 0.0015)), format="%.5f", key="cfg_sell")
            minimum = st.number_input("单笔最低费用（元）", min_value=0.0, value=float(defaults.get("min_cost", 5)), key="cfg_min")
            params = st.text_area("策略参数（JSON）", json.dumps(defaults.get("params", {}), ensure_ascii=False), key="cfg_params")
            history_days = st.number_input("预读历史交易日（0为全部）", min_value=0,
                                           value=int(defaults.get("history_days") or 0), step=50, key="cfg_history_days",
                                           help="回测开始前额外加载的市场交易日。需覆盖策略最长窗口；小资金基线默认需252日，建议300日。")
        st.caption(ASSUMPTIONS)
        active = next((r for r in store.runs() if r["status"] in ACTIVE), None)
        if st.button("▶ 运行回测", type="primary", width="stretch", disabled=active is not None):
            try:
                codes = pool["symbols"] if pool_mode == "local_a" and pool else [symbol(s) for s in universe.replace(",", " ").replace("，", " ").split()]
                config = validate(source, {"start": str(start), "end": str(end), "capital": capital,
                    "symbols": codes, "benchmark": benchmark.strip(), "open_cost": buy,
                    "close_cost": sell, "min_cost": minimum, "params": json.loads(params),
                    "universe_mode": pool_mode, "history_days": history_days})
                with st.spinner("固定本次数据清单…"):
                    cat = catalog(refresh=True)
                    if pool_mode == "local_a":
                        pool = local_a_universe(cat, start)
                        codes = config["symbols"] = pool["symbols"]
                        config.update(universe_asof=pool["asof"], universe_sha256=pool["sha256"])
                    manifest = freeze_manifest(cat, config["start"], config["end"],
                                               codes + ([config["benchmark"]] if config["benchmark"] else []),
                                               history_days=history_days or None)
                    if pool_mode == "local_a" and not any(entry["date"] == pool["asof"] and entry["sha256"] == pool["sha256"]
                                                         for entry in manifest["files"]):
                        raise ValueError("股票池来源文件已变化或不在本次快照中，请刷新后重试")
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
    for i, (field, label) in enumerate(fields.items()):
        if field in frame:
            figure.add_trace(go.Scatter(x=frame.datetime, y=frame[field], name=label, mode="lines",
                                       line=dict(color=PALETTE[i % len(PALETTE)], width=2)))
    style_chart(figure)
    st.caption(title)
    if percent:
        figure.update_yaxes(tickformat=".1%")
    st.plotly_chart(figure, width="stretch", config=CHART_CONFIG)


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
    cols[0].markdown(f'<div class="wb-run"><strong>{escape(run["name"])}</strong>'
                     f'<span class="wb-status {escape(run["status"])}">{LABELS[run["status"]]}</span>'
                     f'<time>{timestamp(run["created"])}</time></div>', unsafe_allow_html=True)
    if run["status"] in ACTIVE:
        st.progress(min(max(run["progress"], 0), 1), text=run["message"] or "准备中…")
        if cols[1].button("停止任务", disabled=run["cancel_at"] is not None):
            store.cancel(run_id)
            st.rerun(scope="fragment")
    elif run["status"] != "success":
        st.warning(run["message"])
    config = json.loads(run["config"])
    st.caption(f'{config["start"]} → {config["end"]}　·　初始资金 {config["capital"]:,.0f} 元'
               f'　·　{len(config["symbols"])} 个标的　·　日线回测')
    if run["status"] == "success":
        stats = json.loads((folder / "metrics.json").read_text())
        if timing := stats.get("timings"):
            stages = [("snapshot_seconds", "数据快照"), ("prepare_seconds", "行情加载"),
                      ("strategy_seconds", "策略回测"), ("comparison_seconds", "买入持有对照")]
            detail = " · ".join(f"{label} {timing[key]:.1f}s" for key, label in stages if key in timing)
            total = timing.get("worker_seconds", timing["total_seconds"])
            cache = " · 对照已复用缓存" if timing.get("comparison_cache_hit") else ""
            st.caption(f"耗时 {total:.1f}s · {detail}{cache}")
        with st.container(key="strategy_metrics"):
            columns = st.columns(5)
            for col, (key, label) in zip(columns, [("total_return", "累计收益"), ("annual_return", "年化收益"),
                                                    ("max_drawdown", "最大回撤"), ("sharpe", "夏普比率"), ("volatility", "年化波动")]):
                value = stats[key]
                col.metric(label, "不可计算" if value is None else (f"{value:.2f}" if key == "sharpe" else f"{value:.2%}"))
        if "benchmark_return" in stats:
            st.caption(f"基准收益 {stats['benchmark_return']:.2%} · 超额收益 {stats['excess_return']:.2%}（策略减基准）")
        hold_label = "标的买入持有" if len(config["symbols"]) == 1 else "股票池初始等权买入持有"
        if "hold_metrics" in stats:
            hold_stats = stats["hold_metrics"]
            with st.container(key="comparison_metrics"):
                comparison = st.columns(3)
                comparison[0].metric(hold_label + "收益", f"{hold_stats['total_return']:.2%}")
                comparison[1].metric("策略 − 买入持有", f"{stats['hold_return_difference'] * 100:+.2f} 个百分点")
                comparison[2].metric("买入持有最大回撤", f"{hold_stats['max_drawdown']:.2%}")
            filled_orders = stats.get("hold_filled_orders")
            if filled_orders is None and (folder / "hold_trades.parquet").exists():
                hold_trades = pd.read_parquet(folder / "hold_trades.parquet")
                filled_orders = int(hold_trades.amount.gt(0).sum()) if "amount" in hold_trades else 0
            if filled_orders == 0:
                st.warning("买入持有对照没有成交：股票池过大、每只分配资金不足一手等情况可能导致全程持有现金。"
                           "此时0%收益不是市场收益，不能据此判断选股是否有效。")
        else:
            st.info("这条历史记录尚未保存买入持有对照；在回测历史中复制配置并重新运行即可生成。")
        report = pd.read_parquet(folder / "report.parquet").drop(columns=["bench"], errors="ignore")
        # Plot the initial cash point as well, so first-day losses are visible.
        initial = {"datetime": pd.Timestamp(config["start"]) - pd.Timedelta(days=1), "nav": 1, "drawdown": 0}
        if "benchmark_nav" in report:
            initial["benchmark_nav"] = 1
        if "hold_nav" in report:
            initial.update(hold_nav=1, hold_drawdown=0, hold_return_difference=0)
        plotted = pd.concat([pd.DataFrame([initial]), report], ignore_index=True)
        with st.container(border=True):
            control, scale = st.columns([3, 1])
            detail_options = ["回撤对比", "每日收益"]
            if "hold_return_difference" in report:
                detail_options.insert(1, "累计收益差")
            detail = control.radio("下方图表", detail_options, horizontal=True, key="detail_" + run_id,
                                   label_visibility="collapsed")
            net_value = scale.toggle("显示净值", key="net_value_" + run_id,
                                     help="默认显示累计收益率；打开后上方曲线显示净值。")
            st.plotly_chart(performance_figure(plotted, hold_label + "（扣费）", detail, net_value),
                            width="stretch", config=CHART_CONFIG)
            st.caption("策略与买入持有均扣费 · 点击图例可隐藏 / 显示同一账户的曲线 · 拖动缩放，双击复位")
        has_plan = (folder / "rebalance_plan.parquet").exists()
        tabs = st.tabs(["每日收益", "每日持仓", "成交明细", "自定义指标", "运行日志", "源码与参数"]
                       + (["调仓计划"] if has_plan else []))
        with tabs[0]:
            st.dataframe(report, width="stretch", height=280, hide_index=True, column_config={**TABLE_LABELS, "cost": "费用比例"})
        for tab, filename, label in [(tabs[1], "holdings", "持仓"), (tabs[2], "trades", "成交"), (tabs[3], "records", "自定义指标")]:
            with tab:
                frame = pd.read_parquet(folder / f"{filename}.parquet")
                if frame.empty:
                    st.info(f"本次回测没有{label}记录。")
                else:
                    if filename == "holdings":
                        day = st.selectbox("查看日期", sorted(frame.datetime.unique(), reverse=True), format_func=lambda d: str(pd.Timestamp(d).date()))
                        st.dataframe(frame[frame.datetime == day], hide_index=True, width="stretch", height=280, column_config=TABLE_LABELS)
                    else:
                        st.dataframe(frame, hide_index=True, width="stretch", height=280, column_config=TABLE_LABELS)
                    if filename == "records":
                        fields = [c for c in frame if c != "datetime"]
                        defaults = [c for c in fields if not any(word in c for word in ("资产", "资金", "信号数"))]
                        selected = st.multiselect("显示指标", fields, default=(defaults or fields)[:6],
                                                  key="record_fields_" + run_id,
                                                  help="选择量纲相近的指标一起比较，避免账户金额掩盖价格变化。")
                        if selected:
                            chart(frame, {c: c for c in selected}, "自定义指标走势")
                    st.download_button(f"导出{label} CSV", frame.to_csv(index=False).encode("utf-8-sig"), f"{filename}.csv", mime="text/csv")
        log_container, source_container = tabs[4], tabs[5]
        if has_plan:
            with tabs[6]:
                plan = pd.read_parquet(folder / "rebalance_plan.parquet")
                st.caption("这是历史调仓日按前一日价格生成的计划，股数为估算；不代表实际成交或当前交易建议。"
                           "执行前需按真实持仓、最新价格和费用重新核对；成交结果见“成交明细”。")
                if plan.empty:
                    st.info("本次没有调仓计划。")
                else:
                    day = st.selectbox("计划日期", sorted(plan.datetime.unique(), reverse=True),
                                       format_func=lambda d: str(pd.Timestamp(d).date()), key="plan_day_" + run_id)
                    st.dataframe(plan[plan.datetime == day], hide_index=True, width="stretch", column_config={
                        "datetime": "调仓日", "signal_date": "行情截止日", "instrument": "股票",
                        "rank": "信号排名", "action": "操作", "reference_price": "参考昨收价",
                        "current_shares": "当前股数", "trade_shares": "变动股数（负数为卖）",
                        "target_shares": "目标股数", "target_value": "目标市值", "target_weight": "目标仓位",
                        "estimated_cost": "预估费用", "industry": "行业", "reason": "原因"})
                    st.download_button("导出调仓计划 CSV", plan.to_csv(index=False).encode("utf-8-sig"),
                                       "rebalance_plan.csv", mime="text/csv")
        if "hold_metrics" in stats:
            with st.expander("买入持有对照明细"):
                for filename, label in [("hold_report", "每日账户"), ("hold_trades", "成交"), ("hold_holdings", "持仓")]:
                    frame = pd.read_parquet(folder / f"{filename}.parquet")
                    st.caption(label)
                    st.dataframe(frame, hide_index=True, width="stretch", column_config=TABLE_LABELS)
                    st.download_button(f"导出对照{label} CSV", frame.to_csv(index=False).encode("utf-8-sig"),
                                       f"{filename}.csv", mime="text/csv")
                st.code(stats["hold_source"], language="python")
        exports = st.columns(2)
        exports[0].download_button("导出每日收益 CSV", report.to_csv(index=False).encode("utf-8-sig"), "report.csv", mime="text/csv", width="stretch")
        exports[1].download_button("下载完整回测 ZIP", store.archive(run_id), f"backtest-{run_id[:8]}.zip", mime="application/zip", width="stretch")
    else:
        log_container = st.container()
        source_container = st.expander("本次源码与参数")
    with st.expander("回测假设与对照口径"):
        st.caption(ASSUMPTIONS)
        st.caption("买入持有对照使用相同初始资金、日期、行情快照和手续费。首日向每只标的提交 1/N 的目标仓位，"
                   "之后不调仓；按昨日价格估算数量、当日开盘撮合。整数手、资金不足、停牌或缺行情可能使实际仓位偏离等权，"
                   "未成交不补单，余额留现金；期末按收盘估值，不强制卖出。")
        st.caption("收益差是两者累计收益率相减，不是风险调整后的 Alpha。策略仓位较低时，应同时比较回撤；"
                   "一次回测跑赢不能证明策略持续有效。自选基准为复权价格收益，未扣交易费用。")
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
    page_header("回测结果", "让收益与风险同屏，观察策略相对买入持有的表现。", "03")
    runs = store.runs()
    if not runs:
        st.info("运行一个策略后，收益曲线和交易记录会显示在这里。")
        return
    selected = st.session_state.get("selected_run", runs[0]["id"])
    refresh = "2s" if store.get_run(selected)["status"] in ACTIVE else None
    st.fragment(run_every=refresh)(run_view)(selected)


def history_page():
    page_header("回测历史", "保留每次研究的代码与参数，比较结果，复现想法。", "04")
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
    page_header("数据管理", "掌握行情覆盖范围，让新增数据进入下一次研究。", "05")
    st.caption("下载可以持续进行。刷新后，新完成的文件将用于后续回测，已运行的快照保持不变。")
    st.text_input("Parquet 数据目录", value=os.environ.get("QLIB_WORKBENCH_DATA", str(ROOT / "data/market_daily")), key="data_path")
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
    st.markdown('<div class="wb-brand"><div class="wb-logo">Q</div><div><strong>Qlib 策略工作台</strong>'
                '<small>QUANT RESEARCH</small></div></div>', unsafe_allow_html=True)
    page = st.radio("工作区", ["我的策略", "策略编辑", "回测结果", "回测历史", "数据管理"], key="page")
    st.divider()
    st.caption("A 股日线 · Qlib 回测引擎")
    st.caption("个人本机模式，策略以当前用户权限执行。")
    st.caption("时间显示：北京时间")

{"我的策略": strategies_page, "策略编辑": editor_page, "回测结果": results_page,
 "回测历史": history_page, "数据管理": data_page}[page]()
