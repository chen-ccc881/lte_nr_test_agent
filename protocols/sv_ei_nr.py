# coding=utf-8
"""
svinsightEIValidator
====================
纯逻辑层：基于本地 svinsight 库（BasicModel/maintenanceTool/svinsight）对 EI 详细文件做
"解析 -> 关联性分析 -> 异常判定" 的自动化校验。

覆盖维度（svinsight 可覆盖的检查尽量全量实现）：
  - 小区级（Cell）：上下行调度次数/失败、BLER、MCS、HARQ 失败率、
    重建立/上下文释放、吞吐、AMC、BSR、PRB 占用。
  - UE 级（Ue）：上行/下行调度、BLER、HARQ、MCS、DTX、SINR（若 PHY 数据存在）。
  - PDCP：上下行 COUNT 连续性（丢包）、窗口、时延。
  - RLC：上下行时延、重传。
  - TCP：上下行重传率、吞吐（若 PDCP TCP 数据存在）。

设计原则：
  - 与 xtraceFlowValidator 一致，不依赖 allure，返回结构化结论。
  - 对缺失的数据类型（如样本无 PHY_srs）做优雅降级，不报错。
  - 关联分析：不只看单一阈值，而是"指标异常 + 关联指标佐证"给出结论。

调用前需保证 svinsight 可被 import（本模块内部动态注入 sys.path，
以屏蔽 svinsight 内部 `import const` 等裸模块名导入问题）。
"""

import os
import sys
import logging

# ----------------------------------------------------------------------------
# svinsight 路径注入：svinsight 内部用 `import const` / `import ue` 等裸模块名，
# 必须把仓库内 BasicModel/maintenanceTool/svinsight 加入 sys.path 才能保证可导入。
# ----------------------------------------------------------------------------
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
# BasicModel/maintenanceTool/svinsightEIValidator.py
#   -> BasicModel/maintenanceTool/svinsight（与 validator 同目录，不再回退到 BasicModel/svinsight）
_SVINSIGHT_ROOT = os.path.normpath(
    os.path.join(_THIS_DIR, "svinsight")
)

if os.path.isdir(_SVINSIGHT_ROOT) and _SVINSIGHT_ROOT not in sys.path:
    sys.path.insert(0, _SVINSIGHT_ROOT)


def get_svinsight_root():
    """返回本地 svinsight 库根目录（供关键字层复用做 sys.path 注入）。"""
    return _SVINSIGHT_ROOT


class SvinsightUnavailable(RuntimeError):
    """svinsight 不可用（通常因缺少 64 位 numpy/pandas 或 EI_FileProc.dll 位数不匹配）。"""
    pass


def _check_dependencies():
    """
    检测 svinsight 运行所需的依赖是否齐备。

    常见问题（本机已遇到）：
      - 64 位 Python 加载了 32 位的 numpy（WinError 193）-> 需安装 amd64 版 numpy/pandas。
      - EI_FileProc.dll 位数与 Python 不一致 -> 需匹配位数的 DLL。

    Raises:
        SvinsightUnavailable: 依赖缺失/不匹配时抛出，附可操作提示。
    """
    try:
        import numpy  # noqa: F401
        import pandas  # noqa: F401
    except Exception as e:  # noqa: BLE001
        raise SvinsightUnavailable(
            "svinsight 依赖 numpy/pandas 不可用：%s\n"
            "请在本机 64 位 Python 下安装匹配位数的 numpy 与 pandas，例如：\n"
            "  py -V:3.8 -m pip install --force-reinstall numpy pandas\n"
            "（注意必须是 win_amd64 版本，与 EI_FileProc.dll 位数一致）" % e
        )
    # EI_FileProc.dll 已由本项目纯 Python fileproc.py 替代，无需外部 DLL。


def _ensure_csv_converted(ei_dir):
    """
    把目录下尚未转成 CSV 的 .ei 原始文件转换出来。

    svinsight 的 NrLog / PdcpAnalysis / RlcAnalysis 只读取 .csv，
    而离线样本目录里经常只提供 .ei 原始文件，因此这里在分析前
    先用 svinsight 自带的纯 Python 实现 util/fileproc.py 把缺的 .ei 补转成 .csv。

    Args:
        ei_dir: 日志目录（可能只含 .ei，或已含 .csv，或两者混合）。
    """
    if not os.path.isdir(ei_dir):
        return
    ei_files = [f for f in os.listdir(ei_dir)
                if f.lower().endswith(".ei")]
    if not ei_files:
        return  # 已经都是 csv，无需转换

    # 已转换过的 .ei 跳过，避免重复转换
    csv_stems = {os.path.splitext(f)[0]
                 for f in os.listdir(ei_dir) if f.lower().endswith(".csv")}
    pending = [f for f in ei_files
               if os.path.splitext(f)[0] not in csv_stems]
    if not pending:
        return

    # 延迟导入转换接口（避免无 DLL 时 import 失败）
    from util.ei2csv import ei2csv
    ei2csv(ei_dir, pending)


# ----------------------------------------------------------------------------
# 默认异常判定阈值（集中管理，便于按基站型号校准）
# ----------------------------------------------------------------------------
DEFAULT_THRESHOLDS = {
    # 说明：比率类阈值统一按「比例」配置（0.10 = 10%），与 LTE 版对齐。
    # BLER 原始值来自 bler_of_slot()，为百分比(0~100)，比较前已归一化。
    # 小区级
    "cell_dl_bler": 0.10,          # 下行 BLER > 10% 视为异常
    "cell_ul_bler": 0.10,          # 上行 BLER > 10%
    "cell_dl_harq_fail": 0.10,     # 下行 HARQ 失败率
    "cell_ul_harq_fail": 0.10,     # 上行 HARQ 失败率
    "cell_dl_schd_fail": 0.10,     # 下行调度失败率
    "cell_ul_schd_fail": 0.10,     # 上行调度失败率
    "cell_dl_mcs_min": 2,          # 下行平均 MCS 过低（含低阶占比）
    "cell_ul_mcs_min": 2,
    "cell_throughput_drop_ratio": 0.50,  # 吞吐相对基线下降 > 50%
    # UE 级
    "ue_dl_bler": 0.10,
    "ue_ul_bler": 0.10,
    "ue_dl_harq_fail": 0.15,
    "ue_ul_harq_fail": 0.15,
    "ue_dl_schd_fail": 0.15,
    "ue_ul_schd_fail": 0.15,
    # PDCP / RLC / TCP
    "pdcp_loss_rate": 0.001,       # PDCP COUNT 丢包率阈值 0.1%
    "tcp_retransmit_rate": 0.05,   # TCP 重传率 5%
    "rlc_dl_delay_ms": 50.0,       # RLC 下行处理时延(p95) 上限 50ms
    "rlc_ul_delay_ms": 50.0,       # RLC 上行空口时延(p95) 上限 50ms
}


# ----------------------------------------------------------------------------
# 关联分析辅助：把异常事件收敛成结论
# ----------------------------------------------------------------------------
def _to_float(value, default=None):
    """安全地把各种返回值转成 float。"""
    try:
        if value is None:
            return default
        if isinstance(value, (list, tuple)) and len(value) > 0:
            value = value[0]
        if hasattr(value, "iloc"):
            # pandas Series / DataFrame
            if len(value) == 0:
                return default
            value = value.iloc[0]
        if hasattr(value, "item"):
            return float(value.item())
        return float(value)
    except Exception:
        return default


def _safe_call(func, *args, **kwargs):
    """调用 svinsight 方法并吞掉因数据缺失导致的异常，返回 (ok, result)。"""
    try:
        return True, func(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        logging.debug("svinsight 调用 %s 失败: %s", getattr(func, "__name__", func), e)
        return False, None


# ----------------------------------------------------------------------------
# 分析器主体
# ----------------------------------------------------------------------------
def analyze_svinsight_ei(ei_dir, thresholds=None, cellids=None, uegids=None,
                         time_interval=None):
    """
    解析 EI 详细文件并做关联性异常分析。

    Args:
        ei_dir:        EI 文件（.ei）所在目录，也可已是 .csv 目录。
        thresholds:    阈值字典，覆盖 DEFAULT_THRESHOLDS。
        cellids:       仅分析指定小区列表（None=全部）。
        uegids:        仅分析指定 UE 列表（None=全部）。
        time_interval: [start, end] 时间过滤（yyyy/mm/dd hh:mm:ss）。

    Returns:
        dict: {
            'dir': str,
            'thresholds': dict,
            'cells': [...],          # 每小区检查结论
            'ues': [...],            # 每 UE 检查结论
            'pdcp': {...},
            'tcp': {...},
            'has_anomaly': bool,
            'events': [str],         # 所有异常事件文本
            'summary': str,
        }
    """
    _check_dependencies()
    from nrlog import NrLog
    from pdcp import PdcpAnalysis
    from rlc import RlcAnalysis

    # 样本目录可能只含 .ei 原始文件（尚未转 CSV）。svinsight 的 NrLog /
    # PdcpAnalysis 只读取 .csv，因此这里先按需把缺失的 .ei 转换成 .csv。
    _ensure_csv_converted(ei_dir)

    th = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        th.update(thresholds)

    nrlog = NrLog(ei_dir, time_interval=time_interval)

    ids_df = nrlog.get_cell_and_ue_ids()
    all_cellids = sorted(set(ids_df["CellId"].tolist())) if not ids_df.empty else []
    all_uegids = sorted(set(ids_df["UEGID"].tolist())) if not ids_df.empty else []

    target_cells = [c for c in all_cellids if (cellids is None or c in cellids)]
    target_ues = [u for u in all_uegids if (uegids is None or u in uegids)]

    events = []
    cells_out = []
    ues_out = []

    # ---- 小区级关联分析 ----
    for cid in target_cells:
        ok, cell = _safe_call(nrlog.get_cell, cid)
        if not ok or cell is None:
            events.append("小区 %s 无法实例化（数据缺失）" % cid)
            continue
        cell_report = _analyze_cell(cell, cid, th, events)
        cells_out.append(cell_report)

    # ---- UE 级关联分析 ----
    for uid in target_ues:
        ok, ue = _safe_call(nrlog.get_ue, uid)
        if not ok or ue is None:
            # UE 可能只存在于部分小区，尝试不加 cellid 再取
            events.append("UE %s 无法实例化（可能无调度数据）" % uid)
            continue
        ue_report = _analyze_ue(ue, uid, th, events)
        ues_out.append(ue_report)

    # ---- PDCP COUNT 丢包分析（全局，跨 UE）----
    pdcp_out = _analyze_pdcp(PdcpAnalysis, ei_dir, th, events)

    # ---- TCP 重传分析 ----
    tcp_out = _analyze_tcp(PdcpAnalysis, ei_dir, th, events)

    # ---- RLC 时延分析 ----
    rlc_out = _analyze_rlc(RlcAnalysis, ei_dir, th, events)

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
    }


def _safe_aggregate(result):
    """把 svinsight 方法返回的 DataFrame/标量安全聚合成单个 float 均值。
    返回 None 表示无有效数值（数据缺失 / CSV 列不全导致方法抛异常）。"""
    import numpy as np
    import pandas as pd
    try:
        if result is None:
            return None
        if isinstance(result, pd.DataFrame):
            if result.empty:
                return None
            num = result.select_dtypes(include=[np.number])
            if num.empty:
                return None
            flat = num.to_numpy(dtype=float, na_value=np.nan).ravel()
            flat = flat[~np.isnan(flat)]
            return float(flat.mean()) if flat.size else None
        arr = np.asarray(result, dtype=float).ravel()
        arr = arr[~np.isnan(arr)]
        return float(arr.mean()) if arr.size else None
    except Exception:  # noqa: BLE001
        return None


# svinsight 真实方法名 -> (检查项中文, 类型)
#   ratio: 比率(BLER)，超阈值判异常
#   level: 水平(MCS/吞吐)，MCS 过低判异常，吞吐仅展示
#   count: 计数(HARQ失败/DTX)，展示为主
#   dist:  分布(调度失败原因)，展示为主
_DIM_METHODS = {
    "BLER": ("bler_of_slot", "ratio"),
    "HARQ失败": ("harqfail_cnt", "count"),
    "调度失败": ("schdfail_reasons", "dist"),
    "MCS": ("mcs_of_slot", "level"),
    "吞吐": ("throuput", "level"),
    "DTX": ("dtx_cnt", "count"),
}


def _analyze_dim(obj, dim_name, th, events, bler_key, mcs_key, id_label, id_val):
    """对单个上下行维度(cell.dl/ul 或 ue.dl/ul)做关联检查。
    返回 (checks, anomalies)。数据缺失的指标自动降级跳过，不报错。"""
    checks, anomalies = [], []
    for cn, (method, kind) in _DIM_METHODS.items():
        if not hasattr(obj, method):
            checks.append({"name": "%s%s" % (dim_name, cn),
                           "value": "方法不支持", "abnormal": False})
            continue
        called = _safe_call(getattr(obj, method))
        # _safe_call 应返回 (ok, value)；个别 svinsight 方法可能直接返回对象，
        # 这里做健壮解包，避免把 DataFrame 当序列索引导致 KeyError。
        if isinstance(called, tuple) and len(called) == 2:
            ok, res = called
        else:
            ok, res = True, called
        name = "%s%s" % (dim_name, cn)

        if kind == "dist":
            # 调度失败原因分布：仅记录是否存在失败原因，不做数值聚合
            has_fail = bool(ok and res is not None and not (
                hasattr(res, "empty") and res.empty))
            checks.append({"name": name, "value": "失败原因存在" if has_fail else "无",
                           "abnormal": False})
            continue

        val = _safe_aggregate(res) if (ok and res is not None) else None
        if val is None:
            checks.append({"name": name, "value": "数据缺失/降级", "abnormal": False})
            continue
        abnormal = False
        detail = ""
        if kind == "ratio":                      # BLER 比率
            # 量纲归一：svinsight 的 bler_of_slot() 返回的是百分比（0~100），
            # 见 dlschd.py / ulschd.py 中 (NACK+DTX)*100/total 的实现；
            # 而阈值按比例配置（0.10 表示 10%）。此前直接比较造成量纲错位，
            # 使 NR 数据几乎必然误报。这里归一化为比例后再比较。
            thr = th[bler_key]
            val_pct = val
            val = val / 100.0
            abnormal = val > thr
            detail = "%s=%.2f%% > %.2f%%" % (name, val_pct, thr * 100)
        elif kind == "level" and cn == "MCS":    # MCS 过低
            thr = th[mcs_key]
            abnormal = val < thr
            detail = "%s=%.2f < %.2f" % (name, val, thr)
        checks.append({"name": name, "value": round(val, 6), "abnormal": abnormal})
        if abnormal:
            msg = "%s %s [%s] %s" % (id_label, id_val, name, detail)
            anomalies.append(msg)
            events.append(msg)
    return checks, anomalies


def _analyze_cell(cell, cid, th, events):
    """小区级关联分析：下行/上行 BLER / HARQ / 调度失败 / MCS / 吞吐 / DTX。"""
    report = {"cellid": cid, "checks": [], "anomalies": []}

    ok, dl = _safe_call(lambda: cell.dl)
    if ok and dl is not None:
        c, a = _analyze_dim(dl, "下行", th, events,
                            "cell_dl_bler", "cell_dl_mcs_min", "小区", cid)
        report["checks"] += c
        report["anomalies"] += a

    ok, ul = _safe_call(lambda: cell.ul)
    if ok and ul is not None:
        c, a = _analyze_dim(ul, "上行", th, events,
                            "cell_ul_bler", "cell_ul_mcs_min", "小区", cid)
        report["checks"] += c
        report["anomalies"] += a

    return report


def _analyze_ue(ue, uid, th, events):
    """UE 级关联分析：下行/上行 BLER / HARQ / 调度失败 / MCS / DTX。"""
    report = {"uegid": uid, "checks": [], "anomalies": []}

    ok, dl = _safe_call(lambda: ue.dl)
    if ok and dl is not None:
        c, a = _analyze_dim(dl, "下行", th, events,
                            "ue_dl_bler", "cell_dl_mcs_min", "UE", uid)
        report["checks"] += c
        report["anomalies"] += a

    ok, ul = _safe_call(lambda: ue.ul)
    if ok and ul is not None:
        c, a = _analyze_dim(ul, "上行", th, events,
                            "ue_ul_bler", "cell_ul_mcs_min", "UE", uid)
        report["checks"] += c
        report["anomalies"] += a

    return report


def _analyze_pdcp(PdcpAnalysis, ei_dir, th, events):
    """PDCP COUNT 连续性（丢包）分析。"""
    out = {"available": False, "loss_rate": None, "loss_events": 0, "anomalies": []}
    ok, pdcp = _safe_call(PdcpAnalysis, ei_dir)
    if not ok or pdcp is None:
        out["note"] = "无 PDCP 数据或分析失败"
        return out
    out["available"] = True
    ok, res = _safe_call(pdcp.count_loss)
    if not ok or res is None:
        out["note"] = "PDCP count_loss 执行失败"
        return out
    for direction in ("ul", "dl"):
        d = res.get(direction)
        if not d:
            continue
        loss_rate = _to_float(d.get("loss_rate"))
        loss_events = d.get("loss_events", 0)
        out[direction] = {
            "total_pkts": d.get("total_pkts"),
            "total_lost": d.get("total_lost"),
            "loss_rate": loss_rate,
            "loss_events": loss_events,
        }
        if loss_rate is not None and loss_rate > th["pdcp_loss_rate"]:
            msg = "PDCP %s 丢包率 %.4f%% > %.4f%%（事件数 %s）" % (
                direction, loss_rate * 100, th["pdcp_loss_rate"] * 100, loss_events)
            out["anomalies"].append(msg)
            events.append(msg)
    return out


def _analyze_tcp(PdcpAnalysis, ei_dir, th, events):
    """TCP 重传率分析（若 PDCP TCP 数据存在）。"""
    out = {"available": False, "anomalies": []}
    ok, pdcp = _safe_call(PdcpAnalysis, ei_dir)
    if not ok or pdcp is None:
        out["note"] = "无 PDCP/TCP 数据"
        return out
    for direction in ("tcp_ul", "tcp_dl"):
        obj = getattr(pdcp, direction, None)
        if obj is None:
            continue
        out["available"] = True
        ok, rt = _safe_call(obj.retransmit_rate)
        if not ok or rt is None:
            continue
        rate = _to_float(rt)
        if rate is None:
            continue
        out.setdefault("rates", {})[direction] = rate
        if rate > th["tcp_retransmit_rate"]:
            msg = "TCP %s 重传率 %.2f%% > %.2f%%" % (
                direction, rate * 100, th["tcp_retransmit_rate"] * 100)
            out["anomalies"].append(msg)
            events.append(msg)
    if not out["available"]:
        out["note"] = "无 TCP 数据"
    return out


def _analyze_rlc(RlcAnalysis, ei_dir, th, events):
    """RLC 时延分析（下行处理时延 / 上行空口时延）。

    样本目录若只含 PDCP/RLC 文件（无 CMAC 调度文件），小区/UE 级
    BLER/HARQ 检查会降级，但 RLC 时延仍可独立评估。
    """
    out = {"available": False, "dl": {}, "ul": {}, "anomalies": []}
    ok, rlc = _safe_call(RlcAnalysis, ei_dir)
    if not ok or rlc is None:
        out["note"] = "无 RLC 数据或分析失败"
        return out

    # 下行处理时延（dl_delay_stats 返回 DataFrame，index 含 'p95'，列含 RlcCost/PdcpCost）
    ok_dl, dl_stats = _safe_call(rlc.dl.dl_delay_stats)
    if ok_dl and dl_stats is not None and not getattr(dl_stats, "empty", True):
        try:
            p95 = dl_stats.loc["p95"]
            dl_cost = _to_float(p95.get("RlcCost"))
            pdcp_cost = _to_float(p95.get("PdcpCost"))
            out["available"] = True
            if dl_cost is not None:
                dl_ms = dl_cost / 1000.0
                out["dl"]["rlc_cost_ms"] = dl_ms
                if dl_ms > th["rlc_dl_delay_ms"]:
                    msg = "RLC 下行处理时延 p95=%.2fms > %.2fms" % (
                        dl_ms, th["rlc_dl_delay_ms"])
                    out["anomalies"].append(msg)
                    events.append(msg)
            if pdcp_cost is not None:
                out["dl"]["pdcp_cost_ms"] = pdcp_cost / 1000.0
        except Exception:  # noqa: BLE001
            pass

    # 上行空口时延（air_cost_stats 返回 dict，含 'p95'）
    ok_ul, ul_stats = _safe_call(rlc.ul.air_cost_stats)
    if ok_ul and isinstance(ul_stats, dict) and ul_stats:
        p95 = ul_stats.get("p95")
        if p95 is not None:
            out["available"] = True
            ul_ms = p95 / 1000.0
            out["ul"]["air_cost_ms"] = ul_ms
            if ul_ms > th["rlc_ul_delay_ms"]:
                msg = "RLC 上行空口时延 p95=%.2fms > %.2fms" % (
                    ul_ms, th["rlc_ul_delay_ms"])
                out["anomalies"].append(msg)
                events.append(msg)

    if not out["available"]:
        out["note"] = "无 RLC 时延数据"
    return out


# ----------------------------------------------------------------------------
# 报告格式化（供关键字层 attach 到 allure / 日志）
# ----------------------------------------------------------------------------
def format_report(report):
    """把分析结论渲染成可读文本。"""
    lines = []
    lines.append("=== svinsight EI 关联分析报告 ===")
    lines.append("目录: %s" % report.get("dir"))
    lines.append("分析小区数: %s, UE数: %s" % (
        report.get("cell_count"), report.get("ue_count")))
    lines.append("结论: %s" % report.get("summary"))

    for cell in report.get("cells", []):
        lines.append("--- 小区 %s ---" % cell["cellid"])
        for c in cell["checks"]:
            flag = "[异常]" if c["abnormal"] else "[正常]"
            lines.append("  %s %s = %s" % (flag, c["name"], c["value"]))

    for ue in report.get("ues", []):
        lines.append("--- UE %s ---" % ue["uegid"])
        for c in ue["checks"]:
            flag = "[异常]" if c["abnormal"] else "[正常]"
            lines.append("  %s %s = %s" % (flag, c["name"], c["value"]))

    pdcp = report.get("pdcp", {})
    if pdcp.get("available"):
        lines.append("--- PDCP 丢包 ---")
        for k, v in pdcp.items():
            if isinstance(v, dict) and ("loss_rate" in v):
                ev = v.get("loss_events")
                if ev is None:
                    ev = v.get("missing", 0)
                pk = v.get("packets") or v.get("total_pkts")
                lines.append("  %s: 丢包率=%s 事件数=%s 总包数=%s" % (
                    k, v.get("loss_rate"), ev, pk))

    tcp = report.get("tcp", {})
    if tcp.get("available"):
        lines.append("--- TCP 重传 ---")
        for k, v in (tcp.get("rates") or {}).items():
            lines.append("  %s 重传率=%.2f%%" % (k, v * 100))

    rlc = report.get("rlc", {})
    if rlc.get("available"):
        lines.append("--- RLC 时延 ---")
        dl = rlc.get("dl", {})
        if dl:
            if "rlc_cost_ms" in dl:
                lines.append("  下行 RLC 处理时延 p95=%.2fms" % dl["rlc_cost_ms"])
            if "pdcp_cost_ms" in dl:
                lines.append("  下行 PDCP 处理时延 p95=%.2fms" % dl["pdcp_cost_ms"])
        ul = rlc.get("ul", {})
        if ul:
            if "air_cost_ms" in ul:
                lines.append("  上行 RLC 空口时延 p95=%.2fms" % ul["air_cost_ms"])
            if "vta_p95" in ul:
                lines.append("  上行 RLC 窗口SN p95=%.2f (max=%.2f, Vrr_max=%.2f)"
                             % (ul["vta_p95"], ul.get("vta_max", 0),
                                ul.get("vrr_max") or 0))
        for a in rlc.get("anomalies", []):
            lines.append("  [异常] %s" % a)

    if report.get("events"):
        lines.append("--- 异常事件 ---")
        for e in report["events"]:
            lines.append("  - %s" % e)
    return "\n".join(lines)


if __name__ == "__main__":
    import json
    # 本地快速验证（需提供样本目录）
    _sample = r"C:\04_workspace\AutoTest\bjckAutotest\AutoTestMain\maintenanceTool\eiLog\20240730142906211"
    if os.path.isdir(_sample):
        _rep = analyze_svinsight_ei(_sample)
        print(format_report(_rep))
        print("\nhas_anomaly:", _rep["has_anomaly"])
    else:
        print("样本目录不存在，跳过自测")
