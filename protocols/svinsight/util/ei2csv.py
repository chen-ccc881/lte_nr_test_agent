#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ei2csv.py — EI 二进制日志 → CSV 转换入口。

转换由 svinsight 自带的纯 Python 实现 ``util/fileproc.py`` 完成
（移植自 fileproc.go）。``fileproc.py`` 已内置 **NR / LTE 双制式**分支：
文件名含 ``_Lte`` 时按 LTE 时隙结构解析（FN = airTime//16 % 1024、
Slot = airTime % 16），否则按 NR 解析（FN = airTime//256 % 1024 …）。
无需 EI_FileProc.dll，跨平台、稳定。

对外接口：``ei2csv(directory, eifiles)``。
"""

import logging
import os

try:
    from .fileproc import convert_file as _convert_file
except ImportError:  # 以脚本方式直接运行、util 不在包上下文时
    from fileproc import convert_file as _convert_file


# ==========================================================================
# 主入口
# ==========================================================================
def ei2csv(directory, eifiles):
    """
    把目录下指定的 .ei 文件转换成 .csv。

    Args:
        directory: .ei 文件所在目录
        eifiles  : .ei 文件名列表（不含目录）

    Returns:
        list: 转换失败的文件名列表（空列表表示全部成功）

    说明：
      单文件异常保护：某个坏文件转换失败不影响其余文件。
    """
    complete = 0
    failed = []
    for eifile in eifiles:
        full = os.path.join(directory, eifile)
        try:
            _convert_file(full, 0)
            complete += 1
            print('processed %d of total %d files' % (complete, len(eifiles)))
        except Exception as e:  # noqa: BLE001
            failed.append(eifile)
            logging.warning("EI 转换失败 %s: %s", eifile, e)

    if failed:
        print("!!Failed %d files: %s" % (len(failed), ", ".join(failed)))
    print("!!Done!!")
    return failed


if __name__ == '__main__':
    import sys
    import pandas as pd

    directory = sys.argv[1] if len(sys.argv) > 1 else r"D:\问题分析\test"
    files = pd.Series(os.listdir(directory))
    eifiles = list(files[files.apply(
        lambda x: x.endswith(r'.ei') and x.rsplit('.')[0] + r'.csv' not in files)])
    ei2csv(directory, eifiles)
