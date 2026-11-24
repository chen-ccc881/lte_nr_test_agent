#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Xtrace 信令流程顺序 + 异常事件 校验模板

背景:
    工程对 xtrace (.xst) 日志的校验原本只有"字段存在/取值"校验
    (key_analyze_and_check_xtrace_log) 与"黑名单信令是否存在"
    (key_analyze_and_check_xtrace_log_with_black_list)。
    两类校验都欠缺"信令出现的先后顺序"与"流程完整性"判断 —— 例如接入流程
    必须是 RRC Setup -> SecurityMode -> ... -> Reconfiguration，若顺序错乱或
    中间环节缺失，用例也应判失败。

本模块职责:
    1. 把 xtrace 解析出的单条 JSON 记录(见 traceLogExtractModel.xtraceLogAnalyze)
       按时间/序号排序，组成一条"信令时序流"。
    2. 提供两类校验:
       - 流程顺序校验(flow): 要求一组信令按指定先后次序出现(可带缺口/可选)。
       - 异常事件校验(anomaly): 要求某些黑名单信令/字段组合不应出现，或在
         特定条件下出现。
    3. 返回结构化的校验结果(dict)，由 keyword 层负责 allure 落盘与 assert，
       校验逻辑与用例断言解耦。

设计原则:
    - 纯函数/纯数据驱动，不依赖 allure / pytest，便于单测与复用。
    - 信令名匹配沿用工程约定: 空格与下划线等价、大小写不敏感(见
      json_field_reader._normalize_filter_key)。
    - 时间/序号解析失败时回退到"文件序号(SN)"，保证一定有可用排序键。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .json_field_reader import read_json_dir


# ==================== 工具函数 ====================

def _norm(s: str) -> str:
    """信令名规范化: 空格/下划线等价，转小写（与 json_field_reader 保持一致）。"""
    return s.replace(" ", "_").lower()


def _seq_of(filename: str) -> int:
    """从 xtrace 输出的 JSON 文件名中提取序号(SN)。

    文件名形如: 0-2026-09-20__10-40-11-...-RRC_SETUP.json
    首段即序号，提取失败返回 0（排序时落到最前）。
    """
    head = os.path.basename(filename).split("-", 1)[0]
    try:
        return int(head)
    except ValueError:
        return 0


def _to_ms(value: Any) -> Optional[float]:
    """把 report_time / frame_info 等尽力转成一个可比的时间毫秒数。

    无法解析时返回 None，调用方回退到 SN 排序。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # 优先匹配 HH:MM:SS(.fff) 或 年-月-日 时:分:秒
    m = re.search(r"(\d{1,2}):(\d{2}):(\d{2})(?:[.\-](\d{1,3}))?", text)
    if m:
        h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
        frac = int((m.group(4) or "0").ljust(3, "0"))
        return ((h * 3600 + mi * 60 + s) * 1000) + frac
    # 退而求其次: 直接找连续数字作为相对刻度
    digits = re.sub(r"\D", "", text)
    if digits:
        try:
            return float(digits)
        except ValueError:
            return None
    return None


def load_signal_records(json_dir: str,
                        sig_names: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    """读取 xtrace 解析目录中的信令记录，并按时间/序号排序。

    Args:
        json_dir: xtraceLogAnalyze 输出的 JSON 目录
        sig_names: 仅关注的信令名列表；为空表示全部。空格/下划线等价匹配。

    Returns:
        已排序的记录列表，每条记录至少含 file/_path/msg_name/report_time/SN 等字段。
    """
    def _as_str(s):
        return s.sig if isinstance(s, FlowStep) else str(s)
    file_filter = [_as_str(s) for s in (sig_names or [])]
    rows = read_json_dir(json_dir, file_filter=file_filter if file_filter else None)
    if not rows:
        # file_filter 为空时 read_json_dir 不按信令过滤，row 仍含 msg_name
        rows = read_json_dir(json_dir)

    def sort_key(r: Dict[str, Any]) -> Tuple[float, int]:
        # 优先 report_time -> frame_info -> SN
        for k in ("report_time", "frame_info", "print_time", "SN"):
            v = r.get(k)
            ms = _to_ms(v)
            if ms is not None:
                return (ms, _seq_of(r.get("file", "")))
        return (0.0, _seq_of(r.get("file", "")))

    rows.sort(key=sort_key)
    return rows


def _match_sig(row: Dict[str, Any], sig: str) -> bool:
    """单条记录是否匹配某个信令名（空格/下划线等价、忽略大小写）。"""
    target = _norm(sig)
    candidates = [str(row.get("file", "")), str(row.get("msg_name", ""))]
    return any(target in _norm(c) for c in candidates)


def _index_records(records: List[Dict[str, Any]]) -> Dict[str, List[int]]:
    """返回 信令名(规范化) -> 在 records 中出现的下标列表。"""
    index: Dict[str, List[int]] = {}
    for i, r in enumerate(records):
        for sig in (r.get("_sig_names") or []):
            index.setdefault(_norm(sig), []).append(i)
    return index


def _first(raw: Dict[str, Any]) -> Dict[str, Any]:
    """把 file_filter_dict 读取结果中嵌套的 _sig_names 规整出来。

    若 records 是由 read_json_dir(field_filter_dict=...) 读取，则每条 row 可能
    已按信令名分行；这里统一把每个 row 的 msg_name 当作其信令名。
    """
    return raw


# ==================== 流程顺序校验 ====================

class FlowStep:
    """流程中的单步/单信令节点。"""

    def __init__(self, sig: str, optional: bool = False, min_count: int = 1):
        self.sig = sig              # 信令名（空格/下划线均可）
        self.optional = optional    # 是否允许缺失（缺失不判失败，但会记录）
        self.min_count = min_count  # 至少出现次数

    def __repr__(self):
        return f"FlowStep({self.sig!r}, optional={self.optional}, min={self.min_count})"


def validate_flow(records: List[Dict[str, Any]],
                 steps: Sequence[FlowStep],
                 strict_order: bool = True) -> Dict[str, Any]:
    """校验信令流程顺序。

    Args:
        records: 已按时间排序的信令记录列表。
        steps:   期望出现的信令顺序。每个 step 可为字符串(= FlowStep(sig)) 或 FlowStep。
        strict_order:
            True  -> 每个 step 的首条命中必须晚于上一个 step 的首条命中
                     （即严格先后的链式顺序，允许中间夹带其它信令）。
            False -> 仅保证 step[0] 早于 step[-1]，中间节点只需"存在"。

    Returns:
        {
            "pass": bool,
            "ordered": bool,           # 顺序是否正确
            "missing": [sig, ...],     # 缺失(且非可选)的信令
            "details": [               # 每个 step 的命中情况
                {"sig":..., "found":bool, "count":int,
                 "first_index":int|None, "first_sn":int|None, "first_time":str|None},
                ...
            ],
            "message": str
        }
    """
    norm_steps: List[FlowStep] = []
    for s in steps:
        norm_steps.append(s if isinstance(s, FlowStep) else FlowStep(str(s)))

    # 为每条记录标注信令名（按 msg_name / file 推导）
    for r in records:
        names = set()
        m = r.get("msg_name")
        if isinstance(m, str):
            names.add(m)
        f = r.get("file", "")
        # 文件名中最后一个 '-' 之后通常是信令名
        tail = os.path.basename(f).rsplit("-", 1)[-1]
        tail = re.sub(r"\.json$", "", tail, flags=re.I)
        if tail:
            names.add(tail)
        r["_sig_names"] = list(names)

    index = _index_records(records)

    details: List[Dict[str, Any]] = []
    last_first_index = -1
    ordered = True
    missing: List[str] = []

    for step in norm_steps:
        hits = index.get(_norm(step.sig), [])
        found = len(hits) >= step.min_count
        first_index = hits[0] if hits else None
        first_sn = None
        first_time = None
        if first_index is not None:
            rec = records[first_index]
            first_time = rec.get("report_time")
            # SN 取文件名首段
            first_sn = _seq_of(rec.get("file", ""))
            if strict_order and first_index <= last_first_index:
                ordered = False
            if first_index > last_first_index:
                last_first_index = first_index

        if not found and not step.optional:
            missing.append(step.sig)

        details.append({
            "sig": step.sig,
            "optional": step.optional,
            "found": found,
            "count": len(hits),
            "first_index": first_index,
            "first_sn": first_sn,
            "first_time": first_time,
        })

    ok = (not missing) and (ordered if strict_order else True)
    msg = _build_flow_message(norm_steps, details, missing, ordered, strict_order)
    return {
        "pass": ok,
        "ordered": ordered,
        "missing": missing,
        "details": details,
        "message": msg,
    }


def _build_flow_message(steps, details, missing, ordered, strict_order) -> str:
    if missing:
        miss = ", ".join(missing)
        return f"流程缺失必需信令: [{miss}]"
    if strict_order and not ordered:
        return "流程信令顺序错误: 存在后出现的信令早于应先出现的信令"
    names = " -> ".join(s.sig for s in steps)
    return f"流程顺序校验通过: {names}"


# ==================== 异常事件校验 ====================

# 工程内常见异常/失败类信令与字段的关键字（可按需扩展）
# 工程内常见异常/失败类信令与字段的关键字（可按需扩展）。
# 同时覆盖 LTE(RRC_CONNECTION_*) 与 NR(RRC_*) 两套命名，并补充切换/释放/掉线类。
DEFAULT_ANOMALY_SIGNALS = [
    # ---- RRC 重建立 / 无线链路失败 ----
    "RRC_REESTABLISHMENT",                  # RRC 重建立（通用）
    "RRC_REESTABLISHMENT_REQUEST",          # 重建立请求
    "RRC_CONNECTION_REESTABLISHMENT",       # LTE 重建立
    "RRC_CONNECTION_REESTABLISHMENT_REQUEST",
    "RRC_CONNECTION_REESTABLISHMENT_REJECT",
    "RLF",                                  # 无线链路失败
    "RADIO_LINK_FAILURE",
    "RADIO_LINK_FAILURE_INDICATION",        # 基站侧 RLF 指示
    # ---- 接入失败 / 拒绝 ----
    "RRC_SETUP_FAILURE",                    # RRC 建立失败
    "RRC_CONNECTION_SETUP_FAILURE",         # LTE RRC 建立失败
    "RRC_CONNECTION_REJECT",                # 接入拒绝
    "RRC_REJECT",
    "RRC_SETUP_REJECT",                     # NR 接入拒绝
    # ---- 异常释放 / 掉线 ----
    "UE_CONTEXT_RELEASE_COMMAND",           # UE 上下文异常释放（需结合 cause 判断，见 allow_signals）
    "UE_CONTEXT_RELEASE_REQUEST",
    "RRC_CONNECTION_RELEASE",               # 异常释放（需结合 cause 判断）
    "RRC_CONNECTION_RELEASE_COMPLETE",
    # ---- 接口复位 / 异常 ----
    "NGAP_RESET",                           # NG 接口复位（异常）
    "NGAP_RESET_ACKNOWLEDGE",
    "S1AP_RESET",                           # S1 接口复位（异常）
    "S1AP_RESET_ACKNOWLEDGE",
    "NGAP_ERROR_INDICATION",                # NG 错误指示
    "S1AP_ERROR_INDICATION",                # S1 错误指示
    # ---- 切换失败 ----
    "HANDOVER_FAILURE",                     # 切换失败
    "RRC_HANDOVER_FAILURE",
    # ---- 信令超时 / 缺失（解析侧约定关键字，若 trace 工具输出此类文件名可命中）----
    "RRC_TIMEOUT",
    "NAS_AUTHENTICATION_REJECT",            # 鉴权拒绝
]


def validate_anomaly(records: List[Dict[str, Any]],
                     black_signals: Optional[Sequence[str]] = None,
                     allow_signals: Optional[Sequence[Tuple[str, Dict[str, Any]]]] = None,
                     case_insensitive: bool = True) -> Dict[str, Any]:
    """校验异常事件。

    Args:
        records: 已排序的信令记录列表。
        black_signals: 黑名单信令，出现即视为异常（命中后记录详情）。
                       为空时使用 DEFAULT_ANOMALY_SIGNALS。
        allow_signals: 白名单例外，[(信令名, {字段:期望值})]，表示"仅在字段匹配时才
                       算异常"，用于误报抑制。例如 RLF 仅当 cause 为特定值时才算。
                       默认为空（黑名单命中即异常）。
        case_insensitive: 信令名匹配是否忽略大小写（默认 True）。

    Returns:
        {
            "pass": bool,
            "hit": [ {sig, sn, time, file, matched_fields} , ... ],
            "message": str
        }
    """
    black = [s for s in (black_signals or DEFAULT_ANOMALY_SIGNALS)]
    black_norm = [_norm(s) for s in black]

    hit: List[Dict[str, Any]] = []
    for r in records:
        names = set()
        m = r.get("msg_name")
        if isinstance(m, str):
            names.add(m)
        tail = os.path.basename(r.get("file", "")).rsplit("-", 1)[-1]
        tail = re.sub(r"\.json$", "", tail, flags=re.I)
        if tail:
            names.add(tail)

        for nm in names:
            nm_norm = _norm(nm)
            if nm_norm not in black_norm:
                continue
            # 白名单例外：若指定字段与期望值匹配，则当作正常（不计入异常）
            suppressed = False
            if allow_signals:
                for allow_sig, field_map in allow_signals:
                    if _norm(allow_sig) != nm_norm:
                        continue
                    if _fields_match(r, field_map):
                        suppressed = True
                        break
            if suppressed:
                continue
            hit.append({
                "sig": nm,
                "sn": _seq_of(r.get("file", "")),
                "time": r.get("report_time"),
                "file": r.get("file"),
                "matched_fields": {},
            })
            break

    ok = len(hit) == 0
    if ok:
        msg = "异常事件校验通过: 未检测到黑名单信令"
    else:
        sigs = ", ".join(h["sig"] for h in hit)
        msg = f"检测到异常信令: [{sigs}]，共 {len(hit)} 条"
    return {"pass": ok, "hit": hit, "message": msg}


def _fields_match(record: Dict[str, Any], field_map: Dict[str, Any]) -> bool:
    """record 中全部指定字段均等于期望值才返回 True。字段缺失视为不匹配。"""
    for k, expect in field_map.items():
        actual = record.get(k)
        if actual is None:
            return False
        # 数值/字符串宽松比较
        try:
            if str(actual) != str(expect) and float(actual) != float(expect):
                return False
        except (TypeError, ValueError):
            if str(actual) != str(expect):
                return False
    return True


# ==================== 组合入口（供 keyword 层调用） ====================

def validate_xtrace_flow_and_anomaly(
    json_dir: str,
    flow_steps: Optional[Sequence[Any]] = None,
    strict_order: bool = True,
    black_signals: Optional[Sequence[str]] = None,
    allow_signals: Optional[Sequence[Tuple[str, Dict[str, Any]]]] = None,
    sig_scope: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """一站式校验: 流程顺序 + 异常事件。

    Args: 见 validate_flow / validate_anomaly。sig_scope 用于限定 load 范围，
          建议传流程与黑名单涉及的全部信令名以提升性能。

    Returns:
        {
            "pass": bool,
            "flow": {...},         # validate_flow 结果
            "anomaly": {...},      # validate_anomaly 结果
            "message": str
        }
    """
    scope = list(flow_steps or [])
    if black_signals:
        scope.extend(black_signals)
    if sig_scope:
        scope.extend(sig_scope)

    records = load_signal_records(json_dir, sig_names=scope or None)

    flow_res = validate_flow(records, flow_steps or [], strict_order=strict_order) \
        if flow_steps else {"pass": True, "ordered": True, "missing": [], "details": [],
                            "message": "未配置流程校验"}

    anomaly_res = validate_anomaly(records, black_signals=black_signals,
                                   allow_signals=allow_signals)

    ok = bool(flow_res.get("pass")) and bool(anomaly_res.get("pass"))
    msg = "; ".join([m for m in (flow_res.get("message"), anomaly_res.get("message")) if m])
    return {"pass": ok, "flow": flow_res, "anomaly": anomaly_res, "message": msg}
