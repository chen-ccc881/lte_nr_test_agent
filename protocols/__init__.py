"""协议深度解码包（本项目内实现，不依赖外部框架 / tshark / DLL）。

对外 API：
- decode_xtrace(path, mode='auto')：Xtrace .xst 解码（XST 头/记录/内部信令纯 Python；ASN.1 DLL 可选增强）
- analyze_sv(ei_dir, mode='auto')：SV/EI 调度日志异常分析（纯 pandas）
- decode_pcap(path)：Wireshark pcap 空口信令分层解码（纯 Python）
- parse_weblmt_config(path)：WebLMT 配置结构化解析
- validate_xtrace_flow(json_dir, ...)：接入流程/异常信令校验

所有函数在失败时返回可读字符串或安全结构，绝不因外部依赖缺失抛异常中断主流程。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def decode_xtrace(
    path: str,
    mode: str = "auto",
    max_records: int = 120,
    max_chars: int = 20000,
) -> str:
    """Xtrace .xst 深度解码（LLM 安全版）。

    关键：XST 记录可达上万条，逐条明细动辄数十万字符，直接回流到 ReAct
    agent 会撑爆模型上下文。因此输出分层：
    - 事件分布与接入关键事件为「全量精确计数」（非 Top8、非抽样），保证
      接入请求/成功/失败等统计可精确给出，不受明细截断影响；
    - 明细仅作时间/字段格式参考，条数受限；
    - 整体字符硬封顶，保证任何日志返回体都控制在 max_chars 以内。
    """
    from collections import Counter

    try:
        from . import trace_log_extract as tle
    except Exception as exc:
        return f"Xtrace解码模块加载失败：{type(exc).__name__}: {exc}"

    file_path = Path(path) if Path(path).is_absolute() else BASE_DIR / path
    if not file_path.exists():
        return f"Xtrace文件不存在：{file_path}"
    if mode == "auto":
        low = str(file_path).lower()
        mode = "4G" if ("lte" in low or "4g" in low) else "5G"

    try:
        parser = tle.XSTParser(str(file_path), mode)
        parser.little_endian = True
        xst = parser.parse()
        records = getattr(xst, "records_4g" if mode == "4G" else "records_5g", []) or []
        total = len(records)

        def event_name(msg_id):
            info = parser.msg_id_map.get(msg_id)
            return getattr(info, "name", "") if info else ""

        # 聚合统计：对全量记录做分布计数，信息密度远高于流水账
        iface_c: Counter = Counter()
        dir_c: Counter = Counter()
        event_c: Counter = Counter()
        msg_c: Counter = Counter()
        for rec in records:
            iface_c[str(getattr(rec, "s_interface", "") or getattr(rec, "u32_interface", "") or "未知")] += 1
            dir_c[str(getattr(rec, "s_dir", "") or getattr(rec, "u8_dir", "") or "未知")] += 1
            mid = getattr(rec, "u16_msg_id", 0)
            msg_c[str(mid)] += 1
            evt = event_name(mid) or "Unknown"
            event_c[evt] += 1

        def top(counter: Counter, n: int = 8) -> str:
            return ", ".join(f"{k}×{v}" for k, v in counter.most_common(n)) or "无"

        # 事件用全量精确计数（非 Top8）：接入类关键事件常落在长尾，
        # Top8 会漏掉，导致请求/成功/失败数只能估算。
        event_full = ", ".join(f"{k}×{v}" for k, v in event_c.most_common()) or "无"
        access_kw = (
            "rrcconnection", "randomaccess", "prach", "preamble", "attach",
            "securitymode", "nas transport", "erabsetup", "reject",
        )
        access_view = ", ".join(
            f"{k}×{v}" for k, v in event_c.most_common()
            if any(w in k.lower() for w in access_kw)
        ) or "无"

        lines = [
            f"【Xtrace深度解码】文件={file_path.name}, 制式={mode}, 记录数={total}",
            f"接口分布(Top8)：{top(iface_c)}",
            f"方向分布(Top8)：{top(dir_c)}",
            f"接入关键事件精确计数（全量）：{access_view}",
            f"事件全量精确计数（共{len(event_c)}类）：{event_full}",
            f"MsgID分布(Top8)：{top(msg_c)}",
            "明细样本（仅供格式/时间参考，精确统计以上方全量计数为准）：",
        ]
        for rec in records[:max_records]:
            msg_id = getattr(rec, "u16_msg_id", 0)
            event = event_name(msg_id)
            interface = getattr(rec, "s_interface", "") or str(getattr(rec, "u32_interface", ""))
            direction = getattr(rec, "s_dir", "") or str(getattr(rec, "u8_dir", ""))
            cell = getattr(rec, "u16_cell_id", "")
            frame = getattr(rec, "u32_sfn", "")
            report_time = getattr(rec, "report_time", "")
            lines.append(
                f"SN={getattr(rec, 'u32_s_info_seq', '')} 时间={report_time} "
                f"接口={interface} 方向={direction} 小区={cell} 帧={frame} "
                f"MsgID={msg_id} 事件={event}"
            )
        if total > max_records:
            lines.append(f"（仅展示前 {max_records} 条明细，共 {total} 条；全量分布见上方统计）")
        lines.append("说明：XST为纯Python解码；标准信令AS码流需ASN.1解码器，未加载时保留头部与MsgID证据。")

        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n（输出已按字符上限截断，完整记录已在统计中计数）"
        return text
    except Exception as exc:
        return f"Xtrace解码失败：{type(exc).__name__}: {exc}"


def analyze_sv(ei_dir: str, mode: str = "auto", thresholds: dict | None = None) -> str:
    """SV/EI 调度日志异常分析：BLER/HARQ/调度失败/MCS/PDCP 丢包等。"""
    try:
        from . import sv_ei_lte
    except Exception as exc:
        return f"SV分析模块加载失败：{type(exc).__name__}: {exc}"

    path = Path(ei_dir) if Path(ei_dir).is_absolute() else BASE_DIR / ei_dir
    if not path.exists():
        return f"SV/EI路径不存在：{path}"
    try:
        if mode == "auto":
            report, is_lte = sv_ei_lte.analyze_svinsight_ei_auto(str(path), thresholds=thresholds)
        elif mode == "LTE":
            report, is_lte = sv_ei_lte.analyze_svinsight_ei_lte(str(path), thresholds=thresholds), True
        else:
            from . import sv_ei_nr
            report, is_lte = sv_ei_nr.analyze_svinsight_ei(str(path), thresholds=thresholds), False

        if isinstance(report, dict):
            events = report.get("events", []) or []
            summary = report.get("summary", "")
            has_anomaly = report.get("has_anomaly", bool(events))
            parts = [
                f"【SV/EI调度日志分析】路径={path}, 制式={'LTE' if is_lte else 'NR'}",
                f"异常判定={has_anomaly}, 摘要={summary}",
                "异常事件：",
                *[f"- {e}" for e in events[:40]],
            ]
            cells = report.get("cells", []) or []
            for cell in cells[:10]:
                checks = cell.get("checks", []) or []
                abnormal = [c for c in checks if c.get("abnormal")]
                if abnormal:
                    parts.append(f"小区{cell.get('cellid')}异常项：" + ", ".join(
                        f"{c.get('name')}={c.get('value')}" for c in abnormal
                    ))
            if not events:
                parts.append("未检测到超阈值异常。")
            return "\n".join(parts)
        return f"SV/EI分析返回：{str(report)[:1000]}"
    except Exception as exc:
        return f"SV/EI分析失败：{type(exc).__name__}: {exc}"


def decode_pcap(path: str, max_packets: int = 5000) -> str:
    from .pcap_decode import decode_pcap as _decode
    return _decode(path, max_packets)


def parse_weblmt_config(path: str, keyword: str = "", max_rows: int = 200) -> str:
    """WebLMT 配置结构化解析：由 data_sources 提供，保持单一实现。"""
    from data_sources import parse_weblmt_config as _parse
    return _parse(path, keyword, max_rows)


def validate_xtrace_flow(json_dir: str, flow_steps=None, strict_order: bool = True,
                         black_signals=None, allow_signals=None) -> str:
    """Xtrace 接入流程/异常信令校验。"""
    try:
        from .xtrace_flow_validator import validate_xtrace_flow_and_anomaly
    except Exception as exc:
        return f"流程校验模块加载失败：{type(exc).__name__}: {exc}"
    path = Path(json_dir) if Path(json_dir).is_absolute() else BASE_DIR / json_dir
    if not path.exists():
        return f"Xtrace JSON目录不存在：{path}"
    try:
        result = validate_xtrace_flow_and_anomaly(
            str(path), flow_steps=flow_steps, strict_order=strict_order,
            black_signals=black_signals, allow_signals=allow_signals,
        )
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"流程校验失败：{type(exc).__name__}: {exc}"
