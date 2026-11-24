#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JSON文件字段读取工具（纯函数调用版本）

调用示例:
    from json_field_reader import read_json_dir

    results = read_json_dir("./gnbxtace", ["msg_name", "rnti", "cell_id"])
    for r in results:
        print(r["msg_name"], r["rnti"])
"""

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional


def _get_nested_value(data: Dict[str, Any], field_path: str) -> Any:
    """获取嵌套字段值，支持点号分隔路径或叶子字段名模糊匹配。

    支持三种用法：
    1. 精确路径: "decoded_content.message.c1.rrcConnectionSetup"
    2. 叶子字段名: "periodicPHR_Timer"  → 递归搜索整个JSON树
    3. 部分路径: "phr_Config.setup.periodicPHR_Timer" → 递归匹配后缀路径
    """
    keys = field_path.split(".")
    # 先尝试精确路径查找
    current = data
    matched = True
    for key in keys:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            matched = False
            break
    if matched:
        return current

    # 精确路径未匹配，递归搜索叶子字段名
    leaf_name = keys[-1]
    results = []

    def _recursive_search(obj, search_key):
        if isinstance(obj, dict):
            if search_key in obj:
                results.append(obj[search_key])
            for v in obj.values():
                _recursive_search(v, search_key)
        elif isinstance(obj, list):
            for item in obj:
                _recursive_search(item, search_key)

    _recursive_search(data, leaf_name)

    if not results:
        return None
    if len(results) == 1:
        return results[0]
    # 多个匹配时，尝试用完整路径的后缀进一步筛选
    if len(keys) > 1:
        for r in results:
            if _check_path_suffix(data, keys, r):
                return r
    # 无法筛选时返回第一个
    return results[0]


def _check_path_suffix(data, keys, target_value) -> bool:
    """检查 data 树中是否存在 keys 指定的（后缀）路径，且叶子取值等于 target_value。

    遍历树中每个 dict 节点，尝试从该节点开始按 keys 精确匹配；
    支持形如 "pdsch_TimeDomainAllocationList[0]" 的数组索引 key。
    """
    def _match_from(node, ks):
        cur = node
        for key in ks:
            if isinstance(cur, dict):
                if key in cur:
                    cur = cur[key]
                else:
                    # 兼容 "base[i]" 形式的数组索引
                    m = re.match(r'^(.*)\[(\d+)\]$', key)
                    if m and m.group(1) in cur and isinstance(cur[m.group(1)], list) \
                            and int(m.group(2)) < len(cur[m.group(1)]):
                        cur = cur[m.group(1)][int(m.group(2))]
                    else:
                        return False
            else:
                return False
        return cur == target_value

    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if _match_from(node, keys):
                return True
            for v in node.values():
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    stack.append(item)
    return False


def _normalize_filter_key(s: str) -> str:
    """规范化过滤关键字/待匹配文本：空格与下划线等价，统一转为小写。"""
    return s.replace(" ", "_").lower()


def _match_filter(fname: str, data: Dict[str, Any], file_filter: List[str]) -> bool:
    """判断文件是否命中过滤关键字。

    过滤关键字可匹配以下两处（空格/下划线视为等价，忽略大小写）：
    1. 文件名（信令名中的空格在文件名中已被替换为下划线）
    2. JSON内容中的 msg_name 字段（存储的是带空格的原始信令名）
    """
    # 候选待匹配文本
    candidates = [fname]
    msg_name = data.get("msg_name")
    if isinstance(msg_name, str):
        candidates.append(msg_name)
    elif isinstance(msg_name, (dict, list)):
        # msg_name 可能嵌套在其他结构中，尝试递归提取字符串值
        collected = []

        def _collect(obj):
            if isinstance(obj, dict):
                if isinstance(obj.get("msg_name"), str):
                    collected.append(obj["msg_name"])
                for v in obj.values():
                    _collect(v)
            elif isinstance(obj, list):
                for item in obj:
                    _collect(item)

        _collect(msg_name)
        candidates.extend(collected)

    for kw in file_filter:
        norm_kw = _normalize_filter_key(kw)
        for cand in candidates:
            if norm_kw in _normalize_filter_key(cand):
                return True
    return False


def _extract_row(data: Dict[str, Any], fpath: str, fields: List[str]) -> Dict[str, Any]:
    """从已解析的JSON数据中提取指定字段，生成一行记录。"""
    row: Dict[str, Any] = {"file": os.path.basename(fpath)}
    for field_name in fields:
        row[field_name] = _get_nested_value(data, field_name)
    return row


def _match_filter_dict(fname: str, data: Dict[str, Any],
                       field_filter_dict: Dict[str, List[str]]) -> List[str]:
    """返回文件命中的所有信令过滤关键字（可空列表表示未命中）。

    每个关键字与文件名的 msg_name/文件名做空格-下划线等价匹配；
    一个文件可能命中多个信令，返回所有命中的关键字。
    """
    matched = []
    for kw in field_filter_dict:
        if _match_filter(fname, data, [kw]):
            matched.append(kw)
    return matched


def read_json_dir(
    json_dir: str,
    fields: Optional[List[str]] = None,
    recursive: bool = True,
    file_filter: Optional[List[str]] = None,
    field_filter_dict: Optional[Dict[str, List[str]]] = None,
    file_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """读取指定JSON文件（或目录下全部JSON文件）的指定字段值，按字典列表返回。

    Args:
        json_dir:    JSON文件所在目录路径
        fields:      要读取的字段名列表，支持点号嵌套路径
                      例如: ["msg_name", "cell_id", "decoded_content.message.c1"]。
                      当未指定 field_filter_dict 时使用；二者同时指定时忽略 fields。
        recursive:   是否递归遍历子目录，默认True
        file_filter: 信令过滤关键字列表，只处理命中的文件，None表示全部。
                     过滤会同时匹配文件名和JSON内容中的 msg_name 字段，
                     且空格/下划线视为等价（如 "INITIAL CONTEXT SETUP REQUEST"
                     可匹配文件名中的 INITIAL_CONTEXT_SETUP_REQUEST）。
        field_filter_dict: 信令名->字段列表 字典，将"过滤"与"取字段"合并为一次操作。
                     格式如: {'SIG_NGAP_PDU_SESSION_RES_SETUP_REQ': ['fieldA', 'fieldB'],
                              'SIG_UU_RRC_RECFG': ['fieldC'],
                              'SIG_NGAP_PDU_SESSION_RES_SETUP_RSP': ['fieldD']}
                     对每个文件，先按信令名过滤（匹配文件名与 msg_name 字段），
                     命中哪个信令就只提取该信令对应的字段；一个文件命中多个信令时
                     会生成多行记录。指定此项时 fields/file_filter 不再单独生效。
        file_name:   指定的JSON文件名（可含路径或仅为文件名）。非空时仅解析该文件，
                     为空时解析目录下所有JSON文件。

    Returns:
        list[dict]: 每条JSON记录对应一个字典，包含:
            - 请求的字段名作为key
            - "file": 文件名
            - "_path": 文件完整路径

    示例:
        results = read_json_dir("./gnbxtace", ["msg_name", "rnti", "report_time"])
        for r in results:
            print(r["msg_name"], r["rnti"], r["_file"])

        # 嵌套字段 + 信令名过滤（空格/下划线均可）
        results = read_json_dir("./gnbxtace", ["msg_name", "direction"],
                                file_filter=["INITIAL CONTEXT SETUP REQUEST"])

        # 每个信令过滤后只检查各自的字段
        results = read_json_dir("./gnbxtace",
                                field_filter_dict={
                                    'SIG_NGAP_PDU_SESSION_RES_SETUP_REQ':
                                        ['NGAP_PDUSessionResourceSetupListSUReq[0].pDuSessionID'],
                                    'SIG_UU_RRC_RECFG':
                                        ['drb_ToAddModList[0].cnAssociation.sdap-Config.pdu_Session'],
                                })

        # 只解析指定文件
        results = read_json_dir("./gnbxtace", ["msg_name", "rnti"],
                                file_name="0-2026-8-18__16-38-45-...-INITIAL_CONTEXT_SETUP_REQUEST.json")
    """
    # 指定了具体文件：直接解析该文件
    if file_name:
        fpath = file_name if os.path.isabs(file_name) else os.path.join(json_dir, file_name)
        if not os.path.isfile(fpath):
            return []

        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return []

        results: List[Dict[str, Any]] = []
        if field_filter_dict:
            # 按信令名过滤并分别取各自的字段
            for sig_name in _match_filter_dict(os.path.basename(fpath), data, field_filter_dict):
                results.append(_extract_row(data, fpath, field_filter_dict[sig_name]))
        else:
            # 兼容旧逻辑：可选的信令过滤 + 统一的字段列表
            if file_filter:
                if not _match_filter(os.path.basename(fpath), data, file_filter):
                    return []
            results.append(_extract_row(data, fpath, fields or []))
        return results

    if not os.path.isdir(json_dir):
        raise NotADirectoryError(f"目录不存在或不可访问: {json_dir}")

    # 收集所有JSON文件路径
    json_files: List[str] = []
    if recursive:
        for root, _, files in os.walk(json_dir):
            for fname in files:
                if fname.lower().endswith(".json"):
                    json_files.append(os.path.join(root, fname))
    else:
        for fname in os.listdir(json_dir):
            if not fname.lower().endswith(".json"):
                continue
            fpath = os.path.join(json_dir, fname)
            if not os.path.isfile(fpath):
                continue
            json_files.append(fpath)

    # 逐个读取
    results = []
    for fpath in json_files:
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            logging.warning("Skip broken JSON file %s: %s", fpath, e)
            continue
        except OSError as e:
            logging.warning("Skip unreadable JSON file %s: %s", fpath, e)
            continue

        if field_filter_dict:
            # 按信令名过滤并分别取各自的字段；一个文件命中多个信令时生成多行
            for sig_name in _match_filter_dict(os.path.basename(fpath), data, field_filter_dict):
                results.append(_extract_row(data, fpath, field_filter_dict[sig_name]))
        else:
            # 兼容旧逻辑：可选的信令过滤 + 统一的字段列表
            if file_filter:
                if not _match_filter(os.path.basename(fpath), data, file_filter):
                    continue
            results.append(_extract_row(data, fpath, fields or []))

    return results

if __name__ == '__main__':
    json_dir = r'C:\04_workspace\AutoTest\bjckAutotest\AutoTestMain\maintenanceTool\20260803110114'
    fields = ['periodicPHR_Timer']
    results = read_json_dir(json_dir, fields)
    print(results)

    # 每个信令过滤后只检查各自的字段
    field_filter_dict = {
        'SIG_NGAP_PDU_SESSION_RES_SETUP_REQ':
            ['NGAP_PDUSessionResourceSetupListSUReq[0].pDuSessionID',
             'QosFlowSetupRequestList[0].qosFlowLevelQosParameters.qosCharacteristics.nonDynamic5QI.fiveQI'],
        'SIG_UU_RRC_RECFG':
            ['drb_ToAddModList[0].cnAssociation.sdap-Config.pdu_Session'],
        'SIG_NGAP_PDU_SESSION_RES_SETUP_RSP':
            ['NGAP_PDUSessionResourceSetupListSURes[0].pDUSessionID'],
    }
    results = read_json_dir(json_dir, field_filter_dict=field_filter_dict)
    print(results)