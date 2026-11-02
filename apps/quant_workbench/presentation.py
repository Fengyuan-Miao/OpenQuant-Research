"""Shared visual styles and linked performance charts for the workbench."""
from html import escape

import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

COLORS = {"strategy": "#2563eb", "hold": "#e58b18", "benchmark": "#8b5cf6", "difference": "#0d9488"}
PALETTE = [COLORS["strategy"], COLORS["hold"], COLORS["difference"], COLORS["benchmark"], "#e6577a", "#64748b"]
CHART_CONFIG = {"displaylogo": False, "responsive": True,
                "modeBarButtonsToRemove": ["lasso2d", "select2d", "autoScale2d"]}

CSS = """<style>
:root {--wb-blue:#2563eb; --wb-ink:#172b4d; --wb-muted:#64748b;}
.block-container {padding:1.3rem 2rem 2rem; max-width:1600px;}
[data-testid="stHeader"] {background:transparent;}
[data-testid="stMainBlockContainer"] > [data-testid="stVerticalBlock"] {gap:.8rem;}
[data-testid="stVerticalBlock"] {gap:.65rem;}
h1 {font-size:1.7rem !important; letter-spacing:-.04em;}
h3 {font-size:1.05rem !important;}
[data-testid="stCaptionContainer"] {color:var(--wb-muted);}
[data-testid="stMetric"] {background:linear-gradient(135deg,#fff,#f8fbff); border:1px solid #e3eaf5;
 border-radius:12px; padding:12px 16px; box-shadow:0 3px 12px #172b4d04; border-top:3px solid #2563eb;}
[data-testid="stMetricLabel"] {color:#64748b; font-size:.8rem;}
[data-testid="stMetricValue"] {font-size:1.7rem; font-weight:650; letter-spacing:-.035em;}
.st-key-comparison_metrics [data-testid="stMetric"] {border-top:2px solid #e58b18; padding:8px 14px; background:#fffcf7;}
.st-key-comparison_metrics [data-testid="stMetricValue"] {font-size:1.2rem;}
.st-key-comparison_metrics [data-testid="stColumn"]:nth-child(2) [data-testid="stMetric"] {border-top-color:#0d9488; background:#f3fbf8;}
[data-testid="stSidebar"] {background:linear-gradient(180deg,#edf3ff,#f7f9fd 70%); border-right:1px solid #dfe7f3;}
[data-testid="stSidebarUserContent"] {padding:1.5rem 1rem;}
[data-testid="stSidebar"] [role="radiogroup"] {gap:6px;}
[data-testid="stSidebar"] [role="radiogroup"] label {padding:9px 12px; border-radius:9px; transition:background .15s; width:100%;}
[data-testid="stSidebar"] [role="radiogroup"] label:hover {background:#e0eaff;}
[data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked) {background:#dce8ff; box-shadow:inset 3px 0 #2563eb;}
[data-testid="stSidebar"] hr {margin:1rem 0;}
[data-testid="stButton"] button, [data-testid="stDownloadButton"] button {border-radius:8px; min-height:2.2rem;}
[data-testid="stBaseButton-primary"] {box-shadow:0 3px 10px #2563eb22;}
[data-testid="stVerticalBlockBorderWrapper"] > div {border-radius:12px;}
[data-testid="stExpander"] {border-color:#e3eaf5; border-radius:10px; background:#ffffff99;}
[data-baseweb="tab-list"] {gap:18px; border-bottom:1px solid #e2e8f0;}
[data-baseweb="tab"] {padding:8px 0; height:40px;}
[data-testid="stDataFrame"] {border-radius:8px; overflow:hidden;}
.wb-hero {display:flex; align-items:center; justify-content:space-between; gap:16px; padding:17px 22px;
 border:1px solid #dce6f7; border-radius:14px; background:radial-gradient(ellipse at 95% 10%,#dce9ff,transparent 55%),linear-gradient(105deg,#fff,#f2f7ff); margin-bottom:2px;}
.wb-hero h1 {margin:0; padding:0; color:#172b4d; line-height:1.35;}
.wb-hero p {margin:5px 0 0; font-size:13px; color:#64748b;}
.wb-kicker {font-size:10px; font-weight:700; letter-spacing:1.6px; color:#2563eb; margin-bottom:5px;}
.wb-hero-mark {font-size:28px; font-weight:700; color:#2563eb; background:#ffffffb3; border:1px solid #d6e3ff; border-radius:12px; padding:8px 15px;}
.wb-brand {display:flex; gap:10px; align-items:center; margin:6px 0 22px;}
.wb-logo {background:#2563eb; color:white; width:36px; height:36px; display:grid; place-items:center; border-radius:10px; font-weight:800; font-size:22px; box-shadow:0 4px 12px #2563eb22;}
.wb-brand strong {font-size:17px; color:#172b4d;}
.wb-brand small {display:block; font-size:10px; color:#64748b; letter-spacing:1.2px; margin-top:2px;}
.wb-run {display:flex; align-items:center; flex-wrap:wrap; gap:10px; font-size:14px;}
.wb-run strong {overflow-wrap:anywhere;}
.wb-run time {font-size:12px; color:#64748b;}
.wb-status {font-size:11px; padding:3px 9px; border-radius:20px; background:#edf2fa; color:#475569; white-space:nowrap;}
.wb-status.success {background:#dcfce7; color:#166534;}
.wb-status.running,.wb-status.preparing {background:#dbeafe; color:#1d4ed8;}
.wb-status.failed {background:#fee2e2; color:#b91c1c;}
@media (max-width:800px) {
 .block-container {padding:1rem .8rem;}
 .wb-hero {padding:14px;}
 .wb-hero-mark {display:none;}
 [data-testid="stMetricValue"] {font-size:1.35rem;}
 [data-testid="stMetric"] {padding:10px;}
 .st-key-strategy_metrics [data-testid="stHorizontalBlock"], .st-key-comparison_metrics [data-testid="stHorizontalBlock"] {
  display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px;}
 .st-key-strategy_metrics [data-testid="stColumn"], .st-key-comparison_metrics [data-testid="stColumn"] {
  width:100% !important; min-width:0 !important;}
 .st-key-strategy_metrics [data-testid="stColumn"]:last-child, .st-key-comparison_metrics [data-testid="stColumn"]:last-child {grid-column:1/-1;}
}
</style>"""


def page_header(title, subtitle, number):
    st.markdown(f'<div class="wb-hero"><div><div class="wb-kicker">QLIB / QUANT RESEARCH</div>'
                f'<h1>{escape(title)}</h1><p>{escape(subtitle)}</p></div>'
                f'<div class="wb-hero-mark" aria-hidden="true">{escape(number)}</div></div>', unsafe_allow_html=True)


def style_chart(figure, height=300):
    figure.update_layout(template="plotly_white", height=height,
        margin=dict(l=14, r=18, t=42, b=18), font=dict(family="sans-serif", size=12, color="#64748b"),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="#ffffff", hovermode="x unified",
        legend=dict(orientation="h", y=1.12, x=0, font=dict(size=12), groupclick="togglegroup"),
        hoverlabel=dict(bgcolor="white", font_size=12), colorway=PALETTE)
    figure.update_xaxes(showgrid=False, zeroline=False, showline=True, linecolor="#e2e8f0")
    figure.update_yaxes(gridcolor="#eef2f7", zerolinecolor="#cbd5e1", automargin=True)
    return figure


def performance_figure(frame, hold_label, detail="回撤对比", net_value=False):
    """Shared date axis; independent y scales for performance and risk."""
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=.10, row_heights=[.70, .30])
    series = [("nav", "策略", "strategy"), ("hold_nav", hold_label, "hold"),
              ("benchmark_nav", "自选基准（未扣费）", "benchmark")]
    for field, label, group in series:
        if field not in frame:
            continue
        color = COLORS[group]
        fig.add_trace(go.Scatter(x=frame.datetime, y=frame[field] if net_value else frame[field] - 1,
            name=label, legendgroup=group, mode="lines",
            line=dict(color=color, width=2.4 if group == "strategy" else 1.8,
                      dash="dot" if group == "benchmark" else "solid"),
            hovertemplate="%{y:.4f}<extra></extra>" if net_value else "%{y:.2%}<extra></extra>"), row=1, col=1)
        if detail == "回撤对比":
            column = {"strategy": "drawdown", "hold": "hold_drawdown"}.get(group)
            values = (frame[column] if column else frame[field] / frame[field].cummax().clip(lower=1) - 1)
        elif detail == "每日收益":
            column = {"strategy": "daily_return", "hold": "hold_daily_return", "benchmark": "benchmark_daily_return"}[group]
            if column not in frame:
                continue
            values = frame[column]
        else:
            continue
        fig.add_trace(go.Scatter(x=frame.datetime, y=values, name=label, legendgroup=group,
            showlegend=False, mode="lines", line=dict(color=color, width=1.5, dash="dot" if group == "benchmark" else "solid"),
            fill="tozeroy" if detail == "回撤对比" and group == "strategy" else None,
            fillcolor="rgba(37,99,235,.08)", hovertemplate="%{y:.2%}<extra></extra>"), row=2, col=1)
    if detail == "累计收益差" and "hold_return_difference" in frame:
        fig.add_trace(go.Scatter(x=frame.datetime, y=frame.hold_return_difference, name="策略 − 买入持有",
            mode="lines", line=dict(color=COLORS["difference"], width=2), showlegend=True,
            hovertemplate="%{y:.2%}<extra></extra>"), row=2, col=1)
    style_chart(fig, height=430)
    fig.update_layout(legend=dict(y=1.14), margin=dict(l=10, r=18, t=45, b=10))
    fig.update_yaxes(title_text="净值" if net_value else "累计收益", tickformat=".2f" if net_value else ".0%", row=1, col=1)
    fig.update_yaxes(title_text={"回撤对比": "回撤", "每日收益": "日收益", "累计收益差": "收益率差"}[detail], tickformat=".0%", row=2, col=1)
    fig.add_hline(y=0, line_width=1, line_color="#cbd5e1", row=2, col=1)
    return fig
