# coding=utf-8
"""
LTE 制式 EI 关联分析器（svinsightEILteValidator）

背景
----
svinsight 原始库（BasicModel/maintenanceTool/svinsight）面向 **NR** 制式，其 bler_of_slot /
harqfail_cnt 等方法按 NR 的列名（CRCI.u8IsHarqFail / ACK.u8AckInfo 等）读取数据。
而 T2232 工具导出的 **LTE** EI 详情 CSV 使用的是另一套列名：

  * 下行调度  CMAC_dlUeTtiInfo_Lte_* : ACK.u8Ack / ACK.b8IsHarqFail /
             TB.u8Mcs0 / TB.u32TbSize0 / SCHD_FAIL_RSN.u32UeSchdFailRsn
  * 上行调度  CMAC_ulUeTtiInfo_Lte_* : CRCI.HarqFail / CRCI.CRCTb /
             GRANT.u8Mcs / GRANT.u32TbSize
  * PDCP      PDCP_dl/ulUeTtiInfo_Lte_* : DL_DATA_PDU.u32pdcpCount
  * RLC       RLC_dl/ulUeTtiInfo_Lte_*  : UL_DATA_PDU.u32Vrr / UL_STATUS_PDU.u16vta

直接用 svinsight 原方法会 "Usecols do not match columns" 而退化为数据缺失。
本模块直接按 LTE 列名读取 CSV，计算与 NR 版等价的关联维度，
并复用 svinsightEIValidator 的阈值 / 报告结构，使关键字层无需改动。

所有指标在数据缺失时安全降级为 None（不抛异常），与原有行为一致。
"""

import os
import logging

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# 默认异常判定阈值（与 NR 版对齐，便于统一管理）
# ----------------------------------------------------------------------------
DEFAULT_THRESHOLDS_LTE = {
    # 小区级
    "cell_dl_bler": 0.10,        # 下行 BLER 超 10% 判异常
    "cell_ul_bler": 0.10,        # 上行 BLER 超 10% 判异常
    "cell_dl_harq_fail": 0.05,   # 下行 HARQ 失败率超 5%
    "cell_ul_harq_fail": 0.05,
    "cell_dl_schd_fail": 0.15,   # 下行调度失败比例超 15%
    "cell_ul_schd_fail": 0.15,
    "cell_dl_mcs_min": 5.0,      # 下行平均 MCS 低于该值判异常
    "cell_ul_mcs_min": 5.0,
    # UE 级
    "ue_dl_bler": 0.10,
    "ue_ul_bler": 0.10,
    "ue_dl_harq_fail": 0.05,
    "ue_ul_harq_fail": 0.05,
    "ue_dl_schd_fail": 0.15,
    "ue_ul_schd_fail": 0.15,
    "ue_dl_mcs_min": 5.0,
    "ue_ul_mcs_min": 5.0,
    # PDCP / RLC
    "pdcp_loss_rate": 0.001,      # PDCP COUNT 丢包率阈值 0.1%
    "rlc_ul_delay_ms": 50.0,      # RLC 上行空口时延(p95) 上限 50ms
    "rlc_dl_delay_ms": 50.0,
}


# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------
def _to_float(value, default=None):
    try:
        if value is None:
            return default
        if isinstance(value, (list, tuple)) and len(value) > 0:
            value = value[0]
        if hasattr(value, "iloc"):
            if len(value) == 0:
                return default
            value = value.iloc[0]
        if hasattr(value, "item"):
            return float(value.item())
        return float(value)
    except Exception:  # noqa: BLE001
        return default


def _safe_call(func, *args, **kwargs):
    try:
        return True, func(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        logging.debug("LTE 调用 %s 失败: %s", getattr(func, "__name__", func), e)
        return False, None


def _find_csv(ei_dir, prefix):
    """返回目录下所有含指定前缀的 .csv 文件全路径（前缀做子串匹配）。"""
    if not os.path.isdir(ei_dir):
        return []
    return [os.path.join(ei_dir, f) for f in os.listdir(ei_dir)
            if f.lower().endswith(".csv") and prefix in f]


def _read_csvs(prefix, ei_dir):
    """读取匹配前缀的所有 CSV 并纵向拼接；无文件返回 None。"""
    import pandas as pd
    files = _find_csv(ei_dir, prefix)
    if not files:
        return None
    try:
        dfs = [pd.read_csv(f, encoding="latin-1", low_memory=False) for f in files]
        return pd.concat(dfs, ignore_index=True) if dfs else None
    except Exception as e:  # noqa: BLE001
        logging.debug("读取 %s CSV 失败: %s", prefix, e)
        return None


def _coerce_num(series):
    """把含 '-' / 空串的列安全转成 float，'-' 视作 NaN。"""
    import pandas as pd
    if series is None:
        return None
    s = pd.to_numeric(series, errors="coerce")
    return s


# ----------------------------------------------------------------------------
# 各维度计算（LTE 列名）
# ----------------------------------------------------------------------------
def _bler_from_ack(ack_series, ack_values=(1,)):
    """
    下行 BLER（编码约定已确认：1=ACK 成功，0=NACK，2=DTX，'-'=无调度）。

    BLER = (NACK + DTX) / (ACK + NACK + DTX)，即所有非 '-' 样本中
    未成功确认（NACK/DTX）的占比。无有效样本返回 None。

    ack_values 可配置哪些取值视为成功（默认 (1,) 即 1=ACK）。
    """
    if ack_series is None:
        return None
    s = ack_series.replace("-", None).dropna().astype(int)
    if s.empty:
        return None
    vc = s.value_counts()
    total = int(vc.sum())
    if total == 0:
        return None
    success = int(sum(vc.get(v, 0) for v in ack_values))
    fail = total - success
    return fail / float(total)


def _ack_distribution(ack_series):
    """返回 ACK.u8Ack 的 (ACK=1, NACK=0, DTX=2) 计数串，用于审计。"""
    if ack_series is None:
        return ""
    s = ack_series.replace("-", None).dropna().astype(int)
    if s.empty:
        return ""
    vc = s.value_counts()
    # 编码约定：1=ACK，0=NACK，2=DTX
    return "ACK(1)=%d,NACK(0)=%d,DTX(2)=%d" % (
        int(vc.get(1, 0)), int(vc.get(0, 0)), int(vc.get(2, 0)))


def _harq_fail_rate_from_ack_flag(flag_series):
    """下行 HARQ 失败率：ACK.b8IsHarqFail 非 0 的比例（仅统计有值样本）。"""
    if flag_series is None:
        return None
    s = flag_series.replace("-", None).dropna().astype(int)
    if s.empty:
        return None
    fails = int((s != 0).sum())
    return fails / float(len(s))


def _harq_fail_rate_from_crci(crc_tb_series, harq_fail_series):
    """
    上行 HARQ 失败率：CRCI.CRCTb=1 表示有 CRC 校验，
    CRCI.HarqFail!=0 表示失败。失败率 = HarqFail / CRCTb。
    """
    if crc_tb_series is None or harq_fail_series is None:
        return None
    crc = crc_tb_series.replace("-", None).dropna().astype(int)
    fail = harq_fail_series.replace("-", None).dropna().astype(int)
    # 仅在有 CRC 的样本上统计
    valid = min(len(crc), len(fail))
    if valid == 0:
        return None
    crc_v = crc.iloc[:valid]
    fail_v = fail.iloc[:valid]
    total_crc = int((crc_v == 1).sum())
    if total_crc == 0:
        return None
    total_fail = int(((crc_v == 1) & (fail_v != 0)).sum())
    return total_fail / float(total_crc)


def _sched_fail_rate(rsn_series):
    """调度失败比例：SCHD_FAIL_RSN.* != 0（且非 '-'）的行占比。"""
    if rsn_series is None:
        return None
    s = rsn_series.replace("-", None).dropna().astype(int)
    if s.empty:
        return None
    fails = int((s != 0).sum())
    return fails / float(len(s))


def _mean_tbs(tbs_series):
    """平均 TB 大小（字节）。"""
    s = _coerce_num(tbs_series)
    if s is None or s.dropna().empty:
        return None
    return float(s.dropna().mean())


# ----------------------------------------------------------------------------
# 小区 / UE 级分析
# ----------------------------------------------------------------------------
def _analyze_direction(df, direction, th, events, id_label, id_val,
                       bler_key, harq_key, schd_key, mcs_key, is_dl):
    """对单个方向（下行/上行）做 LTE 关联检查，返回 (checks, anomalies)。"""
    checks, anomalies = [], []

    def add(name, value, abnormal, detail=""):
        checks.append({"name": "%s%s" % (direction, name),
                       "value": value, "abnormal": abnormal})
        if abnormal and detail:
            msg = "%s %s [%s%s] %s" % (id_label, id_val, direction, name, detail)
            anomalies.append(msg)
            events.append(msg)

    if df is None or df.empty:
        add("BLER", "数据缺失/降级", False)
        add("HARQ失败", "数据缺失/降级", False)
        add("调度失败", "数据缺失/降级", False)
        add("MCS", "数据缺失/降级", False)
        add("吞吐", "数据缺失/降级", False)
        return checks, anomalies

    # ---- BLER ----
    if is_dl:
        bler_info = _bler_from_ack(df.get("ACK.u8Ack"))
        ack_dist = _ack_distribution(df.get("ACK.u8Ack"))
    else:
        # 上行用 CRCI 计算（单一明确语义）
        bler_info = _harq_fail_rate_from_crci(df.get("CRCI.CRCTb"),
                                              df.get("CRCI.HarqFail"))
        ack_dist = ""
    if bler_info is None:
        add("BLER", "数据缺失/降级", False)
    else:
        ab = bler_info > th[bler_key]
        note = (" (ACK分布:%s; 1=ACK)" % ack_dist) if ack_dist else " (1=ACK)"
        add("BLER", round(bler_info, 6), ab,
            "BLER=%.4f > %.4f%s" % (bler_info, th[bler_key], note))

    # ---- HARQ 失败 ----
    if is_dl:
        harq = _harq_fail_rate_from_ack_flag(df.get("ACK.b8IsHarqFail"))
    else:
        harq = _harq_fail_rate_from_crci(df.get("CRCI.CRCTb"),
                                         df.get("CRCI.HarqFail"))
    if harq is None:
        add("HARQ失败", "数据缺失/降级", False)
    else:
        ab = harq > th[harq_key]
        add("HARQ失败", round(harq, 6), ab,
            "HARQ失败率=%.4f > %.4f" % (harq, th[harq_key]))

    # ---- 调度失败 ----
    rsn_col = "SCHD_FAIL_RSN.u32UeSchdFailRsn"
    schd = _sched_fail_rate(df.get(rsn_col)) if rsn_col in df else None
    if schd is None:
        add("调度失败", "无", False)
    else:
        ab = schd > th[schd_key]
        add("调度失败", round(schd, 6), ab,
            "调度失败比例=%.4f > %.4f" % (schd, th[schd_key]))

    # ---- MCS ----
    mcs_col = "TB.u8Mcs0" if is_dl else "GRANT.u8Mcs"
    mcs = _to_float(_coerce_num(df[mcs_col]).dropna().mean()) if mcs_col in df else None
    if mcs is None:
        add("MCS", "数据缺失/降级", False)
    else:
        ab = mcs < th[mcs_key]
        add("MCS", round(mcs, 4), ab,
            "MCS=%.2f < %.2f" % (mcs, th[mcs_key]))

    # ---- 吞吐（平均 TB 大小，字节；仅展示）----
    tbs_col = "TB.u32TbSize0" if is_dl else "GRANT.u32TbSize"
    tbs = _mean_tbs(df[tbs_col]) if tbs_col in df else None
    if tbs is None:
        add("吞吐", "数据缺失/降级", False)
    else:
        add("吞吐", round(tbs, 2), False)

    return checks, anomalies


def _analyze_cell_lte(dl_df, ul_df, cid, th, events):
    report = {"cellid": cid, "checks": [], "anomalies": []}
    if dl_df is not None and not dl_df.empty:
        c, a = _analyze_direction(
            dl_df, "下行", th, events, "小区", cid,
            "cell_dl_bler", "cell_dl_harq_fail", "cell_dl_schd_fail",
            "cell_dl_mcs_min", is_dl=True)
        report["checks"] += c
        report["anomalies"] += a
    if ul_df is not None and not ul_df.empty:
        c, a = _analyze_direction(
            ul_df, "上行", th, events, "小区", cid,
            "cell_ul_bler", "cell_ul_harq_fail", "cell_ul_schd_fail",
            "cell_ul_mcs_min", is_dl=False)
        report["checks"] += c
        report["anomalies"] += a
    return report


def _analyze_ue_lte(dl_df, ul_df, uid, th, events):
    report = {"uegid": uid, "checks": [], "anomalies": []}
    if dl_df is not None and not dl_df.empty:
        c, a = _analyze_direction(
            dl_df, "下行", th, events, "UE", uid,
            "ue_dl_bler", "ue_dl_harq_fail", "ue_dl_schd_fail",
            "ue_dl_mcs_min", is_dl=True)
        report["checks"] += c
        report["anomalies"] += a
    if ul_df is not None and not ul_df.empty:
        c, a = _analyze_direction(
            ul_df, "上行", th, events, "UE", uid,
            "ue_ul_bler", "ue_ul_harq_fail", "ue_ul_schd_fail",
            "ue_ul_mcs_min", is_dl=False)
        report["checks"] += c
        report["anomalies"] += a
    return report


# ----------------------------------------------------------------------------
# PDCP 丢包分析（基于 DL_DATA_PDU.u32pdcpCount 连续性）
# ----------------------------------------------------------------------------
def _analyze_pdcp_lte(ei_dir, th, events):
    out = {"available": False, "dl": {}, "ul": {}, "anomalies": []}
    import pandas as pd

    dl = _read_csvs("PDCP_dlUeTtiInfo_Lte", ei_dir)
    if dl is not None and "DL_DATA_PDU.u32pdcpCount" in dl.columns:
        # 按 UE/rbid 分组检查 COUNT 连续性（允许重传造成的重复，但不允许跳变缺失）
        grp_cols = [c for c in ("UEGID", "DL_DATA_PDU.u8rbid")
                    if c in dl.columns]
        cnt = "DL_DATA_PDU.u32pdcpCount"
        try:
            dl[cnt] = pd.to_numeric(dl[cnt], errors="coerce")
            loss_rate, total, missing = _pdcp_loss_rate(dl, cnt, grp_cols)
            if total > 0:
                out["available"] = True
                out["dl"] = {"loss_rate": loss_rate, "packets": total,
                             "missing": missing}
                if loss_rate > th["pdcp_loss_rate"]:
                    msg = "PDCP 下行丢包率=%.4f%% > %.4f%%" % (
                        loss_rate * 100, th["pdcp_loss_rate"] * 100)
                    out["anomalies"].append(msg)
                    events.append(msg)
        except Exception as e:  # noqa: BLE001
            logging.debug("PDCP DL 分析失败: %s", e)

    ul = _read_csvs("PDCP_ulUeTtiInfo_Lte", ei_dir)
    if ul is not None and "UL_DATA_PDU.u32pdcpCount" in ul.columns:
        cnt = "UL_DATA_PDU.u32pdcpCount"
        grp_cols = [c for c in ("UEGID", "UL_DATA_PDU.u8rbid")
                    if c in ul.columns]
        try:
            ul[cnt] = pd.to_numeric(ul[cnt], errors="coerce")
            loss_rate, total, missing = _pdcp_loss_rate(ul, cnt, grp_cols)
            if total > 0:
                out["available"] = True
                out["ul"] = {"loss_rate": loss_rate, "packets": total,
                             "missing": missing}
                if loss_rate > th["pdcp_loss_rate"]:
                    msg = "PDCP 上行丢包率=%.4f%% > %.4f%%" % (
                        loss_rate * 100, th["pdcp_loss_rate"] * 100)
                    out["anomalies"].append(msg)
                    events.append(msg)
        except Exception as e:  # noqa: BLE001
            logging.debug("PDCP UL 分析失败: %s", e)

    if not out["available"]:
        out["note"] = "无 PDCP 数据"
    return out


def _pdcp_loss_rate(df, cnt_col, grp_cols):
    """按组检查 pdcpCount 是否连续（步长 1），统计缺失数。
    重传会产生重复 COUNT，故去重后检查单调递增跳变。"""
    import numpy as np
    total = 0
    missing = 0
    if grp_cols:
        groups = df.groupby(grp_cols)
    else:
        groups = [(None, df)]
    for _, g in groups:
        s = g[cnt_col].dropna().astype(np.int64).drop_duplicates().sort_values()
        if len(s) < 2:
            continue
        diffs = s.diff().dropna()
        # 正常步长为 1；>1 表示中间丢包
        gaps = diffs[diffs > 1]
        missing += int((gaps - 1).sum())
        total += int(len(s))
    loss_rate = (missing / float(total)) if total > 0 else 0.0
    return loss_rate, total, missing


# ----------------------------------------------------------------------------
# RLC 时延分析（上行：UL_DATA_PDU.u32Vrr -> UL_STATUS_PDU.u16vta 窗口）
# ----------------------------------------------------------------------------
def _analyze_rlc_lte(ei_dir, th, events):
    """RLC 时延分析（LTE）。

    说明：LTE RLC 上行 CSV 中 UL_STATUS_PDU.u16vta / UL_DATA_PDU.u32Vrr
    是 RLC 序号(SN)/窗口值，并非以毫秒为单位的时延，因此这里只做
    p95 统计与展示，不套用 ms 阈值（避免单位错位误报）。如需按 SN
    跳变评估，可在此基础上扩展。
    """
    out = {"available": False, "ul": {}, "anomalies": []}
    ul = _read_csvs("RLC_ulUeTtiInfo_Lte", ei_dir)
    if ul is not None and "UL_STATUS_PDU.u16vta" in ul.columns:
        try:
            import pandas as pd
            vta = pd.to_numeric(ul["UL_STATUS_PDU.u16vta"], errors="coerce").dropna()
            vrr = pd.to_numeric(ul.get("UL_DATA_PDU.u32Vrr"), errors="coerce").dropna()
            if not vta.empty:
                p95 = float(vta.quantile(0.95))
                out["available"] = True
                out["ul"]["vta_p95"] = p95
                out["ul"]["vta_max"] = float(vta.max())
                out["ul"]["vrr_max"] = float(vrr.max()) if not vrr.empty else None
                # 仅展示，不套用 ms 阈值
        except Exception as e:  # noqa: BLE001
            logging.debug("RLC 分析失败: %s", e)
    if not out["available"]:
        out["note"] = "无 RLC 时延数据"
    return out


# ----------------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------------
def analyze_svinsight_ei_lte(ei_dir, thresholds=None, cellids=None,
                             uegids=None, time_interval=None):
    """分析 LTE 制式 EI 详情目录，返回与 NR 版等价的报告字典。"""
    import pandas as pd

    th = dict(DEFAULT_THRESHOLDS_LTE)
    if thresholds:
        th.update(thresholds)

    dl_all = _read_csvs("CMAC_dlUeTtiInfo_Lte", ei_dir)
    ul_all = _read_csvs("CMAC_ulUeTtiInfo_Lte", ei_dir)

    all_cellids = sorted(set(pd.to_numeric(dl_all["CellId"], errors="coerce").dropna().astype(int).tolist()
                             if dl_all is not None and "CellId" in dl_all else []))
    if ul_all is not None and "CellId" in ul_all:
        all_cellids += pd.to_numeric(ul_all["CellId"], errors="coerce").dropna().astype(int).tolist()
    all_cellids = sorted(set(all_cellids))

    all_uegids = sorted(set(pd.to_numeric(dl_all["UEGID"], errors="coerce").dropna().astype(int).tolist()
                            if dl_all is not None and "UEGID" in dl_all else []))
    if ul_all is not None and "UEGID" in ul_all:
        all_uegids += pd.to_numeric(ul_all["UEGID"], errors="coerce").dropna().astype(int).tolist()
    all_uegids = sorted(set(all_uegids))

    target_cells = [c for c in all_cellids if (cellids is None or c in cellids)]
    target_ues = [u for u in all_uegids if (uegids is None or u in uegids)]

    events = []
    cells_out, ues_out = [], []

    def _slice(df, **filters):
        if df is None:
            return None
        sub = df
        for col, val in filters.items():
            if col in sub.columns:
                sub = sub[pd.to_numeric(sub[col], errors="coerce") == val]
        return sub if not sub.empty else None

    for cid in target_cells:
        dl = _slice(dl_all, CellId=cid)
        ul = _slice(ul_all, CellId=cid)
        if dl is None and ul is None:
            events.append("小区 %s 无调度数据" % cid)
            continue
        cells_out.append(_analyze_cell_lte(dl, ul, cid, th, events))

    for uid in target_ues:
        dl = _slice(dl_all, UEGID=uid)
        ul = _slice(ul_all, UEGID=uid)
        if dl is None and ul is None:
            events.append("UE %s 无调度数据" % uid)
            continue
        ues_out.append(_analyze_ue_lte(dl, ul, uid, th, events))

    pdcp_out = _analyze_pdcp_lte(ei_dir, th, events)
    tcp_out = {"available": False, "note": "LTE 样本暂无 TCP 数据"}
    rlc_out = _analyze_rlc_lte(ei_dir, th, events)

    has_anomaly = len(events) > 0
    summary = ("检测到 %d 项异常" % len(events)) if has_anomaly else "未检测到异常"

    return {
        "dir": ei_dir,
        "thresholds": th,
        "cells": cells_out,
        "ues": ues_out,
        "pdcp": pdcp_out,
        "tcp": tcp_out,
        "rlc": rlc_out,
        "has_anomaly": has_anomaly,
        "events": events,
        "summary": summary,
        "cell_count": len(target_cells),
        "ue_count": len(target_ues),
        "lte": True,
    }


# ----------------------------------------------------------------------------
# 自动识别：LTE 还是 NR
# ----------------------------------------------------------------------------
def is_lte_directory(ei_dir):
    """目录含 *_Lte_*.csv / *_Lte_*.ei 即判定为 LTE 制式。"""
    if not os.path.isdir(ei_dir):
        return False
    for f in os.listdir(ei_dir):
        if "_Lte_" in f and (f.lower().endswith(".csv") or f.lower().endswith(".ei")):
            return True
    return False


def analyze_svinsight_ei_auto(ei_dir, thresholds=None, cellids=None,
                              uegids=None, time_interval=None):
    """
    自动识别制式并分发到对应分析器。
    返回 (report, is_lte)。优先用 NR 版 svinsightEIValidator，
    若目录被识别为 LTE 则走 LTE 专用分析器。
    """
    if is_lte_directory(ei_dir):
        return analyze_svinsight_ei_lte(
            ei_dir, thresholds=thresholds, cellids=cellids,
            uegids=uegids, time_interval=time_interval), True
    # NR：复用原模块
    from . import sv_ei_nr as nr_mod
    return nr_mod.analyze_svinsight_ei(
        ei_dir, thresholds=thresholds, cellids=cellids,
        uegids=uegids, time_interval=time_interval), False
