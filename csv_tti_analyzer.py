"""LTE 空口 TTI 级 CSV 日志分析器（DataParser 专家能力之一）。

分析对象：CMAC/PDCP/RLC 的 dl/ulUeTtiInfo_Lte 导出 CSV。
特点：每行是一个 TTI 事件，字段稀疏（'-' 表示该 TTI 无此事件）。

分析维度（按 UE/Cell 聚合）：
- CMAC DL/UL：调度次数、HARQ 失败次数/失败率、平均重传次数、MCS 分布、SINR/CQI/TA、调度失败原因
- PDCP DL/UL：PDU 数、SN 跳变（丢包）、SN 回退（乱序）、RLC buffer 峰值
- RLC DL/UL：RLC SN 重传、NACK 次数、状态 PDU

输出：结构化中文报告 + 异常判定（阈值可配）。

依赖 pandas（已在 requirements.txt）。所有解析失败均返回可读说明，不抛异常中断主流程。
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("lte_nr_agent.csv_tti")

BASE_DIR = Path(__file__).resolve().parent

# 默认异常阈值（可用 analyze_tti_csv 的 thresholds 覆盖）
DEFAULT_THRESHOLDS = {
    "harq_fail_rate_warn": 0.10,   # HARQ 失败率 >10% 告警
    "harq_fail_rate_crit": 0.30,   # >30% 严重
    "retx_avg_warn": 1.5,          # 平均重传次数 >1.5 告警
    "sinr_low_warn": 0.0,          # PUSCH SINR <0dB 告警
    "pdcp_gap_warn": 3,            # PDCP SN 跳变 >3 视为丢包嫌疑
}

FILE_KIND_MAP = {
    "cmac_dl": "CMAC_DL",
    "cmac_ul": "CMAC_UL",
    "pdcp_dl": "PDCP_DL",
    "pdcp_ul": "PDCP_UL",
    "rlc_dl": "RLC_DL",
    "rlc_ul": "RLC_UL",
}


def classify_file(name: str) -> str:
    low = name.lower()
    layer = "cmac" if "cmac" in low else ("pdcp" if "pdcp" in low else ("rlc" if "rlc" in low else ""))
    direction = "dl" if "_dl" in low else ("ul" if "_ul" in low else "")
    return FILE_KIND_MAP.get(f"{layer}_{direction}", "UNKNOWN")


def _to_num(series):
    """把 CSV 列转为数值，- 或空 -> NaN。"""
    import pandas as pd
    return pd.to_numeric(series.astype(str).str.strip().replace({"-": None, "": None}), errors="coerce")


def _read_csv(path: Path, usecols=None, chunksize=200000):
    import pandas as pd
    frames = []
    for chunk in pd.read_csv(path, usecols=usecols, chunksize=chunksize,
                             encoding="utf-8-sig", low_memory=False, dtype=str):
        frames.append(chunk)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _pick(df, *candidates):
    """返回第一个存在的列名。"""
    for name in candidates:
        if name in df.columns:
            return name
    return None


def analyze_cmac(df, kind, thresholds):
    """CMAC DL/UL：HARQ/重传/MCS/SINR/CQI/TA/调度失败。"""
    import pandas as pd
    ue_col = _pick(df, "UEGID", "UEGid") or ("UEGID" if "UEGID" in df.columns else None)
    if ue_col is None:
        return None, "缺少 UEGID 列"

    is_dl = kind == "CMAC_DL"
    if is_dl:
        harq_col = _pick(df, "ACK.b8IsHarqFail", "SCHD.u8HarqId")
        ack_col = _pick(df, "ACK.u8Ack")
        retx_col = _pick(df, "TB.u8TxCnt0", "TB.u8TxCnt1")
        mcs_col = _pick(df, "TB.u8Mcs0", "AMC.aucSchdMCS0")
        cqi_col = _pick(df, "CSI.u8WideBandCqi1")
        ta_col = _pick(df, "TA.s16FltTa", "TA.s16RptTa")
    else:
        harq_col = _pick(df, "CRCI.HarqFail")
        ack_col = _pick(df, "CRCI.CRCTb")
        retx_col = _pick(df, "GRANT.u8TxCnt")
        mcs_col = _pick(df, "GRANT.u8Mcs", "AMC.ucMCS")
        cqi_col = None
        ta_col = _pick(df, "MSG1.s16TaVal")

    fail_col = _pick(df, "SCHD_FAIL_RSN.u32UeSchdFailRsn")

    result = {"kind": kind, "total_rows": len(df), "ues": {}}
    for ue, group in df.groupby(ue_col, sort=False):
        stat = {"rows": len(group)}
        if harq_col:
            harq = _to_num(group[harq_col])
            stat["harq_samples"] = int(harq.notna().sum())
            stat["harq_fail"] = int((harq > 0).sum())
            stat["harq_fail_rate"] = round(stat["harq_fail"] / max(stat["harq_samples"], 1), 4)
        if ack_col:
            ack = _to_num(group[ack_col])
            ack_valid = ack.notna().sum()
            stat["nack"] = int((ack == 0).sum())
            stat["nack_rate"] = round(stat["nack"] / max(int(ack_valid), 1), 4)
        if retx_col:
            # uint8 的 255 通常是无效/哨兵值，过滤掉避免污染统计
            retx = _to_num(group[retx_col])
            retx = retx[(retx >= 0) & (retx <= 15)]
            stat["retx_max"] = float(retx.max()) if len(retx) else 0.0
            stat["retx_avg"] = round(float(retx.mean()), 3) if len(retx) else 0.0
            stat["retx_ge2"] = int((retx >= 2).sum())
        if mcs_col:
            mcs = _to_num(group[mcs_col]).dropna()
            stat["mcs_avg"] = round(float(mcs.mean()), 2) if len(mcs) else None
            stat["mcs_min"] = int(mcs.min()) if len(mcs) else None
        if cqi_col:
            cqi = _to_num(group[cqi_col]).dropna()
            stat["cqi_avg"] = round(float(cqi.mean()), 2) if len(cqi) else None
        if ta_col:
            ta = _to_num(group[ta_col]).dropna()
            if len(ta):
                stat["ta_min"] = float(ta.min())
                stat["ta_max"] = float(ta.max())
                stat["ta_range"] = round(float(ta.max() - ta.min()), 2)
        if fail_col:
            fail = _to_num(group[fail_col])
            stat["schd_fail"] = int((fail > 0).sum())
        result["ues"][str(ue)] = stat
    return result, None


def analyze_pdcp(df, kind, thresholds):
    """PDCP DL/UL：PDU 数、SN 跳变（丢包）、SN 回退（乱序）、buffer 峰值。"""
    ue_col = _pick(df, "UEGID", "UEGid")
    if ue_col is None:
        return None, "缺少 UEGID 列"
    is_dl = kind == "PDCP_DL"
    sn_col = _pick(df, "DL_DATA_PDU.u32pdcpSn" if is_dl else "UL_DATA_PDU.u32pdcpSn")
    cnt_col = _pick(df, "DL_DATA_PDU.u32pdcpCount" if is_dl else "UL_DATA_PDU.u32pdcpCount")
    buf_col = _pick(df, "DL_DATA_PDU.u32rlcbufsize") if is_dl else _pick(df, "UL_DATA_PDU.wNextPdcpRxSn")
    rbid_col = _pick(df, "DL_DATA_PDU.u8rbid" if is_dl else "UL_DATA_PDU.u8rbid")

    gap_thr = thresholds["pdcp_gap_warn"]
    result = {"kind": kind, "total_rows": len(df), "ues": {}}
    for ue, group in df.groupby(ue_col, sort=False):
        stat = {"rows": len(group)}
        # 按 rbid 分别看 SN 连续性
        sn_gaps = 0
        sn_reorder = 0
        sn_samples = 0
        group_iter = group.groupby(rbid_col) if rbid_col else [(None, group)]
        for _, sub in group_iter:
            col = sn_col or cnt_col
            if col:
                sn = _to_num(sub[col]).dropna().astype(int).sort_values()
                if len(sn) > 1:
                    diffs = sn.diff().dropna()
                    sn_samples += len(diffs)
                    sn_gaps += int((diffs > gap_thr).sum())
                    sn_reorder += int((diffs < 0).sum())
        stat["sn_samples"] = sn_samples
        stat["sn_gaps"] = sn_gaps       # SN 大跳变（疑似丢包）
        stat["sn_reorder"] = sn_reorder
        if buf_col:
            buf = _to_num(group[buf_col]).dropna()
            stat["buf_max"] = float(buf.max()) if len(buf) else None
        result["ues"][str(ue)] = stat
    return result, None


def analyze_rlc(df, kind, thresholds):
    """RLC DL/UL：RLC SN 重传、NACK、状态 PDU。"""
    ue_col = _pick(df, "UEGID", "UEGid")
    if ue_col is None:
        return None, "缺少 UEGID 列"
    is_dl = kind == "RLC_DL"
    sn_col = _pick(df, "DL_DATA_PDU.u32Rlcsn" if is_dl else "UL_DATA_PDU.u32Rlcsn")
    nack_col = _pick(df, "DL_STATUS_PDU.u16NackSn" if is_dl else "UL_STATUS_PDU.u16NackSn")
    ack_col = _pick(df, "DL_STATUS_PDU.u16Acksn" if is_dl else "UL_STATUS_PDU.u16Acksn")

    result = {"kind": kind, "total_rows": len(df), "ues": {}}
    for ue, group in df.groupby(ue_col, sort=False):
        stat = {"rows": len(group)}
        if sn_col:
            sn = _to_num(group[sn_col]).dropna().astype(int)
            if len(sn) > 1:
                stat["rlc_sn_retrans"] = int((sn.diff() <= 0).sum())
        if nack_col:
            stat["nack_count"] = int((_to_num(group[nack_col]) > 0).sum())
        if ack_col:
            stat["ack_count"] = int((_to_num(group[ack_col]) > 0).sum())
        result["ues"][str(ue)] = stat
    return result, None


ANALYZERS = {
    "CMAC_DL": analyze_cmac, "CMAC_UL": analyze_cmac,
    "PDCP_DL": analyze_pdcp, "PDCP_UL": analyze_pdcp,
    "RLC_DL": analyze_rlc, "RLC_UL": analyze_rlc,
}


def analyze_tti_csv(path: str, thresholds: dict | None = None, max_ues: int = 20) -> str:
    """主入口：分析单个 TTI CSV 文件，返回结构化中文报告。

    Args:
        path: CSV 文件路径或包含多个 CSV 的目录。
        thresholds: 覆盖默认异常阈值。
        max_ues: 报告中最多展示的 UE 数。
    """
    thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    target = Path(path) if Path(path).is_absolute() else BASE_DIR / path
    if not target.exists():
        return f"TTI CSV路径不存在：{target}"

    files = sorted(target.glob("*.csv")) if target.is_dir() else [target]
    files = [f for f in files if classify_file(f.name) != "UNKNOWN"] if target.is_dir() else files
    if not files:
        return f"未找到可分析的 TTI CSV：{target}"

    sections = []
    for file in files:
        kind = classify_file(file.name)
        analyzer = ANALYZERS.get(kind)
        if analyzer is None:
            sections.append(f"【{file.name}】未识别的文件类型，跳过")
            continue
        try:
            df = _read_csv(file)
            if df.empty:
                sections.append(f"【{file.name}】空文件")
                continue
            result, error = analyzer(df, kind, thresholds)
            if error:
                sections.append(f"【{file.name}】分析失败：{error}")
                continue
            sections.append(_format_result(file, result, thresholds, max_ues))
        except Exception as exc:
            logger.warning("TTI CSV分析失败 %s: %s", file.name, exc)
            sections.append(f"【{file.name}】分析异常：{type(exc).__name__}: {exc}")

    header = f"【LTE空口TTI CSV分析】共 {len(files)} 个文件\n"
    return header + "\n\n".join(sections)


def _format_result(file: Path, result: dict, thresholds: dict, max_ues: int) -> str:
    kind = result["kind"]
    ues = result["ues"]
    lines = [f"--- {file.name}（{kind}）总记录={result['total_rows']}, UE数={len(ues)} ---"]
    anomalies = []
    for ue, stat in list(ues.items())[:max_ues]:
        parts = [f"UE={ue}"]
        for key in ("harq_fail_rate", "nack_rate", "retx_avg", "retx_max", "retx_ge2",
                    "mcs_avg", "mcs_min", "cqi_avg", "ta_range", "schd_fail",
                    "sn_gaps", "sn_reorder", "rlc_sn_retrans", "nack_count", "buf_max"):
            if key in stat:
                parts.append(f"{key}={stat[key]}")
        lines.append("  " + " ".join(parts))
        # 异常判定
        if stat.get("harq_fail_rate", 0) >= thresholds["harq_fail_rate_crit"]:
            anomalies.append(f"UE {ue} HARQ失败率{stat['harq_fail_rate']:.0%} 严重偏高")
        elif stat.get("harq_fail_rate", 0) >= thresholds["harq_fail_rate_warn"]:
            anomalies.append(f"UE {ue} HARQ失败率{stat['harq_fail_rate']:.0%} 偏高")
        if stat.get("retx_avg", 0) >= thresholds["retx_avg_warn"]:
            anomalies.append(f"UE {ue} 平均重传{stat['retx_avg']}次 偏高")
        if stat.get("sn_gaps", 0) > 0:
            anomalies.append(f"UE {ue} PDCP SN跳变{stat['sn_gaps']}处（疑似丢包）")
        if stat.get("retx_ge2", 0) > 0:
            anomalies.append(f"UE {ue} 重传≥2次 {stat['retx_ge2']}次")
    if anomalies:
        lines.append("  【异常】" + "; ".join(anomalies[:12]))
    else:
        lines.append("  【异常】未检测到超阈值异常")
    return "\n".join(lines)
