"""
CPI 監控模組 — 可獨立運行或整合進現有 Streamlit app
用法：streamlit run cpi_monitor.py
依賴：pip install streamlit requests pandas plotly
"""

import streamlit as st
import requests
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timezone
import time
import hashlib
import json

# ── 設定 ──────────────────────────────────────────────────────────────────────
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

CPI_SERIES = {
    "headline_cpi":  ("CPIAUCSL",  "CPI 整體（月率%）",   "headline"),
    "core_cpi":      ("CPILFESL",  "核心CPI（月率%）",     "core"),
    "energy_cpi":    ("CPIENGSL",  "能源CPI（月率%）",     "energy"),
    "food_cpi":      ("CPIFABSL",  "食品CPI（月率%）",     "food"),
}

# 關鍵閾值（核心CPI月率）
CORE_HOT_THRESHOLD  = 0.3   # > 0.3% → 偏熱，聯儲局推遲減息
CORE_COOL_THRESHOLD = 0.2   # ≤ 0.2% → 偏冷，減息預期升溫

# ── 工具函數 ──────────────────────────────────────────────────────────────────

def inject_css():
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=Noto+Sans+TC:wght@400;700&display=swap');

    html, body, [class*="css"] {
        font-family: 'Noto Sans TC', sans-serif;
        background-color: #f5f0e8;
        color: #2c2c2c;
    }
    .metric-card {
        background: #ffffff;
        border: 1px solid #d6cfc0;
        border-radius: 10px;
        padding: 16px 20px;
        margin-bottom: 12px;
    }
    .metric-label {
        font-size: 13px;
        color: #888;
        margin-bottom: 4px;
    }
    .metric-value {
        font-family: 'IBM Plex Mono', monospace;
        font-size: 28px;
        font-weight: 600;
    }
    .hot   { color: #d63031; }
    .cool  { color: #00b894; }
    .neutral { color: #636e72; }
    .signal-box {
        border-radius: 10px;
        padding: 16px 20px;
        font-size: 15px;
        font-weight: 700;
        margin-bottom: 16px;
        text-align: center;
    }
    .signal-hot  { background: #ffe0e0; border: 2px solid #d63031; color: #d63031; }
    .signal-cool { background: #e0fff4; border: 2px solid #00b894; color: #00b894; }
    .signal-wait { background: #f0f0f0; border: 2px solid #b2bec3; color: #636e72; }
    </style>
    """, unsafe_allow_html=True)


@st.cache_data(ttl=300)
def fetch_fred(series_id: str, api_key: str, limit: int = 24) -> pd.DataFrame:
    """從 FRED 抓取指定序列的最近 N 期數據"""
    try:
        r = requests.get(FRED_BASE, params={
            "series_id": series_id,
            "api_key": api_key,
            "sort_order": "desc",
            "limit": limit,
            "file_type": "json",
            "units": "pc1",          # 按年變化%
        }, timeout=10)
        r.raise_for_status()
        obs = r.json().get("observations", [])
        df = pd.DataFrame(obs)[["date", "value"]].copy()
        df["date"] = pd.to_datetime(df["date"])
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        df = df.dropna().sort_values("date")
        return df
    except Exception as e:
        return pd.DataFrame(columns=["date", "value"])


@st.cache_data(ttl=300)
def fetch_fred_mom(series_id: str, api_key: str, limit: int = 24) -> pd.DataFrame:
    """月率變化%"""
    try:
        r = requests.get(FRED_BASE, params={
            "series_id": series_id,
            "api_key": api_key,
            "sort_order": "desc",
            "limit": limit,
            "file_type": "json",
            "units": "pch",          # 月率變化%
        }, timeout=10)
        r.raise_for_status()
        obs = r.json().get("observations", [])
        df = pd.DataFrame(obs)[["date", "value"]].copy()
        df["date"] = pd.to_datetime(df["date"])
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        df = df.dropna().sort_values("date")
        return df
    except Exception as e:
        return pd.DataFrame(columns=["date", "value"])


def send_telegram(token: str, chat_id: str, msg: str, dedup_key: str):
    """發送 Telegram 通知，MD5 去重"""
    key = hashlib.md5(dedup_key.encode()).hexdigest()
    sent = st.session_state.get("tg_sent", set())
    if key in sent:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(url, json={
            "chat_id": chat_id,
            "text": msg,
            "parse_mode": "HTML"
        }, timeout=8)
        sent.add(key)
        st.session_state["tg_sent"] = sent
    except Exception:
        pass


def interpret_core(mom_val: float) -> tuple[str, str, str]:
    """解讀核心CPI月率，返回 (訊號, CSS類, 市場影響)"""
    if mom_val > CORE_HOT_THRESHOLD:
        return (
            f"🔴 偏熱 {mom_val:.2f}% > {CORE_HOT_THRESHOLD}%",
            "signal-hot",
            "通脹升溫 → 聯儲局推遲減息 → 利率預期上升 → NQ 承壓"
        )
    elif mom_val <= CORE_COOL_THRESHOLD:
        return (
            f"🟢 偏冷 {mom_val:.2f}% ≤ {CORE_COOL_THRESHOLD}%",
            "signal-cool",
            "通脹受控 → 減息預期升溫 → 利率預期下降 → NQ 可能反彈"
        )
    else:
        return (
            f"🟡 中性 {mom_val:.2f}%",
            "signal-wait",
            "介乎閾值之間，市場反應視乎其他分項（能源/食品）"
        )


def plot_cpi_history(df_yoy: pd.DataFrame, df_mom: pd.DataFrame, title: str):
    """雙軸：按年（左）+ 按月（右）"""
    fig = go.Figure()
    if not df_yoy.empty:
        fig.add_trace(go.Scatter(
            x=df_yoy["date"], y=df_yoy["value"],
            name="按年%", line=dict(color="#6c8ebf", width=2),
            yaxis="y1"
        ))
    if not df_mom.empty:
        fig.add_trace(go.Bar(
            x=df_mom["date"], y=df_mom["value"],
            name="按月%", marker_color=[
                "#d63031" if v > 0 else "#00b894" for v in df_mom["value"]
            ],
            yaxis="y2", opacity=0.6
        ))
    fig.update_layout(
        title=title,
        paper_bgcolor="#f5f0e8",
        plot_bgcolor="#faf7f2",
        font=dict(family="IBM Plex Mono", color="#2c2c2c"),
        yaxis=dict(title="按年%", tickformat=".1f", gridcolor="#e0d8cc"),
        yaxis2=dict(title="按月%", overlaying="y", side="right",
                    tickformat=".2f", gridcolor="#e0d8cc"),
        legend=dict(orientation="h", y=1.1),
        height=320,
        margin=dict(l=40, r=40, t=50, b=30),
    )
    return fig


# ── 主介面 ────────────────────────────────────────────────────────────────────

def main():
    st.set_page_config(
        page_title="CPI 監控儀表板",
        page_icon="📊",
        layout="wide"
    )
    inject_css()

    st.title("📊 CPI 通脹監控儀表板")
    st.caption("監控美國消費者物價指數，實時解讀對聯儲局政策及 NQ/ES 期貨的影響")

    # ── 側邊欄設定 ────────────────────────────────────────────────────────────
    with st.sidebar:
        st.header("⚙️ 設定")
        fred_key = st.text_input(
            "FRED API Key",
            type="password",
            help="免費申請：https://fred.stlouisfed.org/docs/api/api_key.html"
        )
        st.divider()
        st.subheader("Telegram 通知（選填）")
        tg_token   = st.text_input("Bot Token", type="password")
        tg_chat_id = st.text_input("Chat ID")
        st.divider()
        st.subheader("閾值設定")
        hot_thresh  = st.number_input("偏熱閾值（核心CPI月率%）", value=0.3, step=0.05, format="%.2f")
        cool_thresh = st.number_input("偏冷閾值（核心CPI月率%）", value=0.2, step=0.05, format="%.2f")
        auto_refresh = st.toggle("自動刷新（5分鐘）", value=False)

    if not fred_key:
        st.info("👈 請在左側輸入 FRED API Key 開始監控\n\n免費申請：https://fred.stlouisfed.org/docs/api/api_key.html")
        st.stop()

    # ── 即時時鐘 ──────────────────────────────────────────────────────────────
    now_et = datetime.now(timezone.utc)
    col_time, col_btn = st.columns([3, 1])
    with col_time:
        st.markdown(f"🕐 現在時間（UTC）：**{now_et.strftime('%Y-%m-%d %H:%M:%S')}**　｜　ET = UTC-4　→　**{(now_et.hour - 4) % 24:02d}:{now_et.minute:02d} ET**")
    with col_btn:
        if st.button("🔄 立即刷新", use_container_width=True):
            st.cache_data.clear()

    st.divider()

    # ── 抓取數據 ──────────────────────────────────────────────────────────────
    with st.spinner("正在從 FRED 抓取最新 CPI 數據..."):
        data = {}
        for key, (sid, label, kind) in CPI_SERIES.items():
            data[key] = {
                "yoy": fetch_fred(sid, fred_key),
                "mom": fetch_fred_mom(sid, fred_key),
                "label": label,
                "kind": kind,
            }

    # ── 核心訊號區 ────────────────────────────────────────────────────────────
    st.subheader("🎯 核心訊號")

    core_mom_df = data["core_cpi"]["mom"]
    headline_mom_df = data["headline_cpi"]["mom"]

    if not core_mom_df.empty and not headline_mom_df.empty:
        core_latest    = core_mom_df.iloc[-1]["value"]
        headline_latest = headline_mom_df.iloc[-1]["value"]
        core_prev      = core_mom_df.iloc[-2]["value"] if len(core_mom_df) > 1 else None
        latest_date    = core_mom_df.iloc[-1]["date"].strftime("%Y年%m月")

        signal, css_class, market_impact = interpret_core(core_latest)

        st.markdown(f"""
        <div class="signal-box {css_class}">
            核心CPI（{latest_date}）月率：{signal}
        </div>
        """, unsafe_allow_html=True)

        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">📈 市場影響解讀</div>
            <div style="font-size:15px; margin-top:6px;">{market_impact}</div>
        </div>
        """, unsafe_allow_html=True)

        # 四格指標
        c1, c2, c3, c4 = st.columns(4)
        metrics = [
            ("核心CPI 月率", core_latest, core_prev, "%"),
            ("整體CPI 月率", headline_latest,
             headline_mom_df.iloc[-2]["value"] if len(headline_mom_df) > 1 else None, "%"),
            ("整體CPI 按年", data["headline_cpi"]["yoy"].iloc[-1]["value"]
             if not data["headline_cpi"]["yoy"].empty else None, None, "%"),
            ("核心CPI 按年", data["core_cpi"]["yoy"].iloc[-1]["value"]
             if not data["core_cpi"]["yoy"].empty else None, None, "%"),
        ]
        for col, (label, val, prev, unit) in zip([c1, c2, c3, c4], metrics):
            if val is not None:
                delta = f"{val - prev:+.2f}%" if prev is not None else None
                color_class = "hot" if val > CORE_HOT_THRESHOLD else "cool" if val <= CORE_COOL_THRESHOLD else "neutral"
                col.markdown(f"""
                <div class="metric-card">
                    <div class="metric-label">{label}</div>
                    <div class="metric-value {color_class}">{val:.2f}{unit}</div>
                    {"<div style='font-size:12px;color:#888;'>vs 上期 "+delta+"</div>" if delta else ""}
                </div>
                """, unsafe_allow_html=True)

        # Telegram 通知
        if tg_token and tg_chat_id:
            msg = (
                f"📊 <b>CPI 數據更新</b>（{latest_date}）\n"
                f"核心CPI 月率：<b>{core_latest:.2f}%</b>\n"
                f"整體CPI 月率：<b>{headline_latest:.2f}%</b>\n"
                f"訊號：{signal}\n"
                f"影響：{market_impact}"
            )
            send_telegram(tg_token, tg_chat_id, msg,
                          dedup_key=f"cpi_{latest_date}_{core_latest:.3f}")

    else:
        st.warning("⚠️ 無法獲取 CPI 數據，請檢查 FRED API Key 是否正確")

    st.divider()

    # ── 歷史圖表 ──────────────────────────────────────────────────────────────
    st.subheader("📉 歷史走勢（近24個月）")

    tab1, tab2, tab3, tab4 = st.tabs(["整體CPI", "核心CPI", "能源CPI", "食品CPI"])
    tabs = [tab1, tab2, tab3, tab4]
    keys = ["headline_cpi", "core_cpi", "energy_cpi", "food_cpi"]

    for tab, key in zip(tabs, keys):
        with tab:
            d = data[key]
            if not d["yoy"].empty:
                fig = plot_cpi_history(d["yoy"], d["mom"], d["label"])
                st.plotly_chart(fig, use_container_width=True)

                # 最新數據表格
                latest_row = d["mom"].iloc[-1] if not d["mom"].empty else None
                if latest_row is not None:
                    st.caption(f"最新數據：{latest_row['date'].strftime('%Y-%m')}　月率：**{latest_row['value']:.2f}%**")
            else:
                st.info("暫無數據")

    st.divider()

    # ── NQ/ES 快速參考 ────────────────────────────────────────────────────────
    st.subheader("📌 CPI 公佈後快速判斷框架")
    st.markdown("""
    | 核心CPI 月率 | 訊號 | 聯儲局預期 | NQ 期貨影響 |
    |---|---|---|---|
    | **> 0.3%** | 🔴 偏熱 | 推遲減息 | 承壓，可能下跌 |
    | **0.2% ~ 0.3%** | 🟡 中性 | 維持現狀 | 波動，觀望 |
    | **≤ 0.2%** | 🟢 偏冷 | 減息預期升溫 | 可能反彈 |

    > **今日重點（2026年4月CPI）：** 市場預期整體 +3.7% YoY，核心 +0.3% MoM。  
    > 若核心突破 0.3%，代表高油價（伊朗戰爭）已滲透至非能源品類，聯儲局壓力增大。
    """)

    # ── 自動刷新 ──────────────────────────────────────────────────────────────
    if auto_refresh:
        time.sleep(300)
        st.rerun()


if __name__ == "__main__":
    main()
