import streamlit as st
import pandas as pd
import numpy as np

st.set_page_config(page_title="二批次覆盖率回测-组合级建议", layout="wide")

st.title("二批次覆盖率提升回测（组合级建议）")
st.caption("上传订单级明细（自动回测-明细表），生成：组合级回测结果表 + 阈值建议 + 解锁订单明细。")

# -----------------------
# 1) 上传文件
# -----------------------
file = st.file_uploader("上传 Excel / CSV", type=["xlsx", "csv"])
if not file:
    st.stop()

if file.name.lower().endswith(".csv"):
    df = pd.read_csv(file)
else:
    df = pd.read_excel(file)

# -----------------------
# 2) 字段校验（按你真实字段名）
# -----------------------
REQ_COLS = [
    "销售订单号",
    "包裹数",
    "配送批次",
    "最优二批次服务商组合",
    "费用增幅%(二批次vs单包裹)",
    "是否满足当前二批次阈值",
    "二批次vs实际批次_尾程运费变化",
    "二批次最优_尾程费用",
]
missing = [c for c in REQ_COLS if c not in df.columns]
if missing:
    st.error(f"缺少必要字段：{missing}")
    st.stop()

# 统一类型
df = df.copy()
df["销售订单号"] = df["销售订单号"].astype(str)
df["最优二批次服务商组合"] = df["最优二批次服务商组合"].astype(str)

# 费用增幅% 可能是 “16.40%” 这种字符串，做一次清洗
def pct_to_float(x):
    if pd.isna(x):
        return np.nan
    if isinstance(x, str):
        x = x.strip().replace("%", "")
        if x == "":
            return np.nan
        return float(x) / 100.0
    return float(x)

df["费用增幅_pct"] = df["费用增幅%(二批次vs单包裹)"].apply(pct_to_float)

# -----------------------
# 3) 左侧参数区（输入）
# -----------------------
st.sidebar.header("回测参数（输入）")

base_threshold = 0.07  # 当前系统阈值固定
sim_threshold_pct = st.sidebar.slider("模拟阈值（%）", 7, 25, 15, 1)
sim_threshold = sim_threshold_pct / 100.0

# 建议规则参数（可调）
min_combo_orders = st.sidebar.number_input("组合最小样本数（用于输出建议）", min_value=1, value=1, step=1)
max_avg_delta = st.sidebar.number_input("P0 单均成本增量上限（本币）", min_value=0.0, value=50.0, step=5.0)
p90_uplift_cap = st.sidebar.slider("P0 P90费用增幅上限（%）", 7, 50, 18, 1) / 100.0

# -----------------------
# 4) 基础分层：候选池 & 被卡住池
# -----------------------
# 候选池：进入分批决策且当前最终>=3批
candidate = df[(df["包裹数"] >= 3) & (df["配送批次"] >= 3)].copy()

# 被阈值卡住池：>7%（按你列“是否满足当前二批次阈值”的口径：1=不满足7%）
blocked = candidate[candidate["是否满足当前二批次阈值"] == 1].copy()

# 解锁：在模拟阈值下可放行
blocked["是否解锁"] = blocked["费用增幅_pct"] <= sim_threshold
unlocked = blocked[blocked["是否解锁"]].copy()

# -----------------------
# 5) 组合级回测结果表
# -----------------------
def pctl(s, q):
    s = pd.Series(s).dropna()
    if len(s) == 0:
        return np.nan
    return float(np.percentile(s, q))

def batch_dist_str(x):
    # x: series of 配送批次
    vc = x.value_counts().sort_index()
    return ",".join([f"{int(k)}批:{int(v)}" for k, v in vc.items()])

combo_key = "最优二批次服务商组合"

# 先做 blocked 聚合（基底）
blocked_g = (
    blocked.groupby(combo_key)
    .agg(
        当前被卡住订单数=("销售订单号", "nunique"),
        被卡住平均费用增幅_pct=("费用增幅_pct", "mean"),
        被卡住P90费用增幅_pct=("费用增幅_pct", lambda s: pctl(s, 90)),
    )
    .reset_index()
)

# 再做 unlocked 聚合（解锁增量）
unlocked_g = (
    unlocked.groupby(combo_key)
    .agg(
        解锁订单数=("销售订单号", "nunique"),
        成本增量_本币=("二批次vs实际批次_尾程运费变化", "sum"),
        单均成本增量_本币=("二批次vs实际批次_尾程运费变化", "mean"),
        解锁P50费用增幅_pct=("费用增幅_pct", "median"),
        解锁P90费用增幅_pct=("费用增幅_pct", lambda s: pctl(s, 90)),
        解锁包裹数_P50=("包裹数", "median"),
        解锁订单批次分布=("配送批次", batch_dist_str),
    )
    .reset_index()
)

combo_res = blocked_g.merge(unlocked_g, on=combo_key, how="left")
combo_res["解锁订单数"] = combo_res["解锁订单数"].fillna(0).astype(int)
combo_res["成本增量_本币"] = combo_res["成本增量_本币"].fillna(0.0)
combo_res["单均成本增量_本币"] = combo_res["单均成本增量_本币"].fillna(np.nan)

combo_res["解锁订单占比"] = combo_res["解锁订单数"] / combo_res["当前被卡住订单数"].replace(0, np.nan)
combo_res["模拟阈值_pct"] = sim_threshold
combo_res["当前阈值_pct"] = base_threshold

# -----------------------
# 6) 建议等级 & 建议阈值输出（建议结构）
# -----------------------
def judge_level(row):
    # 样本太少：P2
    if row["当前被卡住订单数"] < min_combo_orders:
        return "P2"
    if row["解锁订单数"] <= 0:
        return "P2"
    # 规则：P0 强建议
    if (pd.notna(row["单均成本增量_本币"]) and row["单均成本增量_本币"] <= max_avg_delta
        and pd.notna(row["解锁P90费用增幅_pct"]) and row["解锁P90费用增幅_pct"] <= p90_uplift_cap):
        return "P0"
    return "P1"

combo_res["建议等级"] = combo_res.apply(judge_level, axis=1)
combo_res["建议阈值_pct"] = np.where(combo_res["解锁订单数"] > 0, sim_threshold, np.nan)

def reason(row):
    if row["解锁订单数"] <= 0:
        return "模拟阈值下无可解锁订单"
    return (f"在阈值{int(sim_threshold*100)}%下可解锁{row['解锁订单数']}单，"
            f"成本增量合计{row['成本增量_本币']:.2f}，"
            f"单均{(row['单均成本增量_本币'] if pd.notna(row['单均成本增量_本币']) else 0):.2f}，"
            f"P90费用增幅{(row['解锁P90费用增幅_pct']*100 if pd.notna(row['解锁P90费用增幅_pct']) else 0):.1f}%")

combo_res["建议理由"] = combo_res.apply(reason, axis=1)

# 建议表（更像结论）
reco = combo_res.loc[:, [
    combo_key,
    "当前阈值_pct",
    "建议阈值_pct",
    "解锁订单数",
    "成本增量_本币",
    "单均成本增量_本币",
    "建议等级",
    "建议理由"
]].copy()

reco = reco.sort_values(["建议等级", "解锁订单数"], ascending=[True, False])

# -----------------------
# 7) 页面输出：左侧输入 / 右侧输出
# -----------------------
left, right = st.columns([1, 2], gap="large")

with left:
    st.subheader("🧩 回测参数区（输入）")
    st.write(f"- 当前系统阈值：**{int(base_threshold*100)}%**")
    st.write(f"- 模拟阈值：**{sim_threshold_pct}%**")
    st.write(f"- 候选订单数（包裹≥3 & 最终≥3批）：**{candidate['销售订单号'].nunique()}**")
    st.write(f"- 当前被阈值卡住订单数（>7%）：**{blocked['销售订单号'].nunique()}**")
    st.write(f"- 模拟解锁订单数：**{unlocked['销售订单号'].nunique()}**")

with right:
    st.subheader("📌 组合级建议输出（Recommendation）")
    st.dataframe(reco, use_container_width=True, height=260)

    st.subheader("📊 组合级回测结果表（Combo Simulation Result）")
    show_cols = [
        combo_key, "当前被卡住订单数", "解锁订单数", "解锁订单占比",
        "成本增量_本币", "单均成本增量_本币",
        "解锁P50费用增幅_pct", "解锁P90费用增幅_pct",
        "解锁包裹数_P50", "解锁订单批次分布",
        "建议阈值_pct", "建议等级"
    ]
    st.dataframe(combo_res[show_cols].sort_values("解锁订单数", ascending=False),
                 use_container_width=True, height=320)

    st.subheader("🔍 解锁订单明细（按组合联动）")
    combo_list = combo_res[combo_key].dropna().unique().tolist()
    selected_combo = st.selectbox("选择一个最优二批次服务商组合", options=combo_list)

    detail_cols = [
        "销售订单号", "包裹数", "配送批次", "最优二批次服务商组合",
        "费用增幅%(二批次vs单包裹)", "二批次vs实际批次_尾程运费变化", "二批次最优_尾程费用"
    ]
    detail = unlocked[unlocked[combo_key] == selected_combo].copy()
    st.dataframe(detail[detail_cols], use_container_width=True, height=320)

    # 可选：导出下载
    st.download_button(
        "下载组合级结果（CSV）",
        combo_res.to_csv(index=False).encode("utf-8-sig"),
        file_name="combo_simulation_result_v1.csv",
        mime="text/csv",
    )
    st.download_button(
        "下载解锁订单明细（CSV）",
        unlocked.to_csv(index=False).encode("utf-8-sig"),
        file_name="unlocked_order_detail.csv",
        mime="text/csv",
    )
