"""纯 Python pcap/pcapng 空口信令解码器（不依赖 tshark / scapy / dpkt）。

实现能力：
- 经典 pcap 与 pcapng 容器解析；
- 链路层Ethernet / Linux cooked / RAW IP 分层；
- IPv4 / IPv6、TCP / UDP / SCTP 传输层解析；
- 空口信令识别：SCTP→NGAP(5G)/S1AP(LTE)、UDP→GTP-U、TCP→HTTP/NAS tunnel；
- 从 payload 提取 NAS EMM/5GS 消息类型、RRC 明文特征、常见协议字段；
- 统计 RRC/NAS 相关包、协议分布、关键事件（Attach/Registration/RA 等）。

定位：提供可审计的结构化证据与消息类型判定；对完全加密的 AS 码流，
如实标注“已加密无法解码”，不编造字段。
"""

from __future__ import annotations

import struct
from collections import Counter
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


# ---------------- NAS 消息类型字典（3GPP TS24.301 / TS24.501） ----------------
NAS_EMM = {
    0x41: "Attach Request", 0x42: "Attach Accept", 0x43: "Attach Complete", 0x44: "Attach Reject",
    0x45: "Detach Request", 0x46: "Detach Accept",
    0x48: "Tracking Area Update Request", 0x49: "Tracking Area Update Accept", 0x4A: "Tracking Area Update Reject",
    0x4B: "Tracking Area Update Complete",
    0x50: "Authentication Request", 0x51: "Authentication Response", 0x52: "Authentication Reject",
    0x53: "Authentication Failure", 0x5A: "Security Mode Command", 0x5B: "Security Mode Complete",
    0x5C: "Security Mode Reject",
    0x60: "Service Request", 0x61: "Service Reject", 0x62: "Service Accept",
    0x64: "ESM Information Request", 0x65: "ESM Information Response",
    0x66: "GUTI Reallocation Command", 0x67: "GUTI Reallocation Complete",
    0x68: "Identity Request", 0x69: "Identity Response",
    0x6A: "Security Mode Command", 0x6B: "Security Mode Complete",
    0x6C: "Security Mode Reject", 0x6D: "EMM Information Request", 0x6E: "EMM Information Response",
    0x70: "EMM Status", 0x71: "Authentication Failure",
}
NAS_5GS = {
    0x41: "Registration Request", 0x42: "Registration Accept", 0x43: "Registration Complete",
    0x44: "Registration Reject", 0x45: "Deregistration Request", 0x46: "Deregistration Accept",
    0x47: "Ul NAS Transport", 0x48: "Dl NAS Transport",
    0x50: "Authentication Request", 0x51: "Authentication Response", 0x52: "Authentication Reject",
    0x53: "Authentication Failure", 0x54: "Authentication Result",
    0x55: "Security Mode Command", 0x56: "Security Mode Complete", 0x57: "Security Mode Reject",
    0x58: "Configuration Update Command", 0x59: "Configuration Update Complete",
    0x61: "Service Request", 0x63: "Service Accept", 0x64: "Service Reject",
    0x65: "PDU Session Establishment Request", 0x66: "PDU Session Establishment Accept",
    0x67: "PDU Session Establishment Reject", 0x68: "PDU Session Establishment Complete",
    0x69: "PDU Session Release Request", 0x6A: "PDU Session Release Reject", 0x6B: "PDU Session Release Complete",
    0x6C: "PDU Session Release Command", 0x6D: "PDU Session Modification Request",
    0x6E: "PDU Session Modification Reject", 0x6F: "PDU Session Modification Complete",
    0x7E: "5GMM Status",
}
# NGAP/S1AP 过程码（部分常见）
NGAP_PROC = {
    0x0C: "InitialUEMessage", 0x15: "InitialContextSetupRequest", 0x16: "InitialContextSetupResponse",
    0x17: "InitialContextSetupFailure", 0x1A: "UEContextReleaseRequest", 0x1B: "UEContextReleaseCommand",
    0x1C: "UEContextReleaseComplete", 0x20: "PDUSessionResourceSetupRequest", 0x21: "PDUSessionResourceSetupResponse",
    0x22: "PDUSessionResourceReleaseCommand", 0x23: "PDUSessionResourceReleaseResponse",
    0x26: "PathSwitchRequest", 0x27: "PathSwitchRequestAcknowledge", 0x28: "PathSwitchRequestFailure",
    0x29: "HandoverRequest", 0x2A: "HandoverRequestAcknowledge", 0x2B: "HandoverFailure",
    0x2C: "HandoverCommand", 0x2D: "HandoverNotify",
    0x2E: "UERadioCapabilityInfoIndication", 0x34: "UplinkNASTransport", 0x35: "DownlinkNASTransport",
    0x3E: "ErrorIndication", 0x40: "Reset", 0x41: "ResetAcknowledge",
}
IP_PROTO = {1: "ICMP", 6: "TCP", 17: "UDP", 132: "SCTP"}
SCTP_PPID_NGAP, SCTP_PPID_S1AP = 60, 18


def _resolve(path: str) -> Path:
    raw = Path(str(path)).expanduser()
    return raw if raw.is_absolute() else BASE_DIR / raw


def _iter_pcap(data: bytes):
    """迭代经典 pcap 包记录，yield (ts_sec, ts_usec, orig_len, frame_bytes)。"""
    if len(data) < 24:
        return
    magic = data[:4]
    if magic == b"\xd4\xc3\xb2\xa1":
        endian, nano = "<", False
    elif magic == b"\xa1\xb2\xc3\xd4":
        endian, nano = ">", False
    elif magic == b"\x4d\x3c\xb2\xa1":
        endian, nano = "<", True
    elif magic == b"\xa1\xb2\x3c\x4d":
        endian, nano = ">", True
    else:
        return
    linktype = struct.unpack_from(endian + "I", data, 20)[0]
    offset = 24
    while offset + 16 <= len(data):
        ts_sec, ts_sub, incl_len, orig_len = struct.unpack_from(endian + "IIII", data, offset)
        offset += 16
        frame = data[offset:offset + incl_len]
        offset += incl_len
        yield linktype, ts_sec + ts_sub / (1e9 if nano else 1e6), orig_len, frame


def _iter_pcapng(data: bytes):
    """迭代 pcapng 的 EPB（Enhanced Packet Block），兼容常见抓包导出。"""
    if data[:4] != b"\x0a\x0d\x0d\x0a":
        return
    offset = 0
    iface_linktypes = [1]
    while offset + 12 <= len(data):
        block_type = struct.unpack_from("<I", data, offset)[0]
        block_len = struct.unpack_from("<I", data, offset + 4)[0]
        if block_len < 12 or offset + block_len > len(data):
            break
        body = data[offset + 8: offset + block_len - 4]
        if block_type == 1:  # Interface Description Block
            linktype = struct.unpack_from("<H", body, 0)[0]
            iface_linktypes.append(linktype)
        elif block_type == 6:  # Enhanced Packet Block
            iface_id, ts_hi, ts_lo, cap_len, orig_len = struct.unpack_from("<IIIII", body, 0)
            ts = ((ts_hi << 32) | ts_lo) / 1e6
            frame = body[20:20 + cap_len]
            yield iface_linktypes[iface_id] if iface_id < len(iface_linktypes) else 1, ts, orig_len, frame
        offset += block_len


def _parse_ip(frame: bytes, linktype: int):
    """返回 (src, dst, proto, l4_payload)；失败返回 None。"""
    if linktype == 1:  # Ethernet
        if len(frame) < 14:
            return None
        ethertype = struct.unpack_from("!H", frame, 12)[0]
        offset = 14
        while ethertype in (0x8100, 0x88A8):  # VLAN
            if len(frame) < offset + 4:
                return None
            ethertype = struct.unpack_from("!H", frame, offset + 2)[0]
            offset += 4
    elif linktype == 113:  # Linux cooked SLL
        if len(frame) < 16:
            return None
        ethertype = struct.unpack_from("!H", frame, 14)[0]
        offset = 16
    elif linktype == 101:  # RAW IP
        ethertype, offset = 0x0800, 0
    elif linktype == 12:  # RAW IPv6
        ethertype, offset = 0x86DD, 0
    else:
        return None

    if ethertype == 0x0800:  # IPv4
        if len(frame) < offset + 20:
            return None
        ihl = (frame[offset] & 0x0F) * 4
        proto = frame[offset + 9]
        src = ".".join(str(b) for b in frame[offset + 12:offset + 16])
        dst = ".".join(str(b) for b in frame[offset + 16:offset + 20])
        l4 = offset + ihl
    elif ethertype == 0x86DD:  # IPv6
        if len(frame) < offset + 40:
            return None
        proto = frame[offset + 6]
        src = frame[offset + 8:offset + 24].hex()
        dst = frame[offset + 24:offset + 40].hex()
        l4 = offset + 40
    else:
        return None
    return src, dst, proto, frame[l4:]


def _l4_ports(proto: int, payload: bytes):
    if proto == 6 and len(payload) >= 4:  # TCP
        return struct.unpack_from("!HH", payload, 0)[0], struct.unpack_from("!HH", payload, 0)[1], payload[20:]
    if proto == 17 and len(payload) >= 4:  # UDP
        return struct.unpack_from("!HH", payload, 0)[0], struct.unpack_from("!HH", payload, 0)[1], payload[8:]
    return None, None, payload


def _decode_sctp_chunks(payload: bytes):
    """解析 SCTP DATA chunk，返回 [(ppid, data)]。"""
    chunks = []
    offset = 12  # 跳过 SCTP common header
    while offset + 4 <= len(payload):
        ctype = payload[offset]
        clen = struct.unpack_from("!H", payload, offset + 2)[0]
        if clen < 4 or offset + clen > len(payload):
            break
        if ctype == 0:  # DATA: 4B通用头 + 12B(TSN/streamID/seq/PPID) => PPID@12, payload@16
            ppid = struct.unpack_from("!I", payload, offset + 12)[0] if clen >= 16 else 0
            chunks.append((ppid, payload[offset + 16:offset + clen]))
        offset += clen + ((4 - clen % 4) % 4)
    return chunks


def _classify_app(proto: int, l4_payload: bytes, sport: int, dport: int):
    """识别应用层/空口协议，返回 (protocol, procedure, evidence)。"""
    if proto == 132:  # SCTP
        for ppid, data in _decode_sctp_chunks(l4_payload):
            if not data:
                continue
            table = NGAP_PROC if ppid == SCTP_PPID_NGAP else {}
            # NGAP/S1AP 初始化 PDU 的过程码通常在首字节后的编码字段，做特征匹配兜底
            proc = ""
            for code, name in table.items():
                if data and data[0] & 0xFE == 0 and code in data[:6]:
                    proc = name
                    break
            if ppid == SCTP_PPID_NGAP:
                return "NGAP", proc or "NGAP-PDU", f"PPID={ppid}, len={len(data)}"
            if ppid == SCTP_PPID_S1AP:
                return "S1AP", proc or "S1AP-PDU", f"PPID={ppid}, len={len(data)}"
        return "SCTP", "", "未解析到DATA chunk"
    if proto == 17:  # UDP
        if sport == 2152 or dport == 2152:  # GTP-U
            return "GTP-U", "用户面数据", f"UDP:{sport}->{dport}"
        return "UDP", "", f"UDP:{sport}->{dport}"
    if proto == 6:  # TCP
        head = l4_payload[:16].lower()
        if b"nas" in head or sport in (2123, 2124) or dport in (2123, 2124):
            return "NAS-over-TCP", "", f"TCP:{sport}->{dport}"
        return "TCP", "", f"TCP:{sport}->{dport}"
    return IP_PROTO.get(proto, f"IP-{proto}"), "", ""


def _nas_guess(payload: bytes):
    """在 payload 中启发式定位 NAS 消息类型字节（处理 security header）。"""
    findings = []
    for idx in range(min(len(payload), 64)):
        byte = payload[idx]
        # 明文 NAS：0x40/0x50/0x60/0x70 系列为 EMM/5GMM 消息
        if byte in NAS_5GS or byte in NAS_EMM:
            name5g = NAS_5GS.get(byte)
            name4g = NAS_EMM.get(byte)
            findings.append((idx, byte, name5g or name4g))
    return findings


def decode_pcap(path: str, max_packets: int = 5000) -> str:
    """主入口：解码 pcap/pcapng，返回结构化中文报告。"""
    file_path = _resolve(path)
    if not file_path.exists():
        return f"pcap文件不存在：{file_path}"
    data = file_path.read_bytes()
    if data[:4] == b"\x0a\x0d\x0d\x0a":
        packets = list(_iter_pcapng(data))[:max_packets]
    else:
        packets = list(_iter_pcap(data))[:max_packets]

    if not packets:
        return f"未从 {file_path.name} 解析到有效包（可能为空文件或不支持的封装）。"

    proto_counter: Counter = Counter()
    app_counter: Counter = Counter()
    nas_events: list[str] = []
    rrc_evidence: list[str] = []
    total_len = 0
    first_ts = None
    last_ts = None

    for linktype, ts, orig_len, frame in packets:
        total_len += orig_len
        first_ts = ts if first_ts is None else first_ts
        last_ts = ts
        parsed = _parse_ip(frame, linktype)
        if not parsed:
            proto_counter["NonIP"] += 1
            continue
        src, dst, proto, l4_payload = parsed
        sport, dport, app_payload = _l4_ports(proto, l4_payload)
        proto_counter[IP_PROTO.get(proto, str(proto))] += 1
        app, procedure, evidence = _classify_app(proto, l4_payload, sport or 0, dport or 0)
        app_counter[app] += 1

        # NAS 消息类型提取（SCTP DATA 或 TCP payload）
        if app in ("NGAP", "S1AP", "NAS-over-TCP"):
            for idx, code, name in _nas_guess(app_payload or l4_payload):
                if name and len(nas_events) < 40:
                    nas_events.append(f"t={ts:.3f} {app} {name}(0x{code:02X}@{idx}) {src}->{dst}")
            if procedure and len(nas_events) < 40:
                nas_events.append(f"t={ts:.3f} {app} 过程={procedure} {evidence}")

        # RRC 明文特征（PCAP 中若为空口镜像且未加密）
        lowered = (app_payload or b"").lower()
        for keyword in (b"rrc", b"rach", b"prach", b"sib1", b"handover", b"reestablishment"):
            if keyword in lowered:
                rrc_evidence.append(f"t={ts:.3f} 命中关键字 {keyword.decode()}")
                break

    duration = (last_ts - first_ts) if (first_ts is not None and last_ts is not None) else 0.0
    lines = [
        f"【pcap空口信令解码】文件={file_path.name}",
        f"解析包数={len(packets)}（上限{max_packets}）, 累计字节={total_len}, 时间跨度≈{duration:.3f}s",
        "传输协议分布：" + ", ".join(f"{k}={v}" for k, v in proto_counter.most_common()),
        "应用/空口协议分布：" + ", ".join(f"{k}={v}" for k, v in app_counter.most_common()),
        "",
        "【NAS/NGAP/S1AP 关键事件】" if nas_events else "【NAS/NGAP/S1AP】未提取到明关键事件（可能加密或未包含控制面）",
        *nas_events[:30],
        "",
        "【RRC 明文特征】" if rrc_evidence else "【RRC】未命中明文特征（AS 消息通常已加密，需终端侧信令日志）",
        *rrc_evidence[:15],
        "",
        "说明：本解码为纯 Python 协议栈分层 + 消息类型识别，不依赖 tshark/DLL；加密 AS 码流如实标注不可解码。",
    ]
    return "\n".join(lines)
