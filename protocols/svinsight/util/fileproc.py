#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fileproc.go 的 Python 实现
将二进制 .ei 文件转换为 CSV 格式

用法:
  python fileproc.py <input.ei file> [instance_id]
"""

import os
import struct
import sys

# ==================== 常量定义 ====================
MAX_VAR_IN_STRUCT = 200
MAX_TASK_MSG_NUM = 50
MAX_REPORT_TLV_KEY_NUM = 1024
MAX_EI_FILE_NAME_LEN = 320
MAX_EI_LOCAL_TIME_LEN = 16
MAX_NUM_TAGID = 100
MAX_NUM_COLUM = 1024
MAX_RECORD_BUFFER_SIZE = 5 * MAX_NUM_COLUM * MAX_NUM_TAGID
RecordValueBufferSize = 1024
FileTypeMagic = 0xAFAFAFAF

# ==================== 结构体二进制长度 ====================
# 与 Go 中 unsafe.Sizeof() 结果一致（已含对齐填充）
SIZEOF_EI_MSG_INFO_SAVE_HEAD = 168
SIZEOF_TLV_INFO_SAVE_HEAD = 52
SIZEOF_VAR_INFO = 56
SIZEOF_EI_MSG_HEAD = 16
SIZEOF_EI_TLV_HEAD = 8


# ==================== 辅助函数 ====================
def bytes_to_string(b: bytes) -> str:
    """字节数组转字符串，遇到 0x00 截断（等价于 Go 的 string([]byte)）"""
    end = len(b)
    for i, c in enumerate(b):
        if c == 0:
            end = i
            break
    return b[:end].decode('latin-1')


def str_trim(s: str) -> str:
    return s.strip()


def int2ip_str(ip_value: int) -> str:
    return "%d.%d.%d.%d" % (
        (ip_value & 0xFF000000) >> 24,
        (ip_value & 0x00FF0000) >> 16,
        (ip_value & 0x0000FF00) >> 8,
        ip_value & 0x000000FF,
    )


def extract_bit_field8(val: int, bit_pos: int, bit_count: int) -> int:
    if bit_count == 0 or bit_count > 8:
        return val
    mask = (1 << bit_count) - 1
    return (val >> (8 - bit_pos)) & mask


def extract_bit_field16(val: int, bit_pos: int, bit_count: int) -> int:
    if bit_count == 0 or bit_count > 16:
        return val
    mask = (1 << bit_count) - 1
    return (val >> (16 - bit_pos)) & mask


def extract_bit_field32(val: int, bit_pos: int, bit_count: int) -> int:
    if bit_count == 0 or bit_count > 32:
        return val
    mask = (1 << bit_count) - 1
    return (val >> (32 - bit_pos)) & mask


def extract_bit_field64(val: int, bit_pos: int, bit_count: int) -> int:
    if bit_count == 0 or bit_count > 64:
        return val
    mask = (1 << bit_count) - 1
    return (val >> (64 - bit_pos)) & mask


# ==================== 主转换函数 ====================
def convert_file(ch_file_name: str, enb_instance_num: int):
    """将二进制 EI 文件转换为 CSV 格式"""
    if ch_file_name == "":
        raise ValueError("chFileName is null")

    # 任务配置数据（等效于 Go 的 T_TaskCfgData）
    task_data = {
        'str_cur_cvt_file_name': ch_file_name,
        'u8_cvt_progress': 0,
        'u8_tlv_key_index_list': [0] * MAX_REPORT_TLV_KEY_NUM,
        'u16_ei_cfg_msg_num': 0,
    }

    # CSV 文件名
    str_csv_file_name = ch_file_name.replace(".ei", ".csv", 1)

    # 读取二进制文件
    with open(ch_file_name, "rb") as f:
        g_data = f.read()

    u32_file_total_size = len(g_data)
    if u32_file_total_size < 12:
        raise ValueError("file size too small")

    # 解析文件头
    data_ptr = 0
    u32_file_type = struct.unpack_from("<I", g_data, data_ptr)[0]
    data_ptr += 4
    _ = struct.unpack_from("<I", g_data, data_ptr)[0]  # 跳过 4 字节
    data_ptr += 4

    if u32_file_type != FileTypeMagic:
        raise ValueError("file type 0x%08X is not 0xAFAFAFAF" % u32_file_type)

    _ = struct.unpack_from("<I", g_data, data_ptr)[0]  # 跳过 4 字节
    data_ptr += 4

    # ---- 读取 EI 消息配置头 T_EiMsgInfoSaveHead ----
    t_ei_msg_info_save_head = {
        'u32_msg_id': 0, 'str_msg_name': b'', 'u16_dsp_id': 0, 'u16_core_id': 0,
        'u16_cell_num': 0, 'au16_cell_id': [0] * 4, 'u16_ue_num': 0,
        'au16_ueg_id': [0] * 40, 'u16_tag_num': 0, 'u16_resv': 0,
    }
    head_bytes = g_data[data_ptr:data_ptr + SIZEOF_EI_MSG_INFO_SAVE_HEAD]
    data_ptr += len(head_bytes)

    t_ei_msg_info_save_head['u32_msg_id'] = struct.unpack_from("<I", head_bytes, 0)[0]
    t_ei_msg_info_save_head['str_msg_name'] = head_bytes[4:68]
    t_ei_msg_info_save_head['u16_dsp_id'] = struct.unpack_from("<H", head_bytes, 68)[0]
    t_ei_msg_info_save_head['u16_core_id'] = struct.unpack_from("<H", head_bytes, 70)[0]
    t_ei_msg_info_save_head['u16_cell_num'] = struct.unpack_from("<H", head_bytes, 72)[0]
    for i in range(4):
        t_ei_msg_info_save_head['au16_cell_id'][i] = struct.unpack_from("<H", head_bytes, 74 + i * 2)[0]
    t_ei_msg_info_save_head['u16_ue_num'] = struct.unpack_from("<H", head_bytes, 82)[0]
    for i in range(40):
        t_ei_msg_info_save_head['au16_ueg_id'][i] = struct.unpack_from("<H", head_bytes, 84 + i * 2)[0]
    t_ei_msg_info_save_head['u16_tag_num'] = struct.unpack_from("<H", head_bytes, 164)[0]
    t_ei_msg_info_save_head['u16_resv'] = struct.unpack_from("<H", head_bytes, 166)[0]

    # ---- 构建完整 T_EiMsgInfo ----
    pt_ei_msg_info = {
        'u32_msg_id': t_ei_msg_info_save_head['u32_msg_id'],
        'u16_dsp_id': t_ei_msg_info_save_head['u16_dsp_id'],
        'u16_core_id': t_ei_msg_info_save_head['u16_core_id'],
        'u16_cell_num': t_ei_msg_info_save_head['u16_cell_num'],
        'u16_ue_num': t_ei_msg_info_save_head['u16_ue_num'],
        'u16_tag_num': t_ei_msg_info_save_head['u16_tag_num'],
        'u16_resv': t_ei_msg_info_save_head['u16_resv'],
        'str_msg_name': t_ei_msg_info_save_head['str_msg_name'],
        'au16_cell_id': list(t_ei_msg_info_save_head['au16_cell_id']),
        'au16_ueg_id': list(t_ei_msg_info_save_head['au16_ueg_id']),
        'at_tlv_info': [],
    }
    for _ in range(MAX_TASK_MSG_NUM):
        pt_ei_msg_info['at_tlv_info'].append({
            'u32_tlv_id': 0, 'str_tlv_name': b'', 'u16_var_num': 0,
            'at_var_info': [
                {'str_var_name': b'', 'b8_signed': False, 'u16_offset': 0,
                 'u16_len': 0, 'bit_region_count': 0, 'b_is_bit_region': False}
                for _ in range(MAX_VAR_IN_STRUCT)
            ],
        })

    # ---- 读取 TLV 配置 ----
    for i in range(min(pt_ei_msg_info['u16_tag_num'], 100)):
        t_tlv_info_save_head = {'u32_tlv_id': 0, 'str_tlv_name': b'', 'u16_var_num': 0}
        tlv_save_head_bytes = g_data[data_ptr:data_ptr + SIZEOF_TLV_INFO_SAVE_HEAD]
        data_ptr += len(tlv_save_head_bytes)

        t_tlv_info_save_head['u32_tlv_id'] = struct.unpack_from("<I", tlv_save_head_bytes, 0)[0]
        t_tlv_info_save_head['str_tlv_name'] = tlv_save_head_bytes[4:50]
        t_tlv_info_save_head['u16_var_num'] = struct.unpack_from("<H", tlv_save_head_bytes, 50)[0]

        pt_ei_msg_info['at_tlv_info'][i]['u32_tlv_id'] = t_tlv_info_save_head['u32_tlv_id']
        pt_ei_msg_info['at_tlv_info'][i]['u16_var_num'] = t_tlv_info_save_head['u16_var_num']
        pt_ei_msg_info['at_tlv_info'][i]['str_tlv_name'] = t_tlv_info_save_head['str_tlv_name']

        # 读取变量信息
        for j in range(min(t_tlv_info_save_head['u16_var_num'], MAX_VAR_IN_STRUCT)):
            t_var_info = {'str_var_name': b'', 'b8_signed': False, 'u16_offset': 0,
                          'u16_len': 0, 'bit_region_count': 0, 'b_is_bit_region': False}
            var_info_bytes = g_data[data_ptr:data_ptr + SIZEOF_VAR_INFO]
            data_ptr += len(var_info_bytes)

            t_var_info['str_var_name'] = var_info_bytes[0:47]
            t_var_info['b8_signed'] = var_info_bytes[47] != 0
            t_var_info['u16_offset'] = struct.unpack_from("<H", var_info_bytes, 48)[0]
            t_var_info['u16_len'] = struct.unpack_from("<H", var_info_bytes, 50)[0]
            t_var_info['bit_region_count'] = struct.unpack_from("<H", var_info_bytes, 52)[0]
            t_var_info['b_is_bit_region'] = var_info_bytes[54] != 0

            pt_ei_msg_info['at_tlv_info'][i]['at_var_info'][j] = t_var_info

    # ---- 打开 CSV 输出 ----
    with open(str_csv_file_name, "w", encoding="utf-8") as csv_file:
        # 写 CSV 表头
        header_builder = []
        header_builder.append('"LocalTime","AirTime","FN","Slot","SN"')
        header_builder.append('"CellId","UEGID"')
        g_t_colum_head_names = {'filed_num': 7, 'tag_num': 0, 'tag_value': [0] * MAX_NUM_TAGID}

        for i in range(min(pt_ei_msg_info['u16_tag_num'], 100)):
            str_tag_name = bytes_to_string(pt_ei_msg_info['at_tlv_info'][i]['str_tlv_name'])
            g_t_colum_head_names['tag_value'][g_t_colum_head_names['tag_num']] = \
                pt_ei_msg_info['at_tlv_info'][i]['u32_tlv_id']
            g_t_colum_head_names['tag_num'] += 1

            for j in range(min(pt_ei_msg_info['at_tlv_info'][i]['u16_var_num'], MAX_VAR_IN_STRUCT)):
                str_val_name = bytes_to_string(pt_ei_msg_info['at_tlv_info'][i]['at_var_info'][j]['str_var_name'])
                str_full_val_name = '"%s.%s"' % (str_trim(str_tag_name), str_trim(str_val_name))
                header_builder.append(str_full_val_name)
                g_t_colum_head_names['filed_num'] += 1

        csv_file.write(",".join(header_builder) + "\n")

        # 计算配置总长度
        u32_save_total_len = SIZEOF_EI_MSG_INFO_SAVE_HEAD
        for i in range(min(pt_ei_msg_info['u16_tag_num'], 100)):
            u32_save_len = (pt_ei_msg_info['at_tlv_info'][i]['u16_var_num'] * SIZEOF_VAR_INFO
                            + SIZEOF_TLV_INFO_SAVE_HEAD)
            u32_save_total_len += u32_save_len

        u32_remaind_len = u32_file_total_size - 12 - u32_save_total_len
        u32_cur_msg_pos = 12 + u32_save_total_len

        g_t_record = {'tag_num': 0, 'values': [''] * MAX_NUM_TAGID}

        # ---- 解析数据记录 ----
        while u32_remaind_len > SIZEOF_EI_MSG_HEAD + SIZEOF_EI_TLV_HEAD:
            data_ptr = u32_cur_msg_pos

            # 读取本地时间
            ach_local_time = g_data[data_ptr:data_ptr + MAX_EI_LOCAL_TIME_LEN]
            ach_local_time = ach_local_time.decode('latin-1').rstrip('\x00')
            data_ptr += MAX_EI_LOCAL_TIME_LEN

            # 读取消息头
            msg_head_bytes = g_data[data_ptr:data_ptr + SIZEOF_EI_MSG_HEAD]
            data_ptr += len(msg_head_bytes)

            t_ei_msg_head = {
                'u32_ei_msg_id': struct.unpack_from("<I", msg_head_bytes, 0)[0],
                'u32_air_time': struct.unpack_from("<I", msg_head_bytes, 4)[0],
                'u32_sn': struct.unpack_from("<I", msg_head_bytes, 8)[0],
                'u16_tlv_num': struct.unpack_from("<H", msg_head_bytes, 12)[0],
                'u16_msg_len': struct.unpack_from("<H", msg_head_bytes, 14)[0],
            }

            # 校验消息 ID
            if t_ei_msg_head['u32_ei_msg_id'] != pt_ei_msg_info['u32_msg_id']:
                raise ValueError("msgId mismatch: got %d, expected %d" % (
                    t_ei_msg_head['u32_ei_msg_id'], pt_ei_msg_info['u32_msg_id']))

            if t_ei_msg_head['u16_tlv_num'] > MAX_REPORT_TLV_KEY_NUM:
                raise ValueError("tlvNum %d exceeds max %d" % (
                    t_ei_msg_head['u16_tlv_num'], MAX_REPORT_TLV_KEY_NUM))

            u32_cur_msg_pos += SIZEOF_EI_MSG_HEAD + MAX_EI_LOCAL_TIME_LEN

            if u32_remaind_len < SIZEOF_EI_MSG_HEAD + t_ei_msg_head['u16_msg_len'] + MAX_EI_LOCAL_TIME_LEN:
                raise ValueError("remaining length %d too small for message length %d" % (
                    u32_remaind_len, t_ei_msg_head['u16_msg_len']))

            u32_remaind_len -= (SIZEOF_EI_MSG_HEAD + t_ei_msg_head['u16_msg_len'] + MAX_EI_LOCAL_TIME_LEN)

            # 更新进度
            task_data['u8_cvt_progress'] = ((u32_file_total_size - u32_remaind_len) * 100) // u32_file_total_size

            u32_next_tlv_pos = u32_cur_msg_pos
            for k in range(len(task_data['u8_tlv_key_index_list'])):
                task_data['u8_tlv_key_index_list'][k] = 0

            # 处理每个 TLV
            for u32_key_tlv_loop in range(t_ei_msg_head['u16_tlv_num']):
                data_ptr = u32_next_tlv_pos
                tlv_head_bytes = g_data[data_ptr:data_ptr + SIZEOF_EI_TLV_HEAD]

                t_key_ei_tlv_head = {
                    'u16_tlv_id': struct.unpack_from("<H", tlv_head_bytes, 0)[0],
                    'u16_id': struct.unpack_from("<H", tlv_head_bytes, 2)[0],
                    'u16_tlv_len': struct.unpack_from("<H", tlv_head_bytes, 4)[0],
                    'u16_cell_id': struct.unpack_from("<H", tlv_head_bytes, 6)[0],
                }

                if t_key_ei_tlv_head['u16_tlv_len'] <= 0 or t_key_ei_tlv_head['u16_tlv_len'] > 200 * 4:
                    break

                if task_data['u8_tlv_key_index_list'][u32_key_tlv_loop] > 0:
                    u32_next_tlv_pos += SIZEOF_EI_TLV_HEAD + t_key_ei_tlv_head['u16_tlv_len']
                    continue

                # 构建 CSV 行
                line_builder = []
                line_builder.append('"%s",' % ach_local_time)
                line_builder.append('%d,' % t_ei_msg_head['u32_air_time'])

                # 判断是否为 LTE 文件
                is_lte_file = "_Lte" in ch_file_name
                # LTE: air_time 低 4 bit 为子帧号，高 bits 为帧号
                # NR : air_time 低 8 bit 为时隙号（半时隙粒度），高 bits 为帧号
                # 工具不输出 HalfSlot 列，故此处不能拆分出半时隙，否则后续所有列整体右移一位
                if is_lte_file:
                    line_builder.append('%d,' % ((t_ei_msg_head['u32_air_time'] // 16) % 1024))
                    line_builder.append('%d,' % (t_ei_msg_head['u32_air_time'] % 16))
                else:
                    line_builder.append('%d,' % ((t_ei_msg_head['u32_air_time'] // 256) % 1024))
                    line_builder.append('%d,' % (t_ei_msg_head['u32_air_time'] & 0xFF))
                line_builder.append('%d,' % t_ei_msg_head['u32_sn'])
                line_builder.append('%d,%d' % (t_key_ei_tlv_head['u16_cell_id'], t_key_ei_tlv_head['u16_id']))

                # 清空记录
                g_t_record['tag_num'] = 0
                for k in range(len(g_t_record['values'])):
                    g_t_record['values'][k] = ""

                u16_cur_row_gid = t_key_ei_tlv_head['u16_id']
                u16_cur_row_cell_id = t_key_ei_tlv_head['u16_cell_id']

                # 查找匹配的 TLV
                b8_is_invalid_tlv = False
                for tag_idx in range(min(pt_ei_msg_info['u16_tag_num'], 100)):
                    u32_remaind_tlv_pos = u32_next_tlv_pos
                    b8_is_invalid_tlv = False

                    for u32_rpt_tlv_loop in range(u32_key_tlv_loop,
                                                  min(t_ei_msg_head['u16_tlv_num'], MAX_REPORT_TLV_KEY_NUM)):
                        data_ptr = u32_remaind_tlv_pos
                        tlv_head_bytes = g_data[data_ptr:data_ptr + SIZEOF_EI_TLV_HEAD]

                        t_ei_tlv_head = {
                            'u16_tlv_id': struct.unpack_from("<H", tlv_head_bytes, 0)[0],
                            'u16_id': struct.unpack_from("<H", tlv_head_bytes, 2)[0],
                            'u16_tlv_len': struct.unpack_from("<H", tlv_head_bytes, 4)[0],
                            'u16_cell_id': struct.unpack_from("<H", tlv_head_bytes, 6)[0],
                        }

                        if t_ei_tlv_head['u16_tlv_len'] <= 0 or t_ei_tlv_head['u16_tlv_len'] > 200 * 4:
                            b8_is_invalid_tlv = True
                            break

                        u32_remaind_tlv_pos += SIZEOF_EI_TLV_HEAD + t_ei_tlv_head['u16_tlv_len']

                        if task_data['u8_tlv_key_index_list'][u32_rpt_tlv_loop] > 0:
                            continue

                        # 匹配 TLV ID、CellId 和 GID
                        if (t_ei_tlv_head['u16_tlv_id'] == pt_ei_msg_info['at_tlv_info'][tag_idx]['u32_tlv_id']
                                and u16_cur_row_cell_id == t_ei_tlv_head['u16_cell_id']
                                and u16_cur_row_gid == t_ei_tlv_head['u16_id']):

                            tlv_id = int(pt_ei_msg_info['at_tlv_info'][tag_idx]['u32_tlv_id'])
                            task_data['u8_tlv_key_index_list'][u32_rpt_tlv_loop] = 1

                            # 读取 TLV 数据
                            base = data_ptr + SIZEOF_EI_TLV_HEAD
                            pch_tlv_data = g_data[base:base + t_ei_tlv_head['u16_tlv_len']]

                            # 位域处理变量
                            b_fir_bit_variable_flag = False
                            offset_position = 0
                            bit_region_position = 0

                            value_builder = []
                            for val_idx in range(min(pt_ei_msg_info['at_tlv_info'][tag_idx]['u16_var_num'],
                                                    MAX_VAR_IN_STRUCT)):
                                var_info = pt_ei_msg_info['at_tlv_info'][tag_idx]['at_var_info'][val_idx]

                                # 位域特殊处理
                                if var_info['b_is_bit_region']:
                                    if not b_fir_bit_variable_flag:
                                        b_fir_bit_variable_flag = True
                                        offset_position = var_info['u16_offset']
                                        bit_region_position = var_info['bit_region_count']
                                    else:
                                        if offset_position == var_info['u16_offset']:
                                            bit_region_position += var_info['bit_region_count']
                                        else:
                                            offset_position = var_info['u16_offset']
                                            bit_region_position = var_info['bit_region_count']
                                else:
                                    b_fir_bit_variable_flag = False
                                    offset_position = 0
                                    bit_region_position = 0

                                # 根据数据类型处理
                                if var_info['u16_len'] == 1:  # 1字节
                                    if var_info['b8_signed']:
                                        u8_val = pch_tlv_data[var_info['u16_offset']]
                                        if u8_val >= 128:
                                            u8_val = 255 - u8_val + 1
                                            value_builder.append(",-%d" % u8_val)
                                        else:
                                            value_builder.append(",%d" % u8_val)
                                    else:
                                        u8_val = pch_tlv_data[var_info['u16_offset']]
                                        if (var_info['b_is_bit_region']
                                                and 1 <= var_info['bit_region_count'] <= 8):
                                            u8_val = extract_bit_field8(
                                                u8_val, bit_region_position, var_info['bit_region_count'])
                                        value_builder.append(",%d" % u8_val)

                                elif var_info['u16_len'] == 2:  # 2字节
                                    if var_info['b8_signed']:
                                        u16_val = struct.unpack_from("<H", pch_tlv_data, var_info['u16_offset'])[0]
                                        if u16_val >= 0x8000:
                                            u16_val = 0xFFFF - u16_val + 1
                                            value_builder.append(",-%d" % u16_val)
                                        else:
                                            value_builder.append(",%d" % u16_val)
                                    else:
                                        u16_val = struct.unpack_from("<H", pch_tlv_data, var_info['u16_offset'])[0]
                                        if (var_info['b_is_bit_region']
                                                and 1 <= var_info['bit_region_count'] <= 16):
                                            u16_val = extract_bit_field16(
                                                u16_val, bit_region_position, var_info['bit_region_count'])
                                        value_builder.append(",%d" % u16_val)

                                elif var_info['u16_len'] == 4:  # 4字节
                                    str_var_name = bytes_to_string(var_info['str_var_name'])
                                    if str_var_name == "u32IpAddr":
                                        u32_val = struct.unpack_from("<I", pch_tlv_data, var_info['u16_offset'])[0]
                                        value_builder.append(",%s" % int2ip_str(u32_val))
                                    elif var_info['b8_signed']:
                                        u32_val = struct.unpack_from("<I", pch_tlv_data, var_info['u16_offset'])[0]
                                        if u32_val >= 0x80000000:
                                            u32_val = 0xFFFFFFFF - u32_val + 1
                                            value_builder.append(",-%d" % u32_val)
                                        else:
                                            value_builder.append(",%d" % u32_val)
                                    else:
                                        u32_val = struct.unpack_from("<I", pch_tlv_data, var_info['u16_offset'])[0]
                                        if (var_info['b_is_bit_region']
                                                and 1 <= var_info['bit_region_count'] <= 32):
                                            u32_val = extract_bit_field32(
                                                u32_val, bit_region_position, var_info['bit_region_count'])
                                        value_builder.append(",%d" % u32_val)

                                elif var_info['u16_len'] == 8:  # 8字节
                                    u64_val = struct.unpack_from("<Q", pch_tlv_data, var_info['u16_offset'])[0]
                                    if (var_info['b_is_bit_region']
                                            and 1 <= var_info['bit_region_count'] <= 64):
                                        u64_val = extract_bit_field64(
                                            u64_val, bit_region_position, var_info['bit_region_count'])
                                    value_builder.append(",%d" % u64_val)

                                else:
                                    raise ValueError("unsupported data length %d" % var_info['u16_len'])

                            g_t_record['values'][tlv_id] = "".join(value_builder)
                            break

                    if b8_is_invalid_tlv:
                        break

                # 输出找到的记录
                for tag_n in range(g_t_colum_head_names['tag_num']):
                    itag_id = g_t_colum_head_names['tag_value'][tag_n]
                    if g_t_record['values'][itag_id] != "":
                        line_builder.append(g_t_record['values'][itag_id])
                    else:
                        for _ in range(min(pt_ei_msg_info['at_tlv_info'][tag_n]['u16_var_num'],
                                           MAX_VAR_IN_STRUCT)):
                            line_builder.append(',"-"')

                csv_file.write("".join(line_builder) + "\n")
                u32_next_tlv_pos += SIZEOF_EI_TLV_HEAD + t_key_ei_tlv_head['u16_tlv_len']

            u32_cur_msg_pos += t_ei_msg_head['u16_msg_len']

    return str_csv_file_name


# ==================== 主函数 ====================
def main():
    if len(sys.argv) < 2:
        print("Usage: fileproc.py <input.ei file> [instance_id]")
        sys.exit(1)

    input_file = sys.argv[1]
    instance_id = 0
    if len(sys.argv) >= 3:
        try:
            instance_id = int(sys.argv[2])
        except ValueError:
            instance_id = 0

    try:
        out = convert_file(input_file, instance_id)
    except Exception as e:
        print("Error converting file: %s" % e)
        sys.exit(1)

    print("Successfully converted %s to CSV format" % input_file)
    print("Output: %s" % out)


if __name__ == "__main__":
    main()
