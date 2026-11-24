#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XST文件解析器 - 4G/5G信令解析 (Python实现)

功能:
  - 解析.xst二进制文件 (自软件格式和网管格式)
  - 5G加载SigTrace.xml配置，4G加载LTESig.xml配置(事件ID映射、结构体/联合体/枚举定义)
  - 解析4G/5G信令记录头信息(时间、接口、方向等)
  - 解析标准信令(MsgID 1000-1999 或 >=3000)与内部信令
  - 通过DLL调用ASN1解码器解析标准信令码流(5G用LTEAsn1.dll，4G用4GAsn1.dll)
  - 将每条信令保存为独立的JSON文件

用法:
  python xst_parser_5G.py <文件路径> [--mode 4G|5G] [--filter 信令名称1,信令名称2,...]
  python xst_parser_5G.py input.xst --mode 4G  # 解析4G
  python xst_parser_5G.py input.xst --mode 5G  # 解析5G(默认)
  python xst_parser_5G.py input.xst --filter INITIAL_CONTEXT_SETUP_REQUEST  # 只解析指定信令
"""

from __future__ import annotations

import os
import sys
import json
import struct
import ctypes
import datetime
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple, Union, Set


# ==================== 常量定义 ====================

FILE_TYPE_MAGIC        = 0xDEDEDEDE
RECORD_SPEC_ID         = 0xBCBC
FILE_EXT_NAME          = ".xst"
MAX_RECORDS            = 56000
FILE_HEADER_SIZE       = 16
RECORD_HEADER_SIZE     = 4
SIGNAL_TRACE_BUFF_LEN_MAX = 20000
UE_EUTRAN_TRACE_ID_LEN = 8


# ==================== 数据类定义 ====================

@dataclass
class FileHeader:
    """文件头结构"""
    u32_file_type: int = 0        # uint32
    au8_file_ex_name: bytes = b'\x00' * 4  # [4]byte
    u32_record_num: int = 0       # uint32
    u32_file_total_len: int = 0   # uint32


@dataclass
class RecordHeader:
    """记录头结构"""
    u16_record_spec_id: int = 0   # uint16
    u16_record_length: int = 0    # uint16


@dataclass
class AbsTime:
    """时间结构"""
    au8_year: bytes = b'\x00' * 2  # [2]byte
    u8_mon: int = 0
    u8_day: int = 0
    u8_hour: int = 0
    u8_min: int = 0
    u8_sec: int = 0


@dataclass
class SignalTraceInfo:
    """信令跟踪业务信息 (20字节) - 4G和5G通用"""
    u16_cell_id: int = 0     # uint16  u16CellID
    u16_du_gid: int = 0      # uint16  u16DuGID
    u32_gid: int = 0         # uint32  u32GID
    u32_interface: int = 0   # uint32  u32Interface
    u8_dir: int = 0          # byte    usDir
    u8_reserved2: int = 0    # byte    usReserved2
    u16_msg_id: int = 0      # uint16  u16MsgID
    u32_enb_id: int = 0      # uint32  u32EnbID


@dataclass
class SignalTraceInfoLTE:
    """4G信令跟踪结构 T_SignalTraceInfo_LTE (20字节)"""
    u16_msg_type: int = 0    # uint16  wDataId
    u16_cell_id: int = 0     # uint16  u16CellID
    u16_gid: int = 0         # uint16  u16GID
    u16_rnti: int = 0        # uint16  u16RNTI
    u16_msg_id: int = 0      # uint16  wMsgName
    u16_trans_time: int = 0  # uint16  u16TransTime
    u8_dir: int = 0          # byte    u8Dir
    u8_sop_reason: int = 0   # byte    u8SopReason
    u16_task_id: int = 0     # uint16  u16TaskID
    u16_sctp_as_id: int = 0  # uint16  u16SctpAsID
    u16_res: int = 0         # uint16  u16Res


@dataclass
class SignalTraceStruct:
    """4G/5G信令跟踪结构"""
    u16_cell_id: int = 0
    u16_du_gid: int = 0
    u32_gid: int = 0
    s_interface: str = ""
    u32_interface: int = 0
    s_dir: str = ""
    u16_msg_id: int = 0
    u32_s_info_seq: int = 0
    u32_sfn: int = 0
    s_fram_info: str = ""
    report_time: str = ""
    u16_msg_len: int = 0
    print_time: str = ""
    ach_print_buff: bytes = b""
    # 4G特有字段
    u16_msg_type: int = 0       # wDataId
    u16_rnti: int = 0           # RNTI
    u16_trans_time: int = 0     # 传输时间
    u8_sop_reason: int = 0      # SOP原因
    u16_task_id: int = 0        # 任务ID
    u16_sctp_as_id: int = 0     # SCTP AS ID
    mode: str = "5G"            # "4G" 或 "5G"


@dataclass
class XSTRecord:
    """单条原始记录"""
    data: bytes = b""


@dataclass
class XSTFile:
    """解析结果"""
    is_self_software_file: bool = False
    records: List[XSTRecord] = field(default_factory=list)
    records_5g: List[SignalTraceStruct] = field(default_factory=list)
    records_4g: List[SignalTraceStruct] = field(default_factory=list)


@dataclass
class SigEventInfo:
    """信号事件信息"""
    name: str = ""          # 事件名称
    struct_type: str = ""   # 结构体类型


@dataclass
class FieldProperty:
    """字段属性"""
    b_is_array: bool = False
    is_changed: bool = False
    field_mem_offset: int = 0        # uint32
    array_dim: int = 0               # uint32
    array_size: List[int] = field(default_factory=lambda: [0] * 5)  # [5]uint32
    b_is_bit_region: bool = False
    bit_region_count: int = 0


@dataclass
class BitRegionProperty:
    """位域属性"""
    b_fir_bit_variable_flag: bool = False
    offset_position: int = 0          # 位域偏移位置（字节偏移）
    bit_region_position: int = 0      # 位域位置（比特位置）


@dataclass
class StructField:
    """结构体字段"""
    name: str = ""
    type: str = ""
    property: FieldProperty = field(default_factory=FieldProperty)


@dataclass
class StructInfo:
    """结构体信息"""
    name: str = ""
    length: int = 0              # 结构体大小
    fields: List[StructField] = field(default_factory=list)
    is_union: bool = False
    is_enum: bool = False


@dataclass
class TreeNode:
    """树节点（用于解析解码结果）"""
    name: str = ""
    children: List[TreeNode] = field(default_factory=list)
    parent: Optional[TreeNode] = None
    leaf_value: str = ""


# ==================== XST 解析器 ====================

class XSTParser:
    """XST文件解析器"""

    def __init__(self, file_path: str, mode: str = "5G"):
        self.file_path: str = file_path
        self.data: bytearray = bytearray()
        self.local_sn: int = 0
        self.msg_id_map: Dict[int, SigEventInfo] = {}      # MsgID -> 事件信息
        self.struct_map: Dict[str, StructInfo] = {}          # 结构体名称 -> 结构体信息
        self.little_endian: bool = False                     # 默认使用大端字节序
        self.bit_region_prop: BitRegionProperty = BitRegionProperty()
        self.mode: str = mode.upper()                        # "4G" 或 "5G"

    # ---------- XML 加载 ----------

    def load_sig_trace_xml(self) -> None:
        """加载脚本同级目录下的XML文件：5G加载SigTrace.xml，4G加载LTESig.xml"""
        # 获取脚本所在目录（同级目录）
        try:
            script_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'xtraceLib')
        except NameError:
            script_dir = os.getcwd()

        # 根据模式选择XML文件
        if self.mode == "4G":
            xml_file_name = "LTESig.xml"
        else:
            xml_file_name = "SigTrace.xml"

        xml_path = os.path.join(script_dir, xml_file_name)
        if not os.path.isfile(xml_path):
            # 脚本目录没有，尝试工作目录
            xml_path = os.path.join(os.getcwd(), xml_file_name)
        if not os.path.isfile(xml_path):
            raise RuntimeError(f"未找到XML文件: {script_dir}\\{xml_file_name}")

        print(f"加载XML文件({self.mode}模式): {xml_path}")

        # 解析XML
        try:
            tree = ET.parse(xml_path)
        except ET.ParseError as e:
            raise RuntimeError(f"解析XML失败: {e}")

        root = tree.getroot()

        # 构建MsgID映射
        event_count = 0
        sig_event_id_list = root.find("SigEventIdList")
        if sig_event_id_list is not None:
            for var_elem in sig_event_id_list.findall("Variable"):
                name = var_elem.get("name", "")
                value_str = var_elem.get("value", "")
                struct_type = var_elem.get("structtype", "")
                try:
                    msg_id = int(value_str)
                except ValueError:
                    print(f"警告: 无法解析MsgID值 '{value_str}'")
                    continue
                self.msg_id_map[msg_id] = SigEventInfo(name=name, struct_type=struct_type)
                event_count += 1

        # 加载结构体列表
        struct_count = 0
        struct_list = root.find("StructList")
        if struct_list is not None:
            for struct_elem in struct_list.findall("Struct"):
                self._parse_struct_element(struct_elem, is_union=False)
                struct_count += 1

        # 加载联合体列表
        union_list = root.find("UnionList")
        if union_list is not None:
            for union_elem in union_list.findall("Struct"):
                self._parse_struct_element(union_elem, is_union=True)
                struct_count += 1

        # 加载枚举列表
        enum_list = root.find("EnumList")
        if enum_list is not None:
            for enum_elem in enum_list.findall("Enum"):
                self._parse_enum_element(enum_elem)
                struct_count += 1

        print(f"成功加载 {event_count} 个事件ID映射")
        print(f"成功加载 {struct_count} 个结构体定义")

    def _parse_struct_element(self, struct_elem: ET.Element, is_union: bool) -> None:
        """解析结构体/联合体元素"""
        name = struct_elem.get("name", "")
        len_str = struct_elem.get("len", "")
        try:
            struct_len = int(len_str)
        except ValueError:
            print(f"警告: 无法解析结构体长度 '{len_str}'")
            return

        fields: List[StructField] = []
        for var_elem in struct_elem.findall("Variable"):
            field_name = var_elem.get("name", "")
            field_type = var_elem.get("type", "")
            offset_str = var_elem.get("offset", "")
            arr_cnt0 = var_elem.get("arrcnt0", "")
            arr_cnt1 = var_elem.get("arrcnt1", "")
            arr_cnt2 = var_elem.get("arrcnt2", "")
            arr_cnt3 = var_elem.get("arrcnt3", "")
            arr_cnt4 = var_elem.get("arrcnt4", "")

            prop = FieldProperty()

            # 解析偏移量
            if offset_str:
                try:
                    prop.field_mem_offset = int(offset_str)
                except ValueError:
                    pass

            # 解析数组维度
            arr_counts = [arr_cnt0, arr_cnt1, arr_cnt2, arr_cnt3, arr_cnt4]
            for i, cnt in enumerate(arr_counts):
                if cnt:
                    prop.b_is_array = True
                    try:
                        prop.array_size[i] = int(cnt)
                    except ValueError:
                        pass
                    prop.array_dim += 1

            # 检查位域（名称中包含 ":"）
            if ":" in field_name:
                prop.b_is_bit_region = True
                parts = field_name.split(":")
                if len(parts) == 2:
                    try:
                        prop.bit_region_count = int(parts[1])
                    except ValueError:
                        pass

            fields.append(StructField(name=field_name, type=field_type, property=prop))

        self.struct_map[name] = StructInfo(
            name=name, length=struct_len, fields=fields, is_union=is_union
        )

        # 调试特定结构体
        if name == "_T_L2_CELL_RB_UPDATE_NOTIFY":
            print(f"调试: 已加载结构体 {name}, 大小: {struct_len}, 字段数: {len(fields)}")
            for i, f in enumerate(fields):
                print(f"  字段{i}: {f.name}, 类型: {f.type}, 偏移: {f.property.field_mem_offset}")

    def _parse_enum_element(self, enum_elem: ET.Element) -> None:
        """解析枚举元素"""
        name = enum_elem.get("name", "")
        len_str = enum_elem.get("len", "")
        try:
            enum_len = int(len_str)
        except ValueError:
            print(f"警告: 无法解析枚举长度 '{len_str}'")
            return

        fields: List[StructField] = []
        for var_elem in enum_elem.findall("Variable"):
            field_name = var_elem.get("name", "")
            const_str = var_elem.get("const", "")

            prop = FieldProperty()

            if const_str:
                if "-" in const_str:
                    prop.field_mem_offset = 0xFFFFFFFF
                else:
                    try:
                        if const_str.lower().startswith("0x"):
                            prop.field_mem_offset = int(const_str, 16)
                        else:
                            prop.field_mem_offset = int(const_str)
                    except ValueError:
                        pass

            fields.append(StructField(name=field_name, type="U32", property=prop))

        self.struct_map[name] = StructInfo(
            name=name, length=enum_len, fields=fields, is_enum=True
        )

    # ---------- 文件读取与解析 ----------

    def parse(self) -> XSTFile:
        """解析XST文件（主入口）"""
        # 加载XML配置文件（5G: SigTrace.xml, 4G: LTESig.xml）
        try:
            self.load_sig_trace_xml()
        except Exception as e:
            print(f"警告: 加载XML配置文件失败: {e}")

        # 读取文件数据
        self._read_file()

        # 解析文件
        xst_file = XSTFile()

        # 读取并验证文件头
        is_self_file = self._parse_file_header()
        xst_file.is_self_software_file = is_self_file

        # 解析记录
        self._parse_records(xst_file)

        return xst_file

    def _read_file(self) -> None:
        """读取文件内容到内存"""
        try:
            with open(self.file_path, "rb") as f:
                raw = f.read()
            self.data = bytearray(raw)
        except Exception as e:
            raise RuntimeError(f"读取文件失败: {e}")

    def _read_file_header(self, offset: int) -> FileHeader:
        """从指定偏移读取文件头"""
        d = self.data
        header = FileHeader()
        header.u32_file_type = struct.unpack_from("<I", d, offset)[0]
        header.au8_file_ex_name = bytes(d[offset + 4:offset + 8])
        header.u32_record_num = struct.unpack_from("<I", d, offset + 8)[0]
        header.u32_file_total_len = struct.unpack_from("<I", d, offset + 12)[0]
        return header

    def _parse_file_header(self) -> bool:
        """解析并验证文件头，返回是否为自软件格式"""
        if len(self.data) < FILE_HEADER_SIZE:
            raise RuntimeError("文件太小，不是有效的XST文件")

        # 从文件开头读取
        header = self._read_file_header(0)
        ext_name = header.au8_file_ex_name.decode("latin-1", errors="replace")
        print(len(self.data))
        if (ext_name == FILE_EXT_NAME and
                header.u32_file_type == FILE_TYPE_MAGIC):
            if header.u32_record_num > MAX_RECORDS:
                raise RuntimeError(
                    f"文件记录数 {header.u32_record_num} 超过最大限制 {MAX_RECORDS}"
                )
            return True  # 自软件格式
        
#         if (ext_name == FILE_EXT_NAME and
#                 header.u32_file_type == FILE_TYPE_MAGIC and
#                 header.u32_file_total_len == len(self.data)):
#             if header.u32_record_num > MAX_RECORDS:
#                 raise RuntimeError(
#                     f"文件记录数 {header.u32_record_num} 超过最大限制 {MAX_RECORDS}"
#                 )
#             return True  # 自软件格式

        # 文件头验证失败，尝试从文件尾部读取（网管文件格式）
        if len(self.data) < FILE_HEADER_SIZE * 2:
            raise RuntimeError("文件太小，不是有效的XST文件")

        tail_offset = len(self.data) - FILE_HEADER_SIZE
        header = self._read_file_header(tail_offset)
        ext_name = header.au8_file_ex_name.decode("latin-1", errors="replace")

        if ext_name != FILE_EXT_NAME or header.u32_file_type != FILE_TYPE_MAGIC:
            raise RuntimeError("文件格式非XST")

        # 验证尾部文件头
        expected_len = (header.u32_file_total_len + FILE_HEADER_SIZE +
                        header.u32_record_num * RECORD_HEADER_SIZE)
        if expected_len != len(self.data):
            raise RuntimeError(
                f"文件长度校验失败: 期望 {expected_len}, 实际 {len(self.data)}"
            )

        if header.u32_record_num > MAX_RECORDS:
            raise RuntimeError(
                f"文件记录数 {header.u32_record_num} 超过最大限制 {MAX_RECORDS}"
            )

        return False  # 网管格式

    def _parse_records(self, xst_file: XSTFile) -> None:
        """解析所有记录"""
        # 读取文件头以获取记录数
        header = self._read_file_header(0)
        if header.u32_file_type != FILE_TYPE_MAGIC:
            tail_offset = len(self.data) - FILE_HEADER_SIZE
            header = self._read_file_header(tail_offset)

        # 设置初始偏移
        accumulate_len = FILE_HEADER_SIZE
        if not xst_file.is_self_software_file:
            accumulate_len = 0

        d = self.data
        total_size = len(d)

        for record_loop in range(header.u32_record_num):
            # 检查是否有足够的数据读取记录头
            if accumulate_len + RECORD_HEADER_SIZE > total_size:
                raise RuntimeError(
                    f"记录 {record_loop}: 数据不足，无法读取记录头"
                )

            # 读取记录头
            record_header = RecordHeader()
            record_header.u16_record_spec_id = struct.unpack_from("<H", d, accumulate_len)[0]
            record_header.u16_record_length = struct.unpack_from("<H", d, accumulate_len + 2)[0]
            accumulate_len += RECORD_HEADER_SIZE

            # 校验记录头
            if record_header.u16_record_length > (total_size - accumulate_len):
                raise RuntimeError(
                    f"记录 {record_loop}: 记录长度 {record_header.u16_record_length} 超过剩余数据长度"
                )
            if record_header.u16_record_spec_id != RECORD_SPEC_ID:
                raise RuntimeError(
                    f"记录 {record_loop}: 无效的记录规范ID 0x{record_header.u16_record_spec_id:04X}"
                )

            # 读取记录数据
            rec_len = record_header.u16_record_length
            if accumulate_len + rec_len > total_size:
                raise RuntimeError(
                    f"记录 {record_loop}: 数据不足，无法读取完整记录"
                )

            record_data = bytes(d[accumulate_len:accumulate_len + rec_len])
            accumulate_len += rec_len

            # 添加到原始记录
            xst_file.records.append(XSTRecord(data=record_data))

            # 根据mode解析4G或5G记录
            sig_record = SignalTraceStruct(
                ach_print_buff=b'\x00' * SIGNAL_TRACE_BUFF_LEN_MAX,
                print_time=datetime.datetime.now().strftime("%H:%M:%S"),
                mode=self.mode,
            )
            try:
                if self.mode == "4G":
                    self._parse_4g_record(record_data, sig_record)
                    # 数量保护
                    if len(xst_file.records_4g) >= MAX_RECORDS:
                        xst_file.records_4g.pop(0)
                    xst_file.records_4g.append(sig_record)
                else:
                    self._parse_5g_record(record_data, sig_record)
                    # 数量保护
                    if len(xst_file.records_5g) >= MAX_RECORDS:
                        xst_file.records_5g.pop(0)
                    xst_file.records_5g.append(sig_record)
                self.local_sn += 1
            except ValueError:
                pass  # 非目标记录，跳过

            # 打印进度
            if (record_loop + 1) % 1000 == 0 or record_loop == header.u32_record_num - 1:
                progress = (record_loop + 1) / header.u32_record_num * 100
                print(f"解析进度: {progress:.2f}% ({record_loop + 1}/{header.u32_record_num})")

    # ---------- 5G / 4G 记录解析 ----------

    @staticmethod
    def _parse_5g_record(data: bytes, record: SignalTraceStruct) -> None:
        """解析5G信令记录数据（大端序）
        记录结构: 24字节(基本头) + 20字节(T_SignalTraceInfo) = 44字节
        T_SignalTraceInfo:
          u16CellID(2) u16DuGID(2) u32GID(4) u32Interface(4)
          usDir(1) usReserved2(1) u16MsgID(2) u32EnbID(4)
        """
        if len(data) < 44:
            raise ValueError("数据长度不足，无法解析5G记录")

        # 基本头部 (offset: 0-23, 共24字节)
        u32_s_info_seq = struct.unpack_from(">I", data, 0)[0]
        u32_frm_inf = struct.unpack_from(">I", data, 4)[0]

        # 时间信息 (offset: 9-15)
        au8_year = data[9:11]  # [2]byte
        u8_mon = data[11]
        u8_day = data[12]
        u8_hour = data[13]
        u8_min = data[14]
        u8_sec = data[15]

        # T_SignalTraceInfo (offset: 24-43, 共20字节)
        u16_cell_id = struct.unpack_from(">H", data, 24)[0]
        u16_du_gid = struct.unpack_from(">H", data, 26)[0]
        u32_gid = struct.unpack_from(">I", data, 28)[0]
        u32_interface = struct.unpack_from(">I", data, 32)[0]
        u8_dir = data[36]
        u16_msg_id = struct.unpack_from(">H", data, 38)[0]

        # 填充记录
        record.u16_cell_id = u16_cell_id
        record.u16_du_gid = u16_du_gid
        record.u32_gid = u32_gid
        record.u32_interface = u32_interface
        record.u16_msg_id = u16_msg_id
        record.u32_s_info_seq = u32_s_info_seq
        record.u32_sfn = u32_frm_inf

        # 解析接口类型
        interface_map = {
            0: "NG", 1: "XN", 2: "UU", 3: "CU_INNER",
            4: "DU_INNER", 5: "OUT", 6: "SYNC", 7: "F1",
        }
        record.s_interface = interface_map.get(u32_interface, "UNKNOWN")

        # 解析方向
        dir_map = {
            0: "CU_SEND", 1: "CU_RECEIVE", 2: "DU_SEND", 3: "DU_RECEIVE",
            4: "CTLP_SEND", 5: "CTLP_RECEIVE", 6: "RRM_SEND", 7: "RRM_RECEIVE",
            8: "ENB_OTHER",
        }
        record.s_dir = dir_map.get(u8_dir, "UNKNOWN")

        # 解析帧号子帧号
        frame_no = u32_frm_inf // 10
        sub_frame_no = u32_frm_inf % 10
        record.s_fram_info = f"{frame_no}:{sub_frame_no}"

        # 解析上报时间（年份按大端序读取：高字节在前，无需字节交换）
        year = (au8_year[0] << 8) + au8_year[1]
        record.report_time = f"{year}-{u8_mon}-{u8_day}  {u8_hour}:{u8_min}:{u8_sec}"

        # 复制打印内容 (offset: 44之后)
        head_size = 44
        if len(data) > head_size:
            copy_len = min(len(data) - head_size, SIGNAL_TRACE_BUFF_LEN_MAX)
            record.ach_print_buff = data[head_size:head_size + copy_len]
            record.u16_msg_len = copy_len

    @staticmethod
    def _parse_4g_record(data: bytes, record: SignalTraceStruct) -> None:
        """解析4G信令记录数据（大端序）
        记录结构: 24字节(基本头) + 20字节(T_SignalTraceInfo_LTE) = 44字节
        T_SignalTraceInfo_LTE:
          u16MsgType(2) u16CellID(2) u16GID(2) u16RNTI(2)
          u16MsgID(2) u16TransTime(2) u8Dir(1) u8SopReason(1)
          u16TaskID(2) u16SctpAsID(2) u16Res(2)
        """
        if len(data) < 44:
            raise ValueError("数据长度不足，无法解析4G记录")

        # 基本头部 (offset: 0-23, 共24字节) — 与5G相同
        u32_s_info_seq = struct.unpack_from(">I", data, 0)[0]
        u32_frm_inf = struct.unpack_from(">I", data, 4)[0]

        # 时间信息 (offset: 9-15)
        au8_year = data[9:11]
        u8_mon = data[11]
        u8_day = data[12]
        u8_hour = data[13]
        u8_min = data[14]
        u8_sec = data[15]

        # T_SignalTraceInfo_LTE (offset: 24-43, 共20字节)
        u16_msg_type = struct.unpack_from(">H", data, 24)[0]    # wDataId
        u16_cell_id = struct.unpack_from(">H", data, 26)[0]
        u16_gid = struct.unpack_from(">H", data, 28)[0]
        u16_rnti = struct.unpack_from(">H", data, 30)[0]
        u16_msg_id = struct.unpack_from(">H", data, 32)[0]     # wMsgName
        u16_trans_time = struct.unpack_from(">H", data, 34)[0]
        u8_dir = data[36]
        u8_sop_reason = data[37]
        u16_task_id = struct.unpack_from(">H", data, 38)[0]
        u16_sctp_as_id = struct.unpack_from(">H", data, 40)[0]
        # u16_res = struct.unpack_from(">H", data, 42)[0]  # 保留

        # 填充记录（基础字段）
        record.u16_cell_id = u16_cell_id
        record.u16_du_gid = 0          # 4G无du_gid
        record.u32_gid = u16_gid       # 4G用16位gid
        record.u32_interface = 0        # LTE无interface字段
        record.u16_msg_id = u16_msg_id
        record.u32_s_info_seq = u32_s_info_seq
        record.u32_sfn = u32_frm_inf

        # 填充4G特有字段
        record.u16_msg_type = u16_msg_type
        record.u16_rnti = u16_rnti
        record.u16_trans_time = u16_trans_time
        record.u8_sop_reason = u8_sop_reason
        record.u16_task_id = u16_task_id
        record.u16_sctp_as_id = u16_sctp_as_id

        # 4G接口类型默认为LTE (T_SignalTraceInfo_LTE不含interface字段)
        record.s_interface = "LTE"

        # 4G方向映射 (eNB视角)
        dir_map = {
            0: "eNB_SEND", 1: "eNB_RECEIVE", 2: "MME_SEND",
            3: "MME_RECEIVE", 4: "GW_SEND", 5: "GW_RECEIVE",
        }
        record.s_dir = dir_map.get(u8_dir, "UNKNOWN")

        # 解析帧号子帧号
        frame_no = u32_frm_inf // 10
        sub_frame_no = u32_frm_inf % 10
        record.s_fram_info = f"{frame_no}:{sub_frame_no}"

        # 解析上报时间（年份按大端序读取：高字节在前，无需字节交换）
        year = (au8_year[0] << 8) + au8_year[1]
        record.report_time = f"{year}-{u8_mon}-{u8_day}  {u8_hour}:{u8_min}:{u8_sec}"

        # 复制打印内容 (offset: 44之后)
        head_size = 44
        if len(data) > head_size:
            copy_len = min(len(data) - head_size, SIGNAL_TRACE_BUFF_LEN_MAX)
            record.ach_print_buff = data[head_size:head_size + copy_len]
            record.u16_msg_len = copy_len

    # ---------- 字段大小与值解析 ----------

    def get_field_size(self, field_type: str) -> int:
        """获取字段类型的大小"""
        type_map = {
            "C8": 1, "c8": 1,
            "U8": 1, "u8": 1,
            "U16": 2, "u16": 2,
            "U32": 4, "u32": 4,
            "U64": 8, "u64": 8,
        }
        if field_type in type_map:
            return type_map[field_type]
        # 查找结构体大小
        sinfo = self.struct_map.get(field_type)
        if sinfo:
            return sinfo.length
        return 4  # 默认4字节

    def _parse_field_value_for_json(self, payload: bytes, offset: int,
                                     field_type: str, prop: FieldProperty) -> str:
        """解析字段值并返回JSON格式的字符串"""
        if prop.b_is_array:
            return f'"\\"[数组] 维度:{prop.array_dim}\\""'

        # 位域特殊处理
        if prop.b_is_bit_region:
            bp = self.bit_region_prop
            if not bp.b_fir_bit_variable_flag:
                bp.b_fir_bit_variable_flag = True
                bp.offset_position = prop.field_mem_offset
                bp.bit_region_position = prop.bit_region_count
            else:
                if bp.offset_position == prop.field_mem_offset:
                    bp.bit_region_position += prop.bit_region_count
                else:
                    bp.offset_position = prop.field_mem_offset
                    bp.bit_region_position = prop.bit_region_count

        if offset >= len(payload):
            return '"\\"[越界]\\""'

        # 基本类型解析
        if field_type in ("C8", "c8"):
            value = payload[offset]
            if 32 <= value <= 126:
                return f'"{chr(value)}"'
            return str(value)

        if field_type in ("U8", "u8"):
            value = payload[offset]
            if prop.b_is_bit_region and 1 <= prop.bit_region_count <= 8:
                # 位域占据 [position-count, position) 区间, 正确做法: 右移并对count位掩码
                value = (value >> (self.bit_region_prop.bit_region_position - prop.bit_region_count)) \
                        & ((1 << prop.bit_region_count) - 1)
            return str(value)

        if field_type in ("U16", "u16"):
            if offset + 1 >= len(payload):
                return '"\\"[数据不足]\\""'
            value = struct.unpack_from("<H" if self.little_endian else ">H", payload, offset)[0]
            if prop.b_is_bit_region and 1 <= prop.bit_region_count <= 16:
                value = (value >> (self.bit_region_prop.bit_region_position - prop.bit_region_count)) \
                        & ((1 << prop.bit_region_count) - 1)
            return str(value)

        if field_type in ("U32", "u32"):
            if offset + 3 >= len(payload):
                return '"\\"[数据不足]\\""'
            value = struct.unpack_from("<I" if self.little_endian else ">I", payload, offset)[0]
            if prop.b_is_bit_region and 1 <= prop.bit_region_count <= 32:
                value = (value >> (self.bit_region_prop.bit_region_position - prop.bit_region_count)) \
                        & ((1 << prop.bit_region_count) - 1)
            return str(value)

        if field_type in ("U64", "u64"):
            if offset + 7 >= len(payload):
                return '"\\"[数据不足]\\""'
            value = struct.unpack_from("<Q" if self.little_endian else ">Q", payload, offset)[0]
            if prop.b_is_bit_region and 1 <= prop.bit_region_count <= 64:
                value = (value >> (self.bit_region_prop.bit_region_position - prop.bit_region_count)) \
                        & ((1 << prop.bit_region_count) - 1)
            return str(value)

        if field_type in ("S8", "s8"):
            value = struct.unpack_from("b", payload, offset)[0]
            if prop.b_is_bit_region and 1 <= prop.bit_region_count <= 8:
                value = self._extract_signed_bit(value, prop.bit_region_count)
            return str(value)

        if field_type in ("S16", "s16"):
            if offset + 1 >= len(payload):
                return '"\\"[数据不足]\\""'
            raw = struct.unpack_from("<H" if self.little_endian else ">H", payload, offset)[0]
            if raw >= 0x8000:
                value = raw - 0x10000  # 有符号
            else:
                value = raw
            if prop.b_is_bit_region and 1 <= prop.bit_region_count <= 16:
                value = self._extract_signed_bit(value, prop.bit_region_count)
            return str(value)

        if field_type in ("S32", "s32", "Int", "INT", "int"):
            if offset + 3 >= len(payload):
                return '"\\"[数据不足]\\""'
            raw = struct.unpack_from("<I" if self.little_endian else ">I", payload, offset)[0]
            if raw >= 0x80000000:
                value = raw - 0x100000000
            else:
                value = raw
            if prop.b_is_bit_region and 1 <= prop.bit_region_count <= 32:
                value = self._extract_signed_bit(value, prop.bit_region_count)
            return str(value)

        # 枚举类型
        enum_info = self.struct_map.get(field_type)
        if enum_info and enum_info.is_enum:
            enum_size = self.get_field_size(field_type)
            if offset + enum_size > len(payload):
                return '"\\"[数据不足]\\""'
            enum_value = 0
            if enum_size == 1:
                enum_value = payload[offset]
            elif enum_size == 2:
                enum_value = struct.unpack_from("<H" if self.little_endian else ">H", payload, offset)[0]
            elif enum_size == 4:
                enum_value = struct.unpack_from("<I" if self.little_endian else ">I", payload, offset)[0]

            # 查找枚举名称
            enum_name = str(enum_value)
            for f in enum_info.fields:
                if f.property.field_mem_offset == enum_value:
                    enum_name = f.name
                    break

            return f'"{enum_name} ({enum_value})"'

        # 普通结构体类型
        return f'"\\"[结构体] {field_type}\\""'

    def _extract_signed_bit(self, value: int, count: int) -> int:
        """有符号位域提取: 先按count位取无符号值, 再做符号扩展"""
        shift = self.bit_region_prop.bit_region_position - count
        u = (value >> shift) & ((1 << count) - 1)
        if count > 0 and (u & (1 << (count - 1))):
            u -= (1 << count)
        return u

    def _parse_field_value(self, payload: bytes, offset: int,
                            field_type: str, prop: FieldProperty) -> str:
        """解析字段值（用于控制台显示）"""
        if prop.b_is_array:
            return f"[数组] 维度:{prop.array_dim}"
        if offset >= len(payload):
            return "[越界]"

        type_map = {
            ("C8", "c8"): lambda: self._fmt_char(payload[offset]),
            ("U8", "u8"): lambda: f"0x{payload[offset]:02X} ({payload[offset]})",
            ("U16", "u16"): lambda: self._fmt_uint(payload, offset, 2),
            ("U32", "u32"): lambda: self._fmt_uint(payload, offset, 4),
            ("U64", "u64"): lambda: self._fmt_uint(payload, offset, 8),
        }
        for keys, func in type_map:
            if field_type in keys:
                return func()
        return f"[结构体] {field_type}"

    @staticmethod
    def _fmt_char(val: int) -> str:
        if 32 <= val <= 126:
            return f'"{chr(val)}"'
        return str(val)

    def _fmt_uint(self, payload: bytes, offset: int, size: int) -> str:
        if offset + size - 1 >= len(payload):
            return "[数据不足]"
        fmt_map = {2: ("<H", ">H", "04X"), 4: ("<I", ">I", "08X"), 8: ("<Q", ">Q", "016X")}
        le_fmt, be_fmt, hex_fmt = fmt_map[size]
        value = struct.unpack_from(le_fmt if self.little_endian else be_fmt, payload, offset)[0]
        return f"0x{value:0{hex_fmt[0:2]}{'X' if hex_fmt.endswith('X') else 'x'}} ({value})"

    # ---------- 结构体转JSON ----------

    def struct_to_json(self, struct_info: StructInfo, payload: bytes) -> str:
        """将结构体转换为JSON格式字符串"""
        # 重置位域属性
        self.bit_region_prop = BitRegionProperty()

        lines: List[str] = ["{"]

        for i, field in enumerate(struct_info.fields):
            field_name = field.name
            # 注意: 保留位域字段名中的 ":" (如 btScsCarrierInfoPresent:1), 不截断

            offset = field.property.field_mem_offset
            field_size = self.get_field_size(field.type)

            is_last = (i == len(struct_info.fields) - 1)

            if offset >= len(payload):
                lines.append(f'  "{field_name}": "[数据不足]"{"" if is_last else ","}')
            elif field.property.b_is_array:
                array_json = self._parse_array_field(payload, offset, field.type, field.property)
                lines.append(f'  "{field_name}": {array_json.rstrip()}{"" if is_last else ","}')
            else:
                if offset + field_size > len(payload):
                    lines.append(f'  "{field_name}": "[数据不足]"{"" if is_last else ","}')
                else:
                    # 检查嵌套结构体
                    nested_info = self.struct_map.get(field.type)
                    if nested_info and field_size <= len(payload) - offset:
                        if nested_info.is_enum:
                            value_str = self._parse_field_value_for_json(
                                payload, offset, field.type, field.property
                            )
                            lines.append(f'  "{field_name}": {value_str}{"" if is_last else ","}')
                        else:
                            nested_payload = payload[offset:offset + field_size]
                            nested_json = self.struct_to_json(nested_info, nested_payload).strip()
                            # 嵌套结构体递归会重置并改写bit_region_prop, 返回后需复位,
                            # 避免污染外层结构体后续位域字段的累计位位置
                            self.bit_region_prop = BitRegionProperty()
                            if nested_json.startswith("{") and nested_json.endswith("}"):
                                nested_json = nested_json[1:-1]
                            nested_lines = [ln for ln in nested_json.split("\n") if ln.strip()]
                            lines.append(f'  "{field_name}": {{')
                            for nl in nested_lines:
                                # 保留原始行尾逗号仅重新缩进，避免数组起始行([)被错误追加逗号
                                lines.append(f"    {nl.strip()}")
                            closing = "  }" + ("" if is_last else ",")
                            lines.append(closing)
                    else:
                        value_str = self._parse_field_value_for_json(
                            payload, offset, field.type, field.property
                        )
                        lines.append(f'  "{field_name}": {value_str}{"" if is_last else ","}')

        lines.append("}")
        return "\n".join(lines)

    def _parse_array_field(self, payload: bytes, offset: int,
                            field_type: str, prop: FieldProperty) -> str:
        """解析数组字段"""
        result = self._parse_array_recursive(payload, offset, field_type,
                                              prop.array_size, 0, "")
        return result.rstrip("\n")

    def _parse_array_recursive(self, payload: bytes, offset: int, field_type: str,
                                array_sizes: List[int], dim_index: int, indent: str) -> str:
        """递归解析多维数组"""
        lines: List[str] = ["["]

        max_elems = max(array_sizes[dim_index], 1)
        elem_size = self.get_field_size(field_type)

        # 计算子数组大小
        sub_array_size = elem_size
        for j in range(dim_index + 1, 5):
            if array_sizes[j] > 0:
                sub_array_size *= array_sizes[j]

        # 数组元素数量受 payload 实际长度限制，不再硬截断，
        # 避免 Sib1Msg/SiContent 等信令内容数组被截断导致数据不完整
        available_elems = min((len(payload) - offset) // max(sub_array_size, 1), max_elems)

        # 判断是否是最后一维
        is_last_dim = all(array_sizes[j] == 0 for j in range(dim_index + 1, 5))

        for i in range(available_elems):
            elem_offset = offset + i * sub_array_size
            if elem_offset + sub_array_size > len(payload):
                lines.append(f'{indent}    "[数据不足]"')
                break

            is_last_elem = (i == available_elems - 1)

            if is_last_dim:
                struct_info = self.struct_map.get(field_type)
                if struct_info:
                    if struct_info.is_enum:
                        val_str = self._parse_field_value_for_json(
                            payload, elem_offset, field_type, FieldProperty()
                        )
                        lines.append(f"{indent}    {val_str}{'' if is_last_elem else ','}")
                    else:
                        elem_payload = payload[elem_offset:elem_offset + elem_size]
                        nested_json = self.struct_to_json(struct_info, elem_payload).strip()
                        if nested_json.startswith("{") and nested_json.endswith("}"):
                            nested_json = nested_json[1:-1]
                        nested_lines = [ln for ln in nested_json.split("\n") if ln.strip()]
                        lines.append(f"{indent}    {{")
                        for nl in nested_lines:
                            # 保留原始行尾逗号仅重新缩进，避免结构体内数组字段([)被错误追加逗号
                            lines.append(f"{indent}      {nl.strip()}")
                        lines.append(f"{indent}    }}{'' if is_last_elem else ','}")
                else:
                    val_str = self._parse_field_value_for_json(
                        payload, elem_offset, field_type, FieldProperty()
                    )
                    lines.append(f"{indent}    {val_str}{'' if is_last_elem else ','}")
            else:
                sub_json = self._parse_array_recursive(
                    payload, elem_offset, field_type, array_sizes, dim_index + 1, indent + "    "
                ).rstrip("\n")
                lines.append(f"{sub_json}{'' if is_last_elem else ','}")

        lines.append(f"{indent}]")
        return "\n".join(lines)

    # ---------- 解码结果解析 ----------

    @staticmethod
    def _get_shrink_num(line: str) -> int:
        """获取缩进层级（每3个空格为一级）"""
        space_str = "   "
        if not line.startswith(space_str):
            return 0
        ret = 0
        rem = line
        while rem.startswith(space_str):
            ret += 1
            rem = rem[3:]
        return ret

    @staticmethod
    def _is_number(s: str) -> bool:
        """判断字符串是否为合法JSON数字。

        排除 '0000'/'0101' 这类位串(前导零在JSON中非法，直接输出会破坏JSON)。
        """
        s = s.strip()
        if not s:
            return False
        if not all(c == '-' or c.isdigit() for c in s):
            return False
        # 去掉符号后，长度>1且以0开头的是非法JSON数字(如 0000, 0101)
        body = s[1:] if s.startswith('-') else s
        if len(body) > 1 and body.startswith('0'):
            return False
        return True

    def parse_decode_result_to_json(self, decode_result: str) -> str:
        """将解码结果字符串解析为树状结构并转换为JSON"""
        lines = decode_result.split("\n")
        if not lines:
            return "{}"

        root_name = lines[0].rstrip("{").strip()
        if " " in root_name:
            root_name = root_name.split(" ")[0].strip()

        tree = TreeNode(name=root_name)
        stack: List[TreeNode] = [tree]

        for i in range(1, len(lines)):
            line = lines[i]
            trimmed = line.strip()
            if not trimmed:
                continue
            if trimmed == "}":
                if len(stack) > 1:
                    stack.pop()
                continue

            depth = self._get_shrink_num(line)
            while len(stack) > depth + 1:
                stack.pop()
            if not stack:
                stack = [tree]

            current_node = stack[-1]
            node_name = trimmed
            has_children = trimmed.endswith("{")
            if has_children:
                node_name = trimmed.rstrip("{").strip()

            key = node_name
            value = ""
            if " = " in node_name:
                parts = node_name.split(" = ", 1)
                key = parts[0].strip()
                value = parts[1].strip() if len(parts) > 1 else ""

            new_node = TreeNode(name=key, leaf_value=value)
            current_node.children.append(new_node)

            if has_children:
                stack.append(new_node)

        return self._tree_to_json(tree, 0)

    def parse_nas_text_to_json(self, nas_text: str) -> str:
        """将 nas-decode.dll(tshark -V)输出解析为JSON字符串。

        tshark -V 文本格式: 每级4空格缩进，行形如
            "Field: value"           -> 叶子(名=值)
            "Field:"                 -> 父节点(后续更深的缩进为子节点)
            "0000 .... = Desc: val"  -> 位域叶子(以 = 分段，左侧为位串、右侧含描述)
        首行为根节点(如 Non-Access-Stratum 5GS (NAS)PDU)。
        """
        lines = nas_text.splitlines()
        # 找到首个非空行作为根
        root_line = None
        start = 0
        for i, ln in enumerate(lines):
            if ln.strip():
                root_line = ln.strip()
                start = i + 1
                break
        if not root_line:
            return "{}"

        root_key = root_line.split(':', 1)[0].strip() if ':' in root_line else root_line.strip()
        root = TreeNode(name=root_key)
        # 栈保存 (层级, 节点): 只保留当前路径上的祖先容器
        stack: List[Tuple[int, TreeNode]] = [(-1, root)]

        def split_kv(text: str):
            """返回 (key, value, has_value)。兼容 ':' 与 'bits = desc'。"""
            text = text.strip()
            if ' = ' in text:
                # 位域行: "0000 .... = Spare: 0" -> key=整行去掉值后的描述, value=值
                left, right = text.split(' = ', 1)
                if ':' in right:
                    k, v = right.split(':', 1)
                    return (left + ' | ' + k.strip()).strip(), v.strip(), True
                return left.strip(), right.strip(), True
            if ':' in text:
                k, v = text.split(':', 1)
                if v.strip():
                    return k.strip(), v.strip(), True
                return k.strip(), "", False
            return text, "", False

        for ln in lines[start:]:
            if not ln.strip():
                continue
            # tshark 每级4空格; 也兼容日志里混入的其它空白行
            stripped_len = len(ln) - len(ln.lstrip(' '))
            depth = stripped_len // 4
            depth = max(depth, 0)
            # 弹出所有层级>=当前行的祖先，仅保留更浅的容器作为父节点
            while len(stack) > 1 and stack[-1][0] >= depth:
                stack.pop()
            parent = stack[-1][1]
            key, value, has_value = split_kv(ln)
            if not key:
                continue
            node = TreeNode(name=key, leaf_value=value if has_value else "")
            parent.children.append(node)
            if not has_value:
                # 无值的行视为容器，其更深缩进行是它的子节点
                stack.append((depth, node))

        return self._tree_to_json(root, 0)

    def _tree_to_json(self, node: TreeNode, indent: int) -> str:
        """将树转换为JSON字符串"""
        indent_str = "  " * indent

        if not node.children:
            if node.leaf_value:
                if self._is_number(node.leaf_value):
                    return node.leaf_value
                # 用 json.dumps 转义值，避免特殊字符破坏JSON
                return json.dumps(node.leaf_value)
            # 用 json.dumps 转义节点名（可能是含引号的hex dump行）
            return json.dumps(node.name)

        lines: List[str] = ["{"]
        for i, child in enumerate(node.children):
            is_last = (i == len(node.children) - 1)
            key = json.dumps(child.name)  # 转义key（hex dump行可能含双引号）
            if child.leaf_value:
                val = child.leaf_value
                if self._is_number(val):
                    lines.append(f'{indent_str}  {key}: {val}{"" if is_last else ","}')
                else:
                    lines.append(f'{indent_str}  {key}: {json.dumps(val)}{"" if is_last else ","}')
            elif not child.children:
                lines.append(f'{indent_str}  {key}: null{"" if is_last else ","}')
            else:
                lines.append(f'{indent_str}  {key}: {{')
                self._tree_to_json_content(child, lines, indent + 4)
                lines.append(f'{indent_str}  }}{"" if is_last else ","}')
        lines.append(f"{indent_str}}}")
        return "\n".join(lines)

    def _tree_to_json_content(self, node: TreeNode, buf_lines: List[str], indent: int) -> None:
        """将树子节点内容写入（不含外层括号）"""
        indent_str = "  " * indent
        for i, child in enumerate(node.children):
            is_last = (i == len(node.children) - 1)
            key = json.dumps(child.name)  # 转义key（hex dump行可能含双引号）
            if child.leaf_value:
                val = child.leaf_value
                if self._is_number(val):
                    buf_lines.append(f'{indent_str}{key}: {val}{"" if is_last else ","}')
                else:
                    buf_lines.append(f'{indent_str}{key}: {json.dumps(val)}{"" if is_last else ","}')
            elif not child.children:
                buf_lines.append(f'{indent_str}{key}: null{"" if is_last else ","}')
            else:
                buf_lines.append(f'{indent_str}{key}: {{')
                self._tree_to_json_content(child, buf_lines, indent + 2)
                buf_lines.append(f'{indent_str}}}{"" if is_last else ","}')

    # ---------- 信令记录JSON输出 ----------

    @staticmethod
    def _get_event_name(msg_id_map: Dict[int, SigEventInfo], msg_id: int) -> str:
        info = msg_id_map.get(msg_id)
        return info.name if info else "Unknown"

    @staticmethod
    def _get_struct_type(msg_id_map: Dict[int, SigEventInfo], msg_id: int) -> str:
        info = msg_id_map.get(msg_id)
        return info.struct_type if info else "Unknown"

    def parse_signal_record(self, decoder: Optional[ASN1Decoder],
                              record: SignalTraceStruct) -> str:
        """解析单条信令记录并返回JSON字符串"""
        msg_id = record.u16_msg_id
        msg_len = record.u16_msg_len
        payload = record.ach_print_buff
        is_4g = (record.mode == "4G")

        event_name = self._get_event_name(self.msg_id_map, msg_id)
        struct_type = self._get_struct_type(self.msg_id_map, msg_id)

        lines: List[str] = ["{"]
        lines.append(f'  "SN": {record.u32_s_info_seq},')
        lines.append(f'  "msg_id": {msg_id},')
        lines.append(f'  "msg_name": {json.dumps(event_name)},')
        lines.append(f'  "struct_type": {json.dumps(struct_type)},')
        lines.append(f'  "msg_len": {msg_len},')
        lines.append(f'  "report_time": {json.dumps(record.report_time)},')
        lines.append(f'  "cell_id": {record.u16_cell_id},')
        if is_4g:
            lines.append(f'  "gid": {record.u32_gid},')
            lines.append(f'  "rnti": {record.u16_rnti},')
            lines.append(f'  "mode": "4G",')
        else:
            lines.append(f'  "du_gid": {record.u16_du_gid},')
            lines.append(f'  "gid": {record.u32_gid},')
            lines.append(f'  "mode": "5G",')
        lines.append(f'  "interface": {json.dumps(record.s_interface)},')
        lines.append(f'  "direction": {json.dumps(record.s_dir)},')
        lines.append(f'  "frame_info": {json.dumps(record.s_fram_info)},')

        # 4G特有字段
        if is_4g:
            lines.append(f'  "msg_type": {record.u16_msg_type},')
            lines.append(f'  "trans_time": {record.u16_trans_time},')
            lines.append(f'  "sop_reason": {record.u8_sop_reason},')
            lines.append(f'  "task_id": {record.u16_task_id},')
            lines.append(f'  "sctp_as_id": {record.u16_sctp_as_id},')

        # 判断信令类型
        is_standard = (1000 <= msg_id <= 1999) or (msg_id >= 3000)
        is_nas = (struct_type == 'T_NAS_MESSAGE')

        if is_nas and decoder and decoder.has_nas_decoder():
            # 5G NAS 消息: 用 nas-decode.dll(基于Wireshark/tshark)解码
            lines.append('  "signal_type": "nas",')
            try:
                nas_text = decoder.decode_nas(payload)
                nas_json = self.parse_nas_text_to_json(nas_text).strip()
                if nas_json.startswith("{") and nas_json.endswith("}"):
                    nas_json = nas_json[1:-1]
                    inner_lines = [ln for ln in nas_json.split("\n") if ln.strip()]
                    lines.append('  "decoded_content": {')
                    for ln in inner_lines:
                        lines.append(f"    {ln.strip()}")
                    lines.append("  }")
                else:
                    lines.append(f'  "decoded_content": {json.dumps(nas_text)}')
            except Exception as e:
                lines.append(f'  "error": {json.dumps(str(e))}')
        elif is_standard:
            lines.append('  "signal_type": "standard",')
            if decoder:
                try:
                    output_stream = decoder.decode_message(struct_type, msg_len, payload)
                    decode_result = _bytes_to_string(output_stream)
                    json_str = self.parse_decode_result_to_json(decode_result).strip()

                    if json_str.startswith("{") and json_str.endswith("}"):
                        json_str = json_str[1:-1]
                        inner_lines = [ln for ln in json_str.split("\n") if ln.strip()]
                        lines.append('  "decoded_content": {')
                        for ln in inner_lines:
                            lines.append(f"    {ln.strip()}")
                        lines.append("  }")
                except Exception as e:
                    lines.append(f'  "error": {json.dumps(str(e))}')
            else:
                lines.append('  "error": "ASN1解码器未初始化"')
        else:
            lines.append('  "signal_type": "internal",')
            struct_info = self.struct_map.get(struct_type)
            if not struct_info:
                lines.append(f'  "error": {json.dumps(f"未找到结构体 {struct_type} 的定义")}')
            else:
                json_str = self.struct_to_json(struct_info, payload).strip()
                if json_str.startswith("{") and json_str.endswith("}"):
                    json_str = json_str[1:-1]
                    inner_lines = [ln for ln in json_str.split("\n") if ln.strip()]
                    lines.append('  "decoded_content": {')
                    for ln in inner_lines:
                        lines.append(f"    {ln.strip()}")
                    lines.append("  }")

                # SIG_L2_BCCH_MSG 信令额外解码 SIB1 ASN.1 内容（Sib1Msg），
                # 供校验 SIB1 中的 RedCap 等字段
                if event_name == 'SIG_L2_BCCH_MSG' and decoder:
                    try:
                        sib1_json = self._decode_sib1(decoder, struct_info, payload)
                        if sib1_json:
                            lines.append('  ,"sib1_decoded_content": {')
                            for ln in sib1_json.split("\n"):
                                if ln.strip():
                                    lines.append(f"    {ln.strip()}")
                            lines.append("  }")
                    except Exception as sib1_ex:
                        lines.append(f'  ,"sib1_decode_error": {json.dumps(str(sib1_ex))}')

        lines.append("}")
        return "\n".join(lines)

    def _decode_sib1(self, decoder: 'ASN1Decoder', struct_info: StructInfo,
                     payload: bytes) -> Optional[str]:
        """解码 SIG_L2_BCCH_MSG 中承载的 SIB1（Sib1Msg 原始字节），返回解码JSON内部内容。

        Returns:
            去掉外层花括号的解码JSON字符串；Sib1字段缺失或解码失败时返回None。
        """
        sib1_field = None
        for f in struct_info.fields:
            if f.type == 'sib1_info':
                sib1_field = f
                break
        if sib1_field is None:
            return None

        offset = sib1_field.property.field_mem_offset
        size = self.get_field_size(sib1_field.type)
        if offset + size > len(payload):
            return None

        sub_payload = payload[offset:offset + size]
        # sib1_info 结构: Sib1Period(0)/HFNStartPosition(1)/SIB1Len(2,U16)/Sib1Msg(4,U8[256])
        if len(sub_payload) < 6:
            return None
        sib1_len = struct.unpack_from("<H" if self.little_endian else ">H", sub_payload, 2)[0]
        if sib1_len <= 0:
            sib1_len = max(len(sub_payload) - 4, 0)
        sib1_bytes = sub_payload[4:4 + sib1_len]
        if not sib1_bytes:
            return None

        output_stream = decoder.decode_message('BCCH_DL_SCH_Message', len(sib1_bytes), sib1_bytes)
        decode_result = _bytes_to_string(output_stream)
        json_str = self.parse_decode_result_to_json(decode_result).strip()
        if json_str.startswith("{") and json_str.endswith("}"):
            return json_str[1:-1]
        return json_str


# ==================== ASN1 解码器 (DLL调用) ====================

class ASN1Decoder:
    """ASN1解码器 - 通过DLL调用解码标准信令"""

    # === 5G 结构体类型到解码函数的映射 ===
    STRUCT_TO_FUNC_5G = {
        "UL_CCCH_Message": "Decode_UL_CCCH_Message",
        "UL_CCCH1_Message": "Decode_UL_CCCH1_Message",
        "DL_CCCH_Message": "Decode_DL_CCCH_Message",
        "UL_DCCH_Message": "Decode_UL_DCCH_Message",
        "DL_DCCH_Message": "Decode_DL_DCCH_Message",
        "PCCH_Message": "Decode_PCCH_Message",
        "NGAP_PDU": "Decode_NG_AP_Message",
        "NG_AP_Message": "Decode_NG_AP_Message",
        "BCCH_BCH_Message": "Decode_BCCH_BCH_Message",
        "BCCH_DL_SCH_Message": "Decode_BCCH_DL_SCH_Message",
        "XNAP_PDU": "Decode_Xn_AP_Message",
        "Xn_AP_Message": "Decode_Xn_AP_Message",
        "F1AP_PDU": "Decode_F1_AP_Message",
        "F1_AP_Message": "Decode_F1_AP_Message",
        "SystemInformation_Message": "Decode_SystemInformation_Message",
        # UE侧抓包: SigTrace.xml 中 structtype 为 SystemInformation(无_Message后缀)
        "SystemInformation": "Decode_SystemInformation_Message",
        # UE侧抓包新增: SIB1 独立解码(SIG_SIB1_MSG, msg_id=1127)
        "Sib1_Message": "Decode_Sib1_Message",
        "UERadioAccessCapabilityInfo": "Decode_UERadioAccessCapabilityInfo",
        "UERadioAccessCapabilityInformation": "Decode_UERadioAccessCapabilityInfo",
        "UE_NR_Capability": "Decode_UE_NR_Capability",
        "CCCH_Message": "Decode_CCCH_Message",
        "DCCH_Message": "Decode_DCCH_Message",
        "RCCH_Message": "Decode_RCCH_Message",
    }

    # === 4G 结构体类型到解码函数的映射 ===
    # 注意: 4GAsn1.dll 中 RRC 函数名带 LTE_ 前缀
    STRUCT_TO_FUNC_4G = {
        "UL_CCCH_Message": "LTE_Decode_UL_CCCH_Message",
        "DL_CCCH_Message": "LTE_Decode_DL_CCCH_Message",
        "UL_DCCH_Message": "LTE_Decode_UL_DCCH_Message",
        "DL_DCCH_Message": "LTE_Decode_DL_DCCH_Message",
        "S1AP_PDU": "Decode_S1_AP_Message",
        "S1_AP_Message": "Decode_S1_AP_Message",
        "X2AP_PDU": "Decode_X2_AP_Message",
        "X2_AP_Message": "Decode_X2_AP_Message",
        "BCCH_BCH_Message": "LTE_Decode_BCCH_BCH_Message",
        "BCCH_DL_SCH_Message": "LTE_Decode_BCCH_DL_SCH_Message",
        "CCCH_Message": "Decode_CCCH_Message",
        "DCCH_Message": "Decode_DCCH_Message",
        "RCCH_Message": "Decode_RCCH_Message",
    }

    # 5G LTEAsn1.dll 函数名列表
    LTE_5G_FUNC_NAMES = [
        "Decode_UL_CCCH_Message",
        "Decode_UL_CCCH1_Message",
        "Decode_DL_CCCH_Message",
        "Decode_UL_DCCH_Message",
        "Decode_DL_DCCH_Message",
        "Decode_PCCH_Message",
        "Decode_NG_AP_Message",
        "Decode_BCCH_BCH_Message",
        "Decode_BCCH_DL_SCH_Message",
        "Decode_Xn_AP_Message",
        "Decode_F1_AP_Message",
        "Decode_SystemInformation_Message",
        # UE侧新增DLL导出函数
        "Decode_Sib1_Message",
        "Decode_UERadioAccessCapabilityInfo",
        "Decode_UE_NR_Capability",
    ]

    # 4G 4GAsn1.dll 函数名列表（RRC函数带LTE_前缀，S1/X2不带）
    LTE_4G_FUNC_NAMES = [
        "LTE_Decode_UL_CCCH_Message",
        "LTE_Decode_DL_CCCH_Message",
        "LTE_Decode_UL_DCCH_Message",
        "LTE_Decode_DL_DCCH_Message",
        "Decode_S1_AP_Message",
        "Decode_X2_AP_Message",
        "LTE_Decode_BCCH_BCH_Message",
        "LTE_Decode_BCCH_DL_SCH_Message",
    ]

    MESH_FUNC_NAMES = [
        "Decode_CCCH_Message",
        "Decode_DCCH_Message",
        "Decode_RCCH_Message",
    ]

    OUTPUT_BUFFER_SIZE = 4096000

    def __init__(self, mode: str = "5G", silent_nas: bool = True):
        self._mode: str = mode.upper()
        self._lte_dll: Optional[ctypes.CDLL] = None
        self._mesh_dll: Optional[ctypes.CDLL] = None
        self._nas_dll: Optional[ctypes.CDLL] = None
        self._nas_decode = None
        self._lib_dir: str = ""  # xtraceLib目录, NAS解码需以此为cwd
        self._silent_nas = silent_nas  # 是否静默nas-decode.dll的C层调试输出
        self._functions: Dict = {}
        # 根据mode选择结构体映射和DLL
        if self._mode == "4G":
            self._struct_to_func = dict(self.STRUCT_TO_FUNC_4G)
            self._lte_func_names = list(self.LTE_4G_FUNC_NAMES)
            self._dll_name = "4GAsn1.dll"
        else:
            self._struct_to_func = dict(self.STRUCT_TO_FUNC_5G)
            self._lte_func_names = list(self.LTE_5G_FUNC_NAMES)
            self._dll_name = "LTEAsn1.dll"

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False

    @staticmethod
    def _discover_dll_decode_exports(dll_path: str) -> List[str]:
        """解析PE导出表，返回DLL里所有 Decode_ 开头的导出函数名。

        用于自动兼容UE侧DLL新增的解码函数(如 Decode_Sib1_Message)。
        解析失败返回空列表，由调用方回退到静态函数名列表。
        """
        try:
            import struct as _struct
            with open(dll_path, 'rb') as f:
                d = f.read()
            pe_off = _struct.unpack_from('<I', d, 0x3c)[0]
            if d[pe_off:pe_off + 4] != b'PE\x00\x00':
                return []
            coff = pe_off + 4
            nsec = _struct.unpack_from('<H', d, coff + 2)[0]
            opt_size = _struct.unpack_from('<H', d, coff + 16)[0]
            opt = coff + 20
            magic = _struct.unpack_from('<H', d, opt)[0]
            dd = opt + (96 if magic == 0x10b else 112)
            exp_rva = _struct.unpack_from('<I', d, dd)[0]
            if not exp_rva:
                return []
            sections = []
            sec_off = opt + opt_size
            for i in range(nsec):
                s = sec_off + i * 40
                vs, va, rs, ra = _struct.unpack_from('<IIII', d, s + 8)
                sections.append((va, vs, ra, rs))

            def rva_to_off(rva):
                for va, vs, ra, rs in sections:
                    if va <= rva < va + max(vs, rs):
                        return ra + (rva - va)
                return None

            exp = rva_to_off(exp_rva)
            if exp is None:
                return []
            n_names = _struct.unpack_from('<I', d, exp + 24)[0]
            addr_names = _struct.unpack_from('<I', d, exp + 32)[0]
            names_off = rva_to_off(addr_names)
            if names_off is None:
                return []
            result = []
            for i in range(n_names):
                name_rva = _struct.unpack_from('<I', d, names_off + i * 4)[0]
                noff = rva_to_off(name_rva)
                if noff is None:
                    continue
                end = d.find(b'\x00', noff)
                if end < 0:
                    continue
                name = d[noff:end].decode('utf-8', errors='replace')
                if name.startswith('Decode_') or name.startswith('LTE_Decode_'):
                    result.append(name)
            return result
        except Exception as e:
            print(f"警告: 解析DLL导出函数失败({dll_path}): {e}")
            return []

    def load(self) -> bool:
        """加载DLL并获取函数指针"""
        import platform
        if platform.system() != "Windows":
            print("警告: ASN1解码器仅在Windows平台可用")
            return False

        # 使用脚本所在目录的绝对路径加载DLL
#         script_dir = os.path.dirname(os.path.abspath(__file__))
        script_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'xtraceLib')
        self._lib_dir = script_dir
        lte_dll_path = os.path.join(script_dir, self._dll_name)
        mesh_dll_path = os.path.join(script_dir, "MeshAsn1.dll")

        # 尝试加载 nas-decode.dll (5G NAS消息解码, 调用Wireshark/tshark)
        nas_dll_path = os.path.join(script_dir, "nas-decode.dll")
        if os.path.isfile(nas_dll_path):
            try:
                self._nas_dll = ctypes.CDLL(nas_dll_path)
                proc = getattr(self._nas_dll, 'nas_decode')
                proc.argtypes = [ctypes.c_uint16, ctypes.POINTER(ctypes.c_byte),
                                 ctypes.POINTER(ctypes.c_byte)]
                proc.restype = ctypes.c_int32
                self._nas_decode = proc
                print(f"NAS解码器加载成功: {nas_dll_path}")
            except Exception as e:
                print(f"警告: 加载nas-decode.dll失败: {e}")
        else:
            print(f"警告: 未找到nas-decode.dll({nas_dll_path})，T_NAS_MESSAGE将无法解码")

        print(f"LTE DLL路径({self._mode}模式): {lte_dll_path}")

        try:
            self._lte_dll = ctypes.CDLL(lte_dll_path)
        except OSError as e:
            print(f"警告: 加载{self._dll_name}失败: {e}")
            return False

        # 静态列表 + 动态发现DLL导出，自动兼容DLL新增的解码函数
        lte_func_names = list(self._lte_func_names)
        for name in self._discover_dll_decode_exports(lte_dll_path):
            if name not in lte_func_names and name not in self.MESH_FUNC_NAMES:
                lte_func_names.append(name)
        for func_name in lte_func_names:
            try:
                proc = getattr(self._lte_dll, func_name)
                proc.argtypes = [ctypes.c_uint16, ctypes.POINTER(ctypes.c_byte),
                                 ctypes.POINTER(ctypes.c_byte)]
                proc.restype = ctypes.c_int32
                self._functions[func_name] = proc
            except AttributeError:
                # 动态发现的函数名未必都在此DLL中(可能属于Mesh等)，静默跳过
                if func_name in self._lte_func_names:
                    print(f"警告: 找不到函数 {func_name}")

        # 尝试加载MeshAsn1.dll
        print(f"Mesh DLL路径: {mesh_dll_path}")
        try:
            self._mesh_dll = ctypes.CDLL(mesh_dll_path)
            for func_name in self.MESH_FUNC_NAMES:
                try:
                    proc = getattr(self._mesh_dll, func_name)
                    proc.argtypes = [ctypes.c_uint16, ctypes.POINTER(ctypes.c_byte),
                                     ctypes.POINTER(ctypes.c_byte)]
                    proc.restype = ctypes.c_int32
                    self._functions[func_name] = proc
                except AttributeError:
                    print(f"警告: 找不到函数 {func_name}")
        except OSError as e:
            print(f"警告: 加载MeshAsn1.dll失败: {e}（如果不需要解析信令则可忽略）")

        return len(self._functions) > 0

    def release(self) -> None:
        """释放DLL句柄

        注意: 仅将CDLL对象置为None并不会真正卸载DLL，
        Windows下DLL会一直驻留内存并锁定文件，导致连续解析时
        再次加载或覆盖(xtraceLib目录中的)同一个DLL失败。
        因此需要调用 FreeLibrary 主动卸载。
        """
        for dll in (self._mesh_dll, self._lte_dll, self._nas_dll):
            if dll is not None:
                try:
                    ctypes.windll.kernel32.FreeLibrary(dll._handle)
                except Exception as e:
                    print(f"警告: 释放DLL失败: {e}")
        self._lte_dll = None
        self._mesh_dll = None
        self._nas_dll = None
        self._nas_decode = None
        self._functions.clear()

    # XML中4G标准信令结构体可能带前缀，需要映射到解码器已知的结构体类型
    # 例如: LTESig.xml 使用 TS1AP_S1AP_PDU / TX2AP_X2AP_PDU，而解码器映射表为 S1AP_PDU / X2AP_PDU
    STRUCT_TYPE_PREFIXES = ("TS1AP_", "TX2AP_")

    def _resolve_struct_type(self, struct_type: str) -> str:
        """将XML中的结构体类型归一化为解码器已知的结构体类型。
        直接命中返回原值；若带 TS1AP_/TX2AP_ 等前缀则去掉前缀后匹配。
        """
        if struct_type in self._struct_to_func:
            return struct_type
        for prefix in self.STRUCT_TYPE_PREFIXES:
            if struct_type.startswith(prefix):
                stripped = struct_type[len(prefix):]
                if stripped in self._struct_to_func:
                    return stripped
        return struct_type

    def decode_message(self, struct_type: str, msg_len: int,
                        input_stream: bytes) -> bytes:
        """调用解码函数"""
        resolved_type = self._resolve_struct_type(struct_type)
        func_name = self._struct_to_func.get(resolved_type)
        if not func_name:
            # 约定兜底: DLL解码函数通常为 Decode_<结构体类型>(4G为 LTE_Decode_<...>)，
            # 这样即使XML新增了结构体类型，只要DLL导出了对应函数即可自动解码
            for candidate in ('Decode_' + resolved_type, 'LTE_Decode_' + resolved_type):
                if candidate in self._functions:
                    func_name = candidate
                    break
        if not func_name:
            raise RuntimeError(f"不支持的结构体类型: {struct_type}")

        proc = self._functions.get(func_name)
        if not proc:
            raise RuntimeError(f"未找到解码函数: {func_name}")

        # 准备输入和输出缓冲区
        input_buf = (ctypes.c_byte * len(input_stream))(*input_stream)
        output_buf = (ctypes.c_byte * self.OUTPUT_BUFFER_SIZE)()

        ret = proc(ctypes.c_uint16(msg_len), input_buf, output_buf)
        if ret != 0:
            raise RuntimeError(f"调用解码函数 {func_name} 失败，返回码: {ret}")

        return bytes(output_buf)

    def decode_nas(self, payload: bytes) -> str:
        r"""调用 nas-decode.dll 解码 5G NAS 消息，返回 tshark 文本。

        nas-decode.dll 内部用相对路径 .\Wireshark\ 调用 text2pcap/tshark，
        因此调用期间必须把 cwd 切到 xtraceLib，结束后恢复，避免影响其他逻辑。
        """
        if self._nas_decode is None:
            raise RuntimeError("NAS解码器未初始化(nas-decode.dll未加载)")

        input_buf = (ctypes.c_byte * len(payload))(*payload)
        output_buf = (ctypes.c_byte * self.OUTPUT_BUFFER_SIZE)()
        # DLL每次调用会在xtraceLib生成中间文件(*-decode_result.log等)，调用前后对比清理
        work_dir = self._lib_dir or os.getcwd()
        before = set(os.listdir(work_dir)) if os.path.isdir(work_dir) else set()
        prev_cwd = os.getcwd()
        saved_stdout = None
        devnull_fd = None
        try:
            if self._silent_nas:
                # DLL用C层printf(宽字符)输出调试信息到fd1, Python的sys.stdout重定向无效,
                # 需在OS层把fd1临时指到devnull; 解码结果由output_buf返回, 不受影响
                try:
                    sys.stdout.flush()
                    saved_stdout = os.dup(1)
                    devnull_fd = os.open(os.devnull, os.O_WRONLY)
                    os.dup2(devnull_fd, 1)
                except Exception:
                    # 重定向失败则保持原样, 不影响解码功能
                    if saved_stdout is not None:
                        try:
                            os.close(saved_stdout)
                        except Exception:
                            pass
                        saved_stdout = None
            if self._lib_dir:
                os.chdir(self._lib_dir)
            ret = self._nas_decode(ctypes.c_uint16(len(payload)), input_buf, output_buf)
        finally:
            os.chdir(prev_cwd)
            if saved_stdout is not None:
                try:
                    os.dup2(saved_stdout, 1)
                except Exception:
                    pass
                if devnull_fd is not None:
                    try:
                        os.close(devnull_fd)
                    except Exception:
                        pass
                try:
                    os.close(saved_stdout)
                except Exception:
                    pass
        self._clean_nas_artifacts(work_dir, before)
        if ret != 0:
            raise RuntimeError(f"调用 nas_decode 失败，返回码: {ret}")

        raw = bytes(output_buf)
        null_pos = raw.find(b'\x00')
        text = raw[:null_pos] if null_pos >= 0 else raw
        return text.decode('utf-8', errors='replace')

    @staticmethod
    def _clean_nas_artifacts(work_dir: str, before: Set[str]) -> None:
        """删除 nas-decode.dll 本次调用新增的中间产物。"""
        try:
            if not os.path.isdir(work_dir):
                return
            for name in set(os.listdir(work_dir)) - before:
                if name.endswith('-decode_result.log') or name in (
                        'textdata.txt', 'decode_temp.pcap', 'decode_output_temp.txt',
                        'print_decode_output.txt', 'nas_decode-log.txt',
                        'format_file_for_text2pcap-log.txt'):
                    try:
                        os.remove(os.path.join(work_dir, name))
                    except Exception:
                        pass
        except Exception:
            pass

    def has_nas_decoder(self) -> bool:
        return self._nas_decode is not None


# ==================== 辅助函数 ====================

def _bytes_to_string(data: bytes) -> str:
    """将字节切片转换为字符串，截断至首个null字节"""
    null_pos = data.find(b'\x00')
    if null_pos >= 0:
        return data[:null_pos].decode("utf-8", errors="replace")
    return data.decode("utf-8", errors="replace")


def display_hex_dump(data: bytes, max_len: int) -> None:
    """显示十六进制dump"""
    data_len = min(max_len, len(data))
    for i in range(0, data_len, 16):
        offset = f"{i:04X}"
        hex_part = ""
        ascii_part = ""
        for j in range(16):
            if i + j < data_len:
                hex_part += f"{data[i + j]:02X} "
                ch = data[i + j]
                ascii_part += chr(ch) if 32 <= ch <= 126 else "."
            else:
                hex_part += "   "
            if j == 7:
                hex_part += " "
        print(f"{offset}  {hex_part} |{ascii_part}|")


def sanitize_filename(s: str) -> str:
    """清理文件名中的非法字符"""
    s = s.replace(" ", "_")
    s = s.replace(":", "-")
    s = s.replace("/", "-")
    s = s.replace("\\", "-")
    return s


# ==================== 便捷函数 ====================

def parse_xst_file(file_path: str, mode: str = "5G") -> XSTFile:
    """便捷函数：直接解析XST文件"""
    parser = XSTParser(file_path, mode)
    return parser.parse()


# ==================== 核心解析函数 ====================

def xtraceLogAnalyze(file_path: str, mode: str = "5G", filterList=None,
                     silent_nas: bool = True) -> dict:
    """解析XST信令文件，将每条信令保存为单独的JSON文件（可被其他模块调用）

    Args:
        file_path: XST文件路径
        mode: 解析模式 "4G" 或 "5G"
        filterList: 信令名称过滤列表，None表示不过滤全部解析。
                    例如: ["INITIAL_CONTEXT_SETUP_REQUEST", "RRCSetup"]
                    大小写不敏感。
        silent_nas: 是否静默 nas-decode.dll 的C层调试输出(默认True)。
                    排查NAS解码问题时可设为False查看DLL日志。

    Returns:
        dict: {
            "success": bool,          # 整体是否成功
            "success_count": int,     # 成功解析的信令数
            "fail_count": int,        # 失败的信令数
            "skip_count": int,        # 过滤跳过的信令数
            "total_count": int,       # 总记录数
            "output_dir": str,        # 输出目录路径
            "mode": str,              # 解析模式
            "mode_label": str,        # 模式标签 (4G-LTE / 5G-NR)
            "error": str or None,     # 错误信息
        }
    """
    # 参数校验
    if mode not in ("4G", "5G"):
        return {"success": False, "error": f"无效的mode '{mode}'，仅支持 4G 或 5G",
                "success_count": 0, "fail_count": 0, "skip_count": 0,
                "total_count": 0, "output_dir": "", "mode": mode, "mode_label": ""}

    # 将filterList转换为大写集合用于匹配
    msg_filter = None
    if filterList:
        msg_filter = set(str(n).strip().upper() for n in filterList if str(n).strip())

    mode_label = "4G-LTE" if mode == "4G" else "5G-NR"
    print(f"开始解析文件: {file_path} (模式: {mode_label})")

    # 创建输出目录
    file_dir = os.path.dirname(file_path) or "."
    file_name = os.path.basename(file_path)
    base_name = os.path.splitext(file_name)[0]
    output_dir = os.path.join(file_dir, base_name)

    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as e:
        return {"success": False, "error": f"创建输出目录失败: {e}",
                "success_count": 0, "fail_count": 0, "skip_count": 0,
                "total_count": 0, "output_dir": "", "mode": mode, "mode_label": mode_label}
    print(f"创建输出目录: {output_dir}")

    # 解析文件
    parser = XSTParser(file_path, mode)
    parser.little_endian = True  # 设置为小端字节序

    try:
        xst_file = parser.parse()
    except Exception as e:
        return {"success": False, "error": f"解析失败: {e}",
                "success_count": 0, "fail_count": 0, "skip_count": 0,
                "total_count": 0, "output_dir": output_dir, "mode": mode, "mode_label": mode_label}

    # 根据mode获取记录列表
    if mode == "4G":
        records = xst_file.records_4g
    else:
        records = xst_file.records_5g

    print()
    print("解析完成!")
    print("文件类型:", "自软件格式" if xst_file.is_self_software_file else "网管格式")
    print(f"原始记录数: {len(xst_file.records)}")
    print(f"{mode_label}解析记录数: {len(records)}")
    if parser.msg_id_map:
        print(f"加载的事件ID映射数: {len(parser.msg_id_map)}")

    print()
    print(f"--- {mode_label}信令记录概览 ---")
    print(f"总计: {len(records)} 条{mode_label}信令记录")

    if msg_filter:
        print(f"过滤条件: 仅解析 {', '.join(sorted(msg_filter))}")

    # 初始化ASN1解码器
    decoder = None
    if records:
        decoder = ASN1Decoder(mode, silent_nas=silent_nas)
        if decoder.load():
            print(f"ASN1解码器初始化成功 (DLL: {decoder._dll_name})")
        else:
            print("ASN1解码器初始化失败（标准信令将无法解码）")

    success_count = 0
    fail_count = 0
    skip_count = 0

    try:
        total = len(records)
        for i, record in enumerate(records):
            event_name = parser._get_event_name(parser.msg_id_map, record.u16_msg_id)

            # 信令名称过滤
            if msg_filter:
                if event_name.upper() not in msg_filter:
                    skip_count += 1
                    continue

            clean_time = sanitize_filename(record.report_time)
            clean_event = sanitize_filename(event_name)

            if mode == "4G":
                json_file_name = (
                    f"{i}-{clean_time}-{record.u16_cell_id}-"
                    f"{record.u32_gid}-{record.u16_rnti}-{clean_event}.json"
                )
            else:
                json_file_name = (
                    f"{i}-{clean_time}-{record.u16_cell_id}-"
                    f"{record.u32_gid}-{record.u16_du_gid}-{clean_event}.json"
                )
            json_path = os.path.join(output_dir, json_file_name)

            try:
                json_content = parser.parse_signal_record(decoder, record)
                with open(json_path, "w", encoding="utf-8") as f:
                    f.write(json_content)
                success_count += 1
            except Exception as e:
                print(f"写入文件失败 {json_path}: {e}")
                fail_count += 1

            # 进度提示
            if (success_count + fail_count + skip_count) % 100 == 0:
                print(f"进度: {i + 1}/{total}")

        print()
        print("解析完成!")
        print(f"成功: {success_count} 条信令")
        print(f"失败: {fail_count} 条信令")
        if msg_filter:
            print(f"过滤跳过: {skip_count} 条信令")
        print(f"输出目录: {output_dir}")

    finally:
        if decoder:
            decoder.release()

    return {
        "success": True,
        "error": None,
        "success_count": success_count,
        "fail_count": fail_count,
        "skip_count": skip_count,
        "total_count": len(records),
        "output_dir": output_dir,
        "mode": mode,
        "mode_label": mode_label,
    }


# ==================== 命令行入口 ====================

def main():
    """命令行入口：解析参数并调用 xtraceLogAnalyze"""
    file_path = None
    mode = "5G"
    filter_list = None  # 命令行输入的过滤列表

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("--mode", "-m"):
            if i + 1 < len(args):
                mode = args[i + 1].upper()
                i += 2
            else:
                print("错误: --mode 需要参数 (4G 或 5G)")
                return
        elif arg.startswith("--mode="):
            mode = arg.split("=", 1)[1].upper()
            i += 1
        elif arg in ("--filter", "-f"):
            if i + 1 < len(args):
                filter_list = [n.strip() for n in args[i + 1].split(",") if n.strip()]
                i += 2
            else:
                print("错误: --filter 需要参数 (信令名称，多个用逗号分隔)")
                return
        elif arg.startswith("--filter="):
            filter_val = arg.split("=", 1)[1]
            filter_list = [n.strip() for n in filter_val.split(",") if n.strip()]
            i += 1
        elif not arg.startswith("-"):
            file_path = arg
            i += 1
        else:
            i += 1

    if mode not in ("4G", "5G"):
        print(f"错误: 无效的mode '{mode}'，仅支持 4G 或 5G")
        return

    if file_path is None:
        print("用法: python xst_parser_5G.py <文件路径> [--mode 4G|5G] [--filter 信令名称]")
        print("示例:")
        print("  python xst_parser_5G.py input.xst                                    # 默认5G模式，解析全部")
        print("  python xst_parser_5G.py input.xst --mode 4G                          # 4G模式")
        print("  python xst_parser_5G.py input.xst --filter INITIAL_CONTEXT_SETUP_REQUEST  # 只解析指定信令")
        print("  python xst_parser_5G.py input.xst -f RRCSetup,RRCSecurityModeCommand  # 只解析多个信令")
        print()
        print("说明:")
        print("  4G/5G信令解析器，将每条信令保存为单独的JSON文件")
        print("  --filter/-f: 只解析指定信令名称，多个用逗号分隔，大小写不敏感")
        print("  5G模式需要LTEAsn1.dll + SigTrace.xml (默认)")
        print("  4G模式需要4GAsn1.dll + LTESig.xml")
        print("  两种模式都需要MeshAsn1.dll来解析Mesh信令")
        return

    # 调用核心解析函数
    result = xtraceLogAnalyze(file_path, mode, filter_list)
    if not result["success"]:
        print(f"错误: {result['error']}")


if __name__ == "__main__":
    file_path = r'F:\tmp\traceLog\20261117170653.xst'
    mode = '5G'
    filter_list = ['SIG_BCCH_DL_SCH_MSG', 'SIG_SYSTEMINFO_MSG', 'Registration request', 
                'Identity request', 'Identity response', 'Authentication request', 'Authentication response',
                'Security mode command', 'Security mode complete', 'Registration accept',
                'Registration complete', 'PDU session establishment request']
    result = xtraceLogAnalyze(file_path, mode, filterList=filter_list)
    print(result)