"""TradingAgents 本地 Web UI（Streamlit）。

启动（在项目根目录）：
    .venv/Scripts/python.exe -m streamlit run my_tools/webui.py --server.headless false

功能：
- 新建分析：填 ticker / 日期 / 分析师阵容，实时看阶段进度和智能体输出流
- 跑完自动保存报告（含 HTML 版），可直接在浏览器打开
- 历史报告：浏览 ~/.tradingagents 下所有已保存报告

注意：必须在导入 tradingagents 之前清理会话注入的内部代理（127.0.0.1:55xxx），
否则会覆盖 .env 里的 10808 代理导致 Yahoo 被限流。
"""

import os
import sys
import webbrowser
from pathlib import Path

# --- 0) 代理清理：必须先于任何 tradingagents 导入 ---
_INTERNAL_MARK = "127.0.0.1:55"  # WorkBuddy 会话内部代理端口段
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
           "ALL_PROXY", "all_proxy"):
    _v = os.environ.get(_k)
    if _v and _INTERNAL_MARK in _v:
        del os.environ[_k]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st  # noqa: E402
import time  # noqa: E402

st.set_page_config(page_title="TradingAgents 控制台", page_icon="📈", layout="wide")

REPORTS_ROOT = Path.home() / ".tradingagents" / "logs" / "reports"
ANALYSTS = {
    "market": "📈 市场技术面",
    "social": "💬 市场情绪",
    "news": "📰 新闻资讯",
    "fundamentals": "🏢 基本面",
}
STAGE_KEYS = [
    ("final_trade_decision", "⚖️ 组合经理最终决策"),
    ("risk_debate_state", "🛡️ 风险团队辩论"),
    ("trader_investment_plan", "🎯 交易员投资计划"),
    ("investment_debate_state", "🔬 研究团队多空辩论"),
]


def _stage_of(chunk: dict) -> str | None:
    if not isinstance(chunk, dict):
        return None
    for key, label in STAGE_KEYS:
        if chunk.get(key):
            return label
    if any(k.endswith("_report") and chunk.get(k) for k in chunk):
        return "🧑‍💼 分析师研究中"
    return None


def _msg_text(message) -> str:
    """从 LangChain 消息提取可展示文本。"""
    sender = getattr(message, "name", None) or type(message).__name__.replace("Message", "")
    content = getattr(message, "content", "") or ""
    if isinstance(content, list):  # 多模态内容块
        content = " ".join(
            b.get("text", "") for b in content if isinstance(b, dict)
        )
    content = str(content).strip()
    if not content:
        return ""
    return f"[{sender}] {content[:400]}"


@st.cache_resource(show_spinner="初始化智能体图…")
def build_graph(selected_analysts: tuple, deep_llm: str, quick_llm: str,
                data_vendors: dict | None = None,
                global_news_proxies: tuple = ()):
    """构造 TradingAgentsGraph；参数变化时重建。

    ``data_vendors`` 为 None 时使用项目默认链（openbb 优先）；传入覆盖字典可
    切回纯 yfinance 等旧行为。``global_news_proxies`` 为空时沿用默认宏观
    新闻代理篮子（SPY/QQQ/DIA/TLT/GLD/USO/UUP）。
    """
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    config = dict(DEFAULT_CONFIG)
    config["deep_think_llm"] = deep_llm
    config["quick_think_llm"] = quick_llm
    if data_vendors:
        config["data_vendors"] = data_vendors
    if global_news_proxies:
        config["global_news_proxy_symbols"] = list(global_news_proxies)
    return TradingAgentsGraph(selected_analysts=selected_analysts, config=config, debug=False)


def run_analysis(graph, ticker: str, date: str):
    """流式跑完整分析；返回 (final_state, 日志行列表)。"""
    init_state = graph.create_run_state(ticker, date, "stock", None)
    args = graph.propagator.get_graph_args()
    trace: list[dict] = []
    log: list[str] = []
    stage = "🧑‍💼 分析师研究中"
    for messages, chunk in graph.stream_run(init_state, **args):
        for m in messages or []:
            line = _msg_text(m)
            if line:
                log.append(line)
        new_stage = _stage_of(chunk)
        if new_stage:
            stage = new_stage
        if chunk:
            trace.append(chunk)
    final_state: dict = {}
    for chunk in trace:
        final_state.update(chunk)
    return final_state, log, stage


# ============ 侧边栏 ============
st.sidebar.title("📈 TradingAgents 控制台")
page = st.sidebar.radio("页面", ["🚀 新建分析", "📚 历史报告"])

VENDOR_CHAINS = {
    "OpenBB 本地平台（默认）": {
        "core_stock_apis": "openbb,yfinance",
        "technical_indicators": "openbb,yfinance",
        "fundamental_data": "openbb,sec_edgar,yfinance",
        "news_data": "openbb,yfinance",
        "macro_data": "openbb,fred",
    },
    "仅 Yahoo Finance（旧行为）": {
        "core_stock_apis": "yfinance",
        "technical_indicators": "yfinance",
        "fundamental_data": "sec_edgar,yfinance",
        "news_data": "yfinance",
        "macro_data": "fred",
    },
    "alpha_vantage 优先": {
        "core_stock_apis": "alpha_vantage,yfinance",
        "technical_indicators": "alpha_vantage,yfinance",
        "fundamental_data": "alpha_vantage,sec_edgar,yfinance",
        "news_data": "alpha_vantage,yfinance",
        "macro_data": "fred",
    },
}

st.sidebar.caption(
    "本地 Web UI · LLM：MiniMax（配置见项目 .env）\n\n"
    "数据源：优先走本地 OpenBB 平台（127.0.0.1:6900），失败自动回退下一家"
)

# ============ 新建分析 ============
if page == "🚀 新建分析":
    st.header("🚀 新建分析")
    c1, c2, c3 = st.columns([2, 1.5, 2])
    with c1:
        ticker = st.text_input("股票代码", value="NVDA", help="美股 NVDA · A股 600519.SS · 港股 0700.HK")
    with c2:
        date = st.date_input("分析日期").isoformat()
    with c3:
        chosen = st.multiselect(
            "分析师阵容", list(ANALYSTS), default=list(ANALYSTS), format_func=ANALYSTS.get
        )
    c4, c5, _ = st.columns([2, 2, 2])
    with c4:
        deep_llm = st.text_input("深度思考模型", value=os.getenv("TRADINGAGENTS_DEEP_THINK_LLM", "MiniMax-M3"))
    with c5:
        quick_llm = st.text_input("快速思考模型", value=os.getenv("TRADINGAGENTS_QUICK_THINK_LLM", "MiniMax-M2.7-highspeed"))

    vendor_choice = st.selectbox(
        "数据源（行情/指标/基本面/新闻）",
        list(VENDOR_CHAINS),
        index=0,
        help="OpenBB：本地 OpenBB 平台优先，失败回退 yfinance；"
             "选择「仅 Yahoo」可完全恢复旧行为",
    )

    from tradingagents.default_config import DEFAULT_CONFIG as _DEFAULT_CFG
    _default_proxies = ",".join(_DEFAULT_CFG.get("global_news_proxy_symbols") or ())
    proxies_raw = st.text_input(
        "宏观新闻代理标的（OpenBB 全局新闻）",
        value=_default_proxies,
        disabled=VENDOR_CHAINS[vendor_choice]["news_data"].startswith("yfinance"),
        help="本地 OpenBB 平台没有独立的「全球新闻」路由，get_global_news 改用这些"
             "市场级标的的新闻页拼出宏观面（指数 + 利率/黄金/原油/美元）。"
             "留空则用默认篮子；选「仅 Yahoo」时该功能不生效，仍走 yfinance 搜索。",
    )

    save_report = st.checkbox("跑完保存报告（含 HTML 版）", value=True)
    auto_open = st.checkbox("跑完自动在浏览器打开 HTML 报告", value=False)

    go = st.button("▶️ 开始分析", type="primary", use_container_width=True,
                   disabled=not (ticker and date and chosen))

    if go:
        if not chosen:
            st.error("至少选择一名分析师")
            st.stop()
        ticker = ticker.strip().upper()
        proxies = tuple(
            s.strip().upper() for s in proxies_raw.replace("，", ",").split(",") if s.strip()
        )
        graph = build_graph(tuple(chosen), deep_llm.strip(), quick_llm.strip(),
                            dict(VENDOR_CHAINS[vendor_choice]), proxies)

        st.info(f"开始分析 **{ticker}** @ {date} · 分析师：{', '.join(ANALYSTS[a] for a in chosen)}\n\n"
                f"数据源：{vendor_choice}"
                + (f" · 宏观新闻代理：{', '.join(proxies)}" if proxies and not
                   VENDOR_CHAINS[vendor_choice]["news_data"].startswith("yfinance") else "")
                + "\n\n⏱️ 真实行情全流程约 10-20 分钟，请勿关闭页面")
        status_box = st.empty()
        log_box = st.empty()
        t0 = time.time()
        try:
            final_state, log, stage = run_analysis(graph, ticker, date)
        except Exception as e:
            status_box.error(f"❌ 分析失败：{type(e).__name__}: {e}")
            if log:
                with st.expander("失败前的输出", expanded=False):
                    st.code("\n".join(log[-60:]), language=None)
            st.stop()

        elapsed = time.time() - t0
        status_box.success(f"✅ 分析完成，耗时 {elapsed/60:.1f} 分钟 · 最后一阶段：{stage}")

        # ---- 最终决策 ----
        decision = (final_state.get("final_trade_decision") or "").strip()
        st.subheader("⚖️ 最终决策")
        if decision:
            from tradingagents.agents.rating import parse_rating

            rating = parse_rating(decision)
            st.markdown(f"**评级：`{rating}`**")
            st.markdown(decision)
        else:
            st.warning("未解析到最终决策文本，请查看完整报告。")

        # ---- 保存报告 ----
        if save_report:
            with st.spinner("保存报告…"):
                save_path = graph.default_report_path(ticker)
                report_file = graph.save_reports(final_state, ticker, save_path, html=True)
            html_file = report_file.parent / "complete_report.html"
            st.success(f"报告已保存：`{report_file.parent}`")
            b1, b2, _ = st.columns([1.5, 1.5, 3])
            if b1.button("🌐 浏览器打开 HTML 报告") and html_file.exists():
                webbrowser.open(html_file.resolve().as_uri())
            if b2.button("📖 页面内查看完整报告") and report_file.exists():
                st.session_state["view_report"] = str(report_file)
        else:
            save_path = None

        # ---- 过程日志 ----
        with st.expander("🧾 过程日志（智能体输出流）", expanded=False):
            st.code("\n".join(log[-200:]), language=None)

        if st.session_state.get("view_report"):
            st.divider()
            st.subheader("📖 完整报告")
            st.markdown(Path(st.session_state["view_report"]).read_text(encoding="utf-8"))

# ============ 历史报告 ============
else:
    st.header("📚 历史报告")
    report_files = sorted(
        REPORTS_ROOT.glob("*/complete_report.md"),
        key=lambda p: p.stat().st_mtime, reverse=True,
    ) if REPORTS_ROOT.exists() else []
    if not report_files:
        st.info(f"还没有已保存的报告（目录：`{REPORTS_ROOT}`）。先去「新建分析」跑一轮并勾选保存。")
        st.stop()

    def _label(p: Path) -> str:
        import datetime
        mtime = datetime.datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M")
        return f"{p.parent.name}  ·  {mtime}"

    pick = st.selectbox("选择报告", report_files, format_func=_label)
    html_file = pick.parent / "complete_report.html"
    b1, b2, _ = st.columns([1.5, 1.5, 3])
    if b1.button("🌐 浏览器打开 HTML 版") and html_file.exists():
        webbrowser.open(html_file.resolve().as_uri())
    st.divider()
    st.markdown(pick.read_text(encoding="utf-8"))
