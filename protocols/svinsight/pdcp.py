# coding=utf-8
"""
pdcp.py — PDCP 分析模块

支持分析 PDCP 上行/下行日志，包括：
- 上行时延（airCost / rlcCost / pdcpCost）
- PDCP 窗口状态（rxNext, rxDeliv → 积压包数）
- 流量统计（SDU 吞吐量、字节数）
- 丢包率（KPI 字段）
- 下行 PDCP SDU 发送窗口（txNext, txDeliv）与 RLC 缓冲
- TCP 报文分析（端口/流量/重传/拥塞标志，上下行通用）→ PdcpTcp
- ICMP 报文分析（ping RTT，UL Echo Request 与 DL Echo Reply 按 seq 配对）→ PdcpIcmp

字段前缀说明：
  UL 日志（PDCP_ulUeTtiInfo）：
      UL_DATA_PDU.u16srcport / u16dstport / u32seq / u32ackno / u32icmpType / u32icmpSeq
      注：UL 日志无 u8tcpFlag 字段
  DL 日志（PDCP_dlUeTtiInfo）：
      DL_DATA_SDU.u16srcport / u16dstport / u32seq / u32ackno / u8tcpFlag / u32icmpType / u32icmpSeq

有效报文判断：
  TCP  — u32seq != 0
  ICMP — u32icmpSeq != 0
  无上层报文时，各字段值均为 0。

TCP Flag 位定义（u8tcpFlag，仅 DL 日志有效）：
  bit0=FIN  bit1=SYN  bit2=RST  bit3=PSH  bit4=ACK  bit5=URG

ICMP Type 定义：
  8 = Echo Request（UL 方向，终端发送 ping）
  0 = Echo Reply  （DL 方向，服务器回包）
"""

import pandas as pd
import numpy as np
import os

# 特殊 UEGID 起始值（SSB/SIB1/MSG4 等广播/公共信道），不属于普通 UE
SPECIAL_UEGID_MIN = 7201

# PDCP 日志文件前缀
NR_FILE_PDCP_UL = 'PDCP_ulUeTtiInfo'
NR_FILE_PDCP_DL = 'PDCP_dlUeTtiInfo'

# ============================================================
# 工具函数
# ============================================================

def _load_csvs(directory, prefix, id_filter=None, time_interval=None):
    """
    加载目录下所有匹配 prefix 前缀的 CSV 文件，合并为 DataFrame。

    Args:
        directory: 日志目录
        prefix: 文件名前缀，如 'PDCP_ulUeTtiInfo'
        id_filter: dict，如 {'UEGID': [23], 'CellId': [2]}
        time_interval: [start_str, end_str]，如 ['2026/03/17 10:41:00', '2026/03/17 10:42:00']
    Returns:
        DataFrame（已合并、已过滤）
    """
    files = sorted([f for f in os.listdir(directory) if f.endswith('.csv') and prefix in f])
    if not files:
        return pd.DataFrame()

    parts = []
    for fname in files:
        fpath = os.path.join(directory, fname)
        df = pd.read_csv(fpath, na_values='-')
        # 过滤特殊 UEGID
        if 'UEGID' in df.columns:
            df = df[df['UEGID'] < SPECIAL_UEGID_MIN]
        # id_filter
        if id_filter:
            for col, vals in id_filter.items():
                if col in df.columns:
                    df = df[df[col].isin(vals)]
        # 时间过滤
        if time_interval and 'LocalTime' in df.columns:
            datestr = os.path.splitext(fname)[0].rsplit('_')[-1]
            y, m, d = datestr[:4], datestr[4:6], datestr[6:8]

            def _parse_time(t):
                try:
                    h, mi, s, ms = t.replace(' ', '').split(':')
                    import datetime
                    return datetime.datetime(int(y), int(m), int(d),
                                            int(h), int(mi), int(s), int(ms) * 1000)
                except Exception:
                    return pd.NaT

            df['_lt'] = df['LocalTime'].apply(_parse_time)
            t0 = pd.to_datetime(time_interval[0])
            t1 = pd.to_datetime(time_interval[1])
            df = df[(df['_lt'] >= t0) & (df['_lt'] <= t1)].drop(columns=['_lt'])
        parts.append(df)

    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True)


def _parse_localtime(df, directory, fname_col='_fname'):
    """将 LocalTime 字符串解析为 datetime，日期从文件名提取。"""
    # 此函数仅在需要时调用，调用者需提供日期字符串 datestr
    pass


# ============================================================
# PdcpUl — PDCP 上行分析
# ============================================================

class PdcpUl:
    """
    PDCP 上行日志分析类（PDCP_ulUeTtiInfo）

    关键字段（DATA PDU 部分，单位 us）：
        UL_DATA_PDU.u32airCost   — 空口时延（从 RLC 收包到 PDCP 时间差）
        UL_DATA_PDU.u32rlcCost   — RLC 层处理时延
        UL_DATA_PDU.u32pdcpCost  — PDCP 层处理时延
        UL_DATA_PDU.u32rxNext    — PDCP 期望接收的下一个 SN（窗口前沿）
        UL_DATA_PDU.u32rxDeliv   — PDCP 已交付到上层的最大连续 SN（窗口下沿）
    KPI 字段（每秒统计）：
        PDCP_KPI.u32ulPdcpRcvSduNum   — 上行接收 SDU 数
        PDCP_KPI.u32ulPdcpLostSduNum  — 上行丢失 SDU 数
        PDCP_KPI.u32ulPdcpRcvSduByte  — 上行接收字节数
        PDCP_KPI.u32dlPdcpSndSduNum   — 下行发送 SDU 数（在上行日志中）
        PDCP_KPI.u32dlPdcpSndSduByte  — 下行发送字节数（在上行日志中）
        PDCP_KPI.u32dlPdcpLostSduNum  — 下行丢失 SDU 数
    """

    UL_DELAY_COLS = [
        'UL_DATA_PDU.u32airCost',
        'UL_DATA_PDU.u32rlcCost',
        'UL_DATA_PDU.u32pdcpCost',
    ]
    WINDOW_COLS = [
        'UL_DATA_PDU.u32rxNext',
        'UL_DATA_PDU.u32rxDeliv',
    ]
    KPI_UL_COLS = [
        'PDCP_KPI.u32ulPdcpRcvSduNum',
        'PDCP_KPI.u32ulPdcpLostSduNum',
        'PDCP_KPI.u32ulPdcpRcvSduByte',
    ]
    KPI_DL_COLS = [
        'PDCP_KPI.u32dlPdcpSndSduNum',
        'PDCP_KPI.u32dlPdcpLostSduNum',
        'PDCP_KPI.u32dlPdcpSndSduByte',
    ]
    # PDCP COUNT 字段（协议规定全局单调递增，32-bit，无重复）
    COUNT_COL = 'UL_DATA_PDU.u32pdcpCount'
    # RbId 字段（PDCP 实例粒度为 UEGID + RbId + 方向）
    RBID_COL  = 'UL_DATA_PDU.u8rbId'
    # PDCP COUNT 32-bit 回绕上限
    PDCP_COUNT_MAX = 2 ** 32

    def __init__(self, directory, id_filter=None, time_interval=None):
        self._dir = directory
        self._id_filter = id_filter or {}
        self._time_interval = time_interval
        self._df = None

    def _data(self):
        if self._df is None:
            self._df = _load_csvs(
                self._dir, NR_FILE_PDCP_UL,
                id_filter=self._id_filter,
                time_interval=self._time_interval,
            )
        return self._df

    def ul_delay_stats(self):
        """
        上行时延统计（数据 PDU 粒度，单位 us）

        Returns:
            DataFrame: index=['mean','p50','p95','p99','max'], columns=[airCost, rlcCost, pdcpCost]
        """
        df = self._data()
        cols = self.UL_DELAY_COLS
        avail = [c for c in cols if c in df.columns]
        if not avail:
            return pd.DataFrame()
        sub = df[avail].dropna(how='all')
        result = sub.agg(['mean', 'median', lambda x: x.quantile(0.95),
                          lambda x: x.quantile(0.99), 'max'])
        result.index = ['mean', 'p50', 'p95', 'p99', 'max']
        short_cols = {c: c.split('.')[-1].replace('u32', '') for c in avail}
        result.rename(columns=short_cols, inplace=True)
        return result

    def ul_delay_timeseries(self, time_bin=1):
        """
        上行时延时序（按秒聚合均值，单位 us）

        Returns:
            DataFrame: index=time, columns=[airCost, rlcCost, pdcpCost]
        """
        df = self._data()
        if 'LocalTime' not in df.columns:
            return pd.DataFrame()

        # 构造时间戳：取 AirTime（us）做相对秒轴，或用 LocalTime 索引
        cols = ['LocalTime'] + [c for c in self.UL_DELAY_COLS if c in df.columns]
        sub = df[cols].dropna(how='any')
        if sub.empty:
            return pd.DataFrame()

        # LocalTime 转为 datetime（使用第一个文件的日期，近似）
        sub = sub.copy()
        try:
            sub['_t'] = pd.to_datetime(sub['LocalTime'].str.strip(), format='%H:%M:%S:%f', errors='coerce')
        except Exception:
            return pd.DataFrame()
        sub = sub.dropna(subset=['_t']).set_index('_t')
        sub = sub.drop(columns=['LocalTime'])
        rlt = sub.resample(f'{time_bin}s').mean()
        short_cols = {c: c.split('.')[-1].replace('u32', '') for c in sub.columns}
        rlt.rename(columns=short_cols, inplace=True)
        return rlt

    def ul_delay_hist(self, col='UL_DATA_PDU.u32pdcpCost', bins=30):
        """
        上行时延直方图数据（单位 us）

        Returns:
            (counts, bin_edges) — np.histogram 格式
        """
        df = self._data()
        if col not in df.columns:
            return None, None
        s = df[col].dropna()
        if s.empty:
            return None, None
        return np.histogram(s, bins=bins)

    def window_backlog(self):
        """
        PDCP 上行窗口积压（rxNext - rxDeliv，即已收到但未交付上层的包数）

        Returns:
            Series: index=行号, values=积压包数
        """
        df = self._data()
        if not all(c in df.columns for c in self.WINDOW_COLS):
            return pd.Series(dtype=float, name='ul_pdcp_window_backlog')
        sub = df[self.WINDOW_COLS].dropna()
        backlog = sub.iloc[:, 0] - sub.iloc[:, 1]
        backlog.name = 'ul_pdcp_window_backlog'
        return backlog

    def window_timeseries(self, time_bin=1):
        """
        PDCP 上行窗口时序（rxNext/rxDeliv 均值，及积压量均值）

        Returns:
            DataFrame: index=time, columns=[rxNext, rxDeliv, backlog]
        """
        df = self._data()
        cols = ['LocalTime'] + [c for c in self.WINDOW_COLS if c in df.columns]
        if len(cols) < 3:
            return pd.DataFrame()
        sub = df[cols].dropna(how='any').copy()
        try:
            sub['_t'] = pd.to_datetime(sub['LocalTime'].str.strip(), format='%H:%M:%S:%f', errors='coerce')
        except Exception:
            return pd.DataFrame()
        sub = sub.dropna(subset=['_t']).set_index('_t').drop(columns=['LocalTime'])
        sub['backlog'] = sub.iloc[:, 0] - sub.iloc[:, 1]
        rlt = sub.resample(f'{time_bin}s').mean()
        short = {c: c.split('.')[-1].replace('u32', '') for c in rlt.columns if c != 'backlog'}
        rlt.rename(columns=short, inplace=True)
        return rlt

    def kpi_ul(self):
        """
        PDCP KPI 上行统计（周期性 KPI 行）

        Returns:
            DataFrame: KPI 原始数据（每行是一个统计周期）
        """
        df = self._data()
        kpi_cols = [c for c in self.KPI_UL_COLS if c in df.columns]
        if not kpi_cols:
            return pd.DataFrame()
        return df[kpi_cols].dropna(how='all')

    def kpi_dl(self):
        """
        PDCP KPI 下行统计（周期性 KPI 行，位于上行日志中）

        Returns:
            DataFrame: KPI 原始数据
        """
        df = self._data()
        kpi_cols = [c for c in self.KPI_DL_COLS if c in df.columns]
        if not kpi_cols:
            return pd.DataFrame()
        return df[kpi_cols].dropna(how='all')

    def loss_rate(self):
        """
        PDCP 上行丢包率（基于 KPI 字段）

        Returns:
            float: 丢包率 [0, 1]，无数据时返回 None
        """
        df = self._data()
        rcv_col = 'PDCP_KPI.u32ulPdcpRcvSduNum'
        lost_col = 'PDCP_KPI.u32ulPdcpLostSduNum'
        if rcv_col not in df.columns or lost_col not in df.columns:
            return None
        kpi = df[[rcv_col, lost_col]].dropna(how='all')
        if kpi.empty:
            return None
        total_rcv = kpi[rcv_col].sum()
        total_lost = kpi[lost_col].sum()
        if total_rcv + total_lost == 0:
            return 0.0
        return float(total_lost) / float(total_rcv + total_lost)

    def pdcp_count_loss(self, count_col=None, rbid_col=None):
        """
        基于 PDCP COUNT（u32pdcpCount）检测上行 PDCP 层真实丢包。

        原理：
            PDCP 实例的粒度是 UEGID + RbId + 方向（UL/DL），每个实例的 COUNT
            按协议规定独立单调递增、不重复（32-bit）。
            按 (UEGID, RbId) 分组后，对 COUNT 序列排序，计算相邻差值 Δ：
              Δ == 1  → 正常，连续无丢包
              Δ  > 1  → 丢包，本次事件丢失 Δ-1 个包
              Δ  < 0  → COUNT 32-bit 回绕，修正后重新计算
              Δ == 0  → 重复包（正常情况不应出现，计入异常统计）

        Args:
            count_col: COUNT 字段完整列名，默认 'UL_DATA_PDU.u32pdcpCount'
            rbid_col:  RbId 字段完整列名，默认 'UL_DATA_PDU.u8rbId'

        Returns:
            dict: {
                'col_used'       : str,       # 实际使用的 COUNT 字段名
                'rbid_col_used'  : str,       # 实际使用的 RbId 字段名（None 表示未找到）
                'total_pkts'     : int,       # 总记录包数
                'total_lost'     : int,       # 估算总丢包数
                'loss_rate'      : float,     # 全局丢包率 = lost / (received + lost)
                'dup_pkts'       : int,       # 重复 COUNT 数（Δ==0，异常情况）
                'loss_events'    : int,       # 丢包事件次数
                'per_instance'   : DataFrame, # 各 (UEGID, RbId) 实例统计汇总
                'loss_detail'    : DataFrame, # 每次丢包事件详情
            }

            per_instance 列说明：
                UEGID, rbId, total_pkts, total_lost, loss_rate,
                loss_events, dup_pkts, first_count, last_count

            loss_detail 列说明：
                UEGID, rbId, prev_count, curr_count, gap, lost_pkts,
                airtime_prev, airtime_curr, local_time
        """
        col  = count_col or self.COUNT_COL
        rcol = rbid_col  or self.RBID_COL
        df   = self._data()

        empty_ret = {
            'col_used'      : col,
            'rbid_col_used' : None,
            'total_pkts'    : 0,
            'total_lost'    : 0,
            'loss_rate'     : 0.0,
            'dup_pkts'      : 0,
            'loss_events'   : 0,
            'per_instance'  : pd.DataFrame(),
            'loss_detail'   : pd.DataFrame(),
        }

        if df.empty or col not in df.columns:
            return empty_ret

        # 收集需要的列（COUNT 必须有；RbId / UEGID / AirTime / LocalTime 可选）
        want_cols = [col]
        has_uegid = 'UEGID' in df.columns
        has_rbid  = rcol in df.columns
        if has_uegid:
            want_cols = ['UEGID'] + want_cols
        if has_rbid:
            want_cols.append(rcol)
        if 'u32AirTime' in df.columns:
            want_cols.append('u32AirTime')
        if 'LocalTime' in df.columns:
            want_cols.append('LocalTime')

        sub = df[[c for c in want_cols if c in df.columns]].copy()
        sub = sub.dropna(subset=[col])
        sub[col] = sub[col].astype(np.int64)

        if sub.empty:
            return empty_ret

        # 确定分组键：优先 (UEGID, RbId)，其次 UEGID，再次全局
        if has_uegid and has_rbid:
            group_keys = ['UEGID', rcol]
        elif has_uegid:
            group_keys = ['UEGID']
        else:
            group_keys = None

        groups = sub.groupby(group_keys) if group_keys else [((None, None), sub)]

        per_instance_list = []
        loss_detail_list  = []
        total_pkts_all  = 0
        total_lost_all  = 0
        dup_pkts_all    = 0
        loss_events_all = 0

        for key, grp in groups:
            # 解包分组键
            if group_keys and len(group_keys) == 2:
                uegid, rbid = key
            elif group_keys and len(group_keys) == 1:
                uegid, rbid = key, None
            else:
                uegid, rbid = None, None

            sort_key = 'u32AirTime' if 'u32AirTime' in grp.columns else col
            grp_sorted = grp.sort_values(sort_key).reset_index(drop=True)

            counts = grp_sorted[col].values
            n = len(counts)
            total_pkts_all += n

            if n < 2:
                per_instance_list.append({
                    'UEGID'       : uegid,
                    'rbId'        : rbid,
                    'total_pkts'  : n,
                    'total_lost'  : 0,
                    'loss_rate'   : 0.0,
                    'loss_events' : 0,
                    'dup_pkts'    : 0,
                    'first_count' : int(counts[0]) if n > 0 else None,
                    'last_count'  : int(counts[-1]) if n > 0 else None,
                })
                continue

            # 计算相邻 COUNT 差值，处理 32-bit 回绕
            diffs = np.diff(counts)
            wrap_mask = diffs < -(self.PDCP_COUNT_MAX // 2)
            diffs[wrap_mask] += self.PDCP_COUNT_MAX

            dup_mask  = diffs == 0
            loss_mask = diffs > 1

            dup_cnt   = int(dup_mask.sum())
            loss_ev   = int(loss_mask.sum())
            lost_pkts = int((diffs[loss_mask] - 1).sum())

            total_lost_all  += lost_pkts
            dup_pkts_all    += dup_cnt
            loss_events_all += loss_ev

            loss_rate = lost_pkts / (n + lost_pkts) if (n + lost_pkts) > 0 else 0.0

            per_instance_list.append({
                'UEGID'       : uegid,
                'rbId'        : rbid,
                'total_pkts'  : n,
                'total_lost'  : lost_pkts,
                'loss_rate'   : round(loss_rate, 6),
                'loss_events' : loss_ev,
                'dup_pkts'    : dup_cnt,
                'first_count' : int(counts[0]),
                'last_count'  : int(counts[-1]),
            })

            loss_indices = np.where(loss_mask)[0]
            for idx in loss_indices:
                detail = {
                    'UEGID'      : uegid,
                    'rbId'       : rbid,
                    'prev_count' : int(counts[idx]),
                    'curr_count' : int(counts[idx + 1]),
                    'gap'        : int(diffs[idx]),
                    'lost_pkts'  : int(diffs[idx] - 1),
                }
                if 'u32AirTime' in grp_sorted.columns:
                    detail['airtime_prev'] = grp_sorted['u32AirTime'].iloc[idx]
                    detail['airtime_curr'] = grp_sorted['u32AirTime'].iloc[idx + 1]
                if 'LocalTime' in grp_sorted.columns:
                    detail['local_time'] = grp_sorted['LocalTime'].iloc[idx + 1]
                loss_detail_list.append(detail)

        global_loss_rate = (total_lost_all / (total_pkts_all + total_lost_all)
                            if (total_pkts_all + total_lost_all) > 0 else 0.0)

        detail_df = pd.DataFrame()
        if loss_detail_list:
            detail_df = pd.DataFrame(loss_detail_list)
            sort_cols = ['UEGID', 'rbId']
            if 'airtime_prev' in detail_df.columns:
                sort_cols.append('airtime_prev')
            detail_df = detail_df.sort_values(
                [c for c in sort_cols if c in detail_df.columns]
            ).reset_index(drop=True)

        return {
            'col_used'      : col,
            'rbid_col_used' : rcol if has_rbid else None,
            'total_pkts'    : total_pkts_all,
            'total_lost'    : total_lost_all,
            'loss_rate'     : round(global_loss_rate, 6),
            'dup_pkts'      : dup_pkts_all,
            'loss_events'   : loss_events_all,
            'per_instance'  : pd.DataFrame(per_instance_list) if per_instance_list else pd.DataFrame(),
            'loss_detail'   : detail_df,
        }

    def throughput_ul(self):
        """
        PDCP 上行吞吐量概要（来自 KPI 字段）

        Returns:
            dict: {'total_sdu_num', 'total_bytes', 'loss_rate'}
        """
        df = self._data()
        num_col  = 'PDCP_KPI.u32ulPdcpRcvSduNum'
        byte_col = 'PDCP_KPI.u32ulPdcpRcvSduByte'
        lost_col = 'PDCP_KPI.u32ulPdcpLostSduNum'
        result = {}
        kpi_cols = [c for c in [num_col, byte_col, lost_col] if c in df.columns]
        if not kpi_cols:
            return result
        kpi = df[kpi_cols].dropna(how='all')
        if num_col in kpi.columns:
            result['total_sdu_num'] = int(kpi[num_col].sum())
        if byte_col in kpi.columns:
            result['total_bytes'] = int(kpi[byte_col].sum())
        if lost_col in kpi.columns and num_col in kpi.columns:
            tot = kpi[num_col].sum() + kpi[lost_col].sum()
            result['loss_rate'] = float(kpi[lost_col].sum()) / tot if tot > 0 else 0.0
        return result

    def describe(self):
        """概要描述：数据行数、UEGID、时间范围"""
        df = self._data()
        if df.empty:
            return "No PDCP UL data found."
        uegids = sorted(df['UEGID'].dropna().unique().astype(int)) if 'UEGID' in df.columns else []
        cells = sorted(df['CellId'].dropna().unique().astype(int)) if 'CellId' in df.columns else []
        lt = df['LocalTime'].dropna() if 'LocalTime' in df.columns else pd.Series()
        t_range = f"{lt.iloc[0]} ~ {lt.iloc[-1]}" if len(lt) >= 2 else 'N/A'
        data_rows = df[self.UL_DELAY_COLS[0]].dropna().shape[0] if self.UL_DELAY_COLS[0] in df.columns else 0
        return {
            'total_rows': len(df),
            'data_pdu_rows': data_rows,
            'uegids': uegids,
            'cellids': cells,
            'time_range': t_range,
        }


# ============================================================
# PdcpDl — PDCP 下行分析
# ============================================================

class PdcpDl:
    """
    PDCP 下行日志分析类（PDCP_dlUeTtiInfo）

    注意：PDCP 下行日志中没有 pdcpCost/rlcCost 字段；
    下行时延信息在 RLC 下行日志（DL_DATA_PDU.u32PdcpCost, u32RlcCost）中。

    关键字段（DL_DATA_SDU）：
        DL_DATA_SDU.u16dataLen    — SDU 长度（字节）
        DL_DATA_SDU.u32pdcpSn     — PDCP SN
        DL_DATA_SDU.u32txNext     — 下一个待发 SN（窗口前沿）
        DL_DATA_SDU.u32txDeliv    — 已确认发送的最大连续 SN（窗口下沿，RLC 反馈）
        DL_DATA_SDU.u32rlcBuffSize— RLC 发送缓冲区大小（包数）
        DL_DATA_SDU.u32sduCurNum  — PDCP SDU 队列当前积压数
    窗口积压 = txNext - txDeliv
    """

    WINDOW_COLS = ['DL_DATA_SDU.u32txNext', 'DL_DATA_SDU.u32txDeliv']
    BUFFER_COLS = ['DL_DATA_SDU.u32rlcBuffSize', 'DL_DATA_SDU.u32sduCurNum']
    F1U_COLS    = ['DL_F1U_DATA.u32rlcBufSize',
                   'DL_F1U_DATA.u32maxdeliverycount',
                   'DL_F1U_DATA.u32maxtransmitcount']
    # PDCP COUNT 字段（协议规定全局单调递增，32-bit，无重复）
    COUNT_COL      = 'DL_DATA_SDU.u32pdcpCount'
    # RbId 字段（PDCP 实例粒度为 UEGID + RbId + 方向）
    RBID_COL       = 'DL_DATA_SDU.u8rbId'
    PDCP_COUNT_MAX = 2 ** 32

    def __init__(self, directory, id_filter=None, time_interval=None):
        self._dir = directory
        self._id_filter = id_filter or {}
        self._time_interval = time_interval
        self._df = None

    def _data(self):
        if self._df is None:
            self._df = _load_csvs(
                self._dir, NR_FILE_PDCP_DL,
                id_filter=self._id_filter,
                time_interval=self._time_interval,
            )
        return self._df

    def window_backlog(self):
        """
        下行 PDCP 窗口积压（txNext - txDeliv，已发送但未被 RLC 确认的包数）

        Returns:
            Series: values=积压包数
        """
        df = self._data()
        if not all(c in df.columns for c in self.WINDOW_COLS):
            return pd.Series(dtype=float, name='dl_pdcp_window_backlog')
        sub = df[self.WINDOW_COLS].dropna()
        backlog = sub.iloc[:, 0] - sub.iloc[:, 1]
        backlog.name = 'dl_pdcp_window_backlog'
        return backlog

    def window_timeseries(self, time_bin=1):
        """
        下行 PDCP 窗口时序（txNext/txDeliv/backlog/rlcBuffSize/sduCurNum）

        Returns:
            DataFrame: index=time
        """
        df = self._data()
        want = ['LocalTime'] + self.WINDOW_COLS + self.BUFFER_COLS
        cols = [c for c in want if c in df.columns]
        sub = df[cols].dropna(subset=self.WINDOW_COLS).copy()
        if sub.empty:
            return pd.DataFrame()
        try:
            sub['_t'] = pd.to_datetime(sub['LocalTime'].str.strip(), format='%H:%M:%S:%f', errors='coerce')
        except Exception:
            return pd.DataFrame()
        sub = sub.dropna(subset=['_t']).set_index('_t').drop(columns=['LocalTime'], errors='ignore')
        sub['backlog'] = sub['DL_DATA_SDU.u32txNext'] - sub['DL_DATA_SDU.u32txDeliv']
        rlt = sub.resample(f'{time_bin}s').mean()
        short = {c: c.split('.')[-1].replace('u32', '').replace('u16', '') for c in rlt.columns if c != 'backlog'}
        rlt.rename(columns=short, inplace=True)
        return rlt

    def rlc_buffer_timeseries(self, time_bin=1):
        """
        RLC 缓冲区大小时序（来自 DL_F1U_DATA，每秒 KPI）

        Returns:
            DataFrame: index=time, columns=[rlcBufSize, maxdeliverycount, maxtransmitcount]
        """
        df = self._data()
        cols = [c for c in self.F1U_COLS if c in df.columns]
        if not cols:
            return pd.DataFrame()
        lt = 'LocalTime'
        if lt not in df.columns:
            return pd.DataFrame()
        sub = df[[lt] + cols].dropna(subset=cols).copy()
        try:
            sub['_t'] = pd.to_datetime(sub[lt].str.strip(), format='%H:%M:%S:%f', errors='coerce')
        except Exception:
            return pd.DataFrame()
        sub = sub.dropna(subset=['_t']).set_index('_t').drop(columns=[lt])
        rlt = sub.resample(f'{time_bin}s').mean()
        short = {c: c.split('.')[-1].replace('u32', '') for c in rlt.columns}
        rlt.rename(columns=short, inplace=True)
        return rlt

    def pdcp_count_loss(self, count_col=None, rbid_col=None):
        """
        基于 PDCP COUNT（u32pdcpCount）检测下行 PDCP 层真实丢包。

        PDCP 实例的粒度是 UEGID + RbId + 方向（UL/DL），每个实例的 COUNT
        按协议规定独立单调递增、不重复（32-bit）。
        按 (UEGID, RbId) 分组后，对 COUNT 序列排序，计算相邻差值 Δ：
          Δ == 1  → 正常，连续无丢包
          Δ  > 1  → 丢包，本次事件丢失 Δ-1 个包
          Δ  < 0  → COUNT 32-bit 回绕，修正后重新计算
          Δ == 0  → 重复包（正常情况不应出现，计入异常统计）

        Args:
            count_col: COUNT 字段完整列名，默认 'DL_DATA_SDU.u32pdcpCount'
            rbid_col:  RbId 字段完整列名，默认 'DL_DATA_SDU.u8rbId'

        Returns:
            dict: {
                'col_used'       : str,
                'rbid_col_used'  : str,
                'total_pkts'     : int,
                'total_lost'     : int,
                'loss_rate'      : float,
                'dup_pkts'       : int,
                'loss_events'    : int,
                'per_instance'   : DataFrame,   # 各 (UEGID, RbId) 实例统计
                'loss_detail'    : DataFrame,   # 每次丢包事件详情
            }
            （字段说明同 PdcpUl.pdcp_count_loss()，方向为下行）
        """
        col  = count_col or self.COUNT_COL
        rcol = rbid_col  or self.RBID_COL
        df   = self._data()

        empty_ret = {
            'col_used'      : col,
            'rbid_col_used' : None,
            'total_pkts'    : 0,
            'total_lost'    : 0,
            'loss_rate'     : 0.0,
            'dup_pkts'      : 0,
            'loss_events'   : 0,
            'per_instance'  : pd.DataFrame(),
            'loss_detail'   : pd.DataFrame(),
        }

        if df.empty or col not in df.columns:
            return empty_ret

        want_cols = [col]
        has_uegid = 'UEGID' in df.columns
        has_rbid  = rcol in df.columns
        if has_uegid:
            want_cols = ['UEGID'] + want_cols
        if has_rbid:
            want_cols.append(rcol)
        if 'u32AirTime' in df.columns:
            want_cols.append('u32AirTime')
        if 'LocalTime' in df.columns:
            want_cols.append('LocalTime')

        sub = df[[c for c in want_cols if c in df.columns]].copy()
        sub = sub.dropna(subset=[col])
        sub[col] = sub[col].astype(np.int64)

        if sub.empty:
            return empty_ret

        if has_uegid and has_rbid:
            group_keys = ['UEGID', rcol]
        elif has_uegid:
            group_keys = ['UEGID']
        else:
            group_keys = None

        groups = sub.groupby(group_keys) if group_keys else [((None, None), sub)]

        per_instance_list = []
        loss_detail_list  = []
        total_pkts_all  = 0
        total_lost_all  = 0
        dup_pkts_all    = 0
        loss_events_all = 0

        for key, grp in groups:
            if group_keys and len(group_keys) == 2:
                uegid, rbid = key
            elif group_keys and len(group_keys) == 1:
                uegid, rbid = key, None
            else:
                uegid, rbid = None, None

            sort_key = 'u32AirTime' if 'u32AirTime' in grp.columns else col
            grp_sorted = grp.sort_values(sort_key).reset_index(drop=True)

            counts = grp_sorted[col].values
            n = len(counts)
            total_pkts_all += n

            if n < 2:
                per_instance_list.append({
                    'UEGID'       : uegid,
                    'rbId'        : rbid,
                    'total_pkts'  : n,
                    'total_lost'  : 0,
                    'loss_rate'   : 0.0,
                    'loss_events' : 0,
                    'dup_pkts'    : 0,
                    'first_count' : int(counts[0]) if n > 0 else None,
                    'last_count'  : int(counts[-1]) if n > 0 else None,
                })
                continue

            diffs = np.diff(counts)
            wrap_mask = diffs < -(self.PDCP_COUNT_MAX // 2)
            diffs[wrap_mask] += self.PDCP_COUNT_MAX

            dup_mask  = diffs == 0
            loss_mask = diffs > 1

            dup_cnt   = int(dup_mask.sum())
            loss_ev   = int(loss_mask.sum())
            lost_pkts = int((diffs[loss_mask] - 1).sum())

            total_lost_all  += lost_pkts
            dup_pkts_all    += dup_cnt
            loss_events_all += loss_ev

            loss_rate = lost_pkts / (n + lost_pkts) if (n + lost_pkts) > 0 else 0.0

            per_instance_list.append({
                'UEGID'       : uegid,
                'rbId'        : rbid,
                'total_pkts'  : n,
                'total_lost'  : lost_pkts,
                'loss_rate'   : round(loss_rate, 6),
                'loss_events' : loss_ev,
                'dup_pkts'    : dup_cnt,
                'first_count' : int(counts[0]),
                'last_count'  : int(counts[-1]),
            })

            loss_indices = np.where(loss_mask)[0]
            for idx in loss_indices:
                detail = {
                    'UEGID'      : uegid,
                    'rbId'       : rbid,
                    'prev_count' : int(counts[idx]),
                    'curr_count' : int(counts[idx + 1]),
                    'gap'        : int(diffs[idx]),
                    'lost_pkts'  : int(diffs[idx] - 1),
                }
                if 'u32AirTime' in grp_sorted.columns:
                    detail['airtime_prev'] = grp_sorted['u32AirTime'].iloc[idx]
                    detail['airtime_curr'] = grp_sorted['u32AirTime'].iloc[idx + 1]
                if 'LocalTime' in grp_sorted.columns:
                    detail['local_time'] = grp_sorted['LocalTime'].iloc[idx + 1]
                loss_detail_list.append(detail)

        global_loss_rate = (total_lost_all / (total_pkts_all + total_lost_all)
                            if (total_pkts_all + total_lost_all) > 0 else 0.0)

        detail_df = pd.DataFrame()
        if loss_detail_list:
            detail_df = pd.DataFrame(loss_detail_list)
            sort_cols = ['UEGID', 'rbId']
            if 'airtime_prev' in detail_df.columns:
                sort_cols.append('airtime_prev')
            detail_df = detail_df.sort_values(
                [c for c in sort_cols if c in detail_df.columns]
            ).reset_index(drop=True)

        return {
            'col_used'      : col,
            'rbid_col_used' : rcol if has_rbid else None,
            'total_pkts'    : total_pkts_all,
            'total_lost'    : total_lost_all,
            'loss_rate'     : round(global_loss_rate, 6),
            'dup_pkts'      : dup_pkts_all,
            'loss_events'   : loss_events_all,
            'per_instance'  : pd.DataFrame(per_instance_list) if per_instance_list else pd.DataFrame(),
            'loss_detail'   : detail_df,
        }

    def sdu_stats(self):
        """
        下行 SDU 长度统计

        Returns:
            dict: {'count', 'mean_bytes', 'min_bytes', 'max_bytes'}
        """
        df = self._data()
        col = 'DL_DATA_SDU.u16dataLen'
        if col not in df.columns:
            return {}
        s = df[col].dropna()
        return {
            'count': int(len(s)),
            'mean_bytes': float(s.mean()),
            'min_bytes': float(s.min()),
            'max_bytes': float(s.max()),
        }

    def describe(self):
        df = self._data()
        if df.empty:
            return "No PDCP DL data found."
        uegids = sorted(df['UEGID'].dropna().unique().astype(int)) if 'UEGID' in df.columns else []
        lt = df['LocalTime'].dropna() if 'LocalTime' in df.columns else pd.Series()
        t_range = f"{lt.iloc[0]} ~ {lt.iloc[-1]}" if len(lt) >= 2 else 'N/A'
        sdu_rows = df['DL_DATA_SDU.u16dataLen'].dropna().shape[0] if 'DL_DATA_SDU.u16dataLen' in df.columns else 0
        return {
            'total_rows': len(df),
            'sdu_rows': sdu_rows,
            'uegids': uegids,
            'time_range': t_range,
        }


# ============================================================
# PdcpAnalysis — 组合入口
# ============================================================

class PdcpAnalysis:
    """
    PDCP 综合分析入口，同时管理上下行分析实例。

    Usage:
        pdcp = PdcpAnalysis(directory)
        # 基础时延/窗口分析
        pdcp.ul.ul_delay_stats()
        pdcp.dl.window_timeseries()
        # TCP 分析
        pdcp.tcp_ul.port_stats()
        pdcp.tcp_dl.retransmit_rate()
        # ICMP ping RTT
        pdcp.icmp.rtt_stats()
        pdcp.icmp.rtt_timeseries()
    """

    def __init__(self, directory, uegid=None, cellid=None, time_interval=None):
        id_filter = {}
        if uegid is not None:
            id_filter['UEGID'] = [uegid] if not isinstance(uegid, list) else uegid
        if cellid is not None:
            id_filter['CellId'] = [cellid] if not isinstance(cellid, list) else cellid

        _id = id_filter if id_filter else None

        self.ul     = PdcpUl(directory, id_filter=_id, time_interval=time_interval)
        self.dl     = PdcpDl(directory, id_filter=_id, time_interval=time_interval)
        self.tcp_ul = PdcpTcp(directory, direction='ul', id_filter=_id,
                               time_interval=time_interval)
        self.tcp_dl = PdcpTcp(directory, direction='dl', id_filter=_id,
                               time_interval=time_interval)
        self.icmp   = PdcpIcmp(directory, id_filter=_id, time_interval=time_interval)

        self._directory = directory
        self._uegid     = uegid
        self._cellid    = cellid

    @property
    def directory(self):
        return self._directory

    def summary(self):
        """打印上下行基础概要"""
        print("=== PDCP UL ===")
        print(self.ul.describe())
        print("\n=== PDCP DL ===")
        print(self.dl.describe())

    def count_loss(self, ul_count_col=None, dl_count_col=None):
        """
        基于 PDCP COUNT（u32pdcpCount）同时分析上下行丢包情况。

        PDCP COUNT 是按协议规定单调递增、全局不重复的 32-bit 计数器。
        通过检测相邻 COUNT 的连续性，可精确定位：
          - 哪个 UE 发生丢包
          - 丢包时刻（AirTime / LocalTime）
          - 每次事件丢失了多少包
          - 全局/各 UE 的丢包率

        Args:
            ul_count_col: 上行 COUNT 字段名，默认 'UL_DATA_PDU.u32pdcpCount'
            dl_count_col: 下行 COUNT 字段名，默认 'DL_DATA_SDU.u32pdcpCount'

        Returns:
            dict: {
                'ul': dict,   # PdcpUl.pdcp_count_loss() 结果
                'dl': dict,   # PdcpDl.pdcp_count_loss() 结果
            }
        """
        ul_result = self.ul.pdcp_count_loss(count_col=ul_count_col)
        dl_result = self.dl.pdcp_count_loss(count_col=dl_count_col)
        return {'ul': ul_result, 'dl': dl_result}

    def print_count_loss(self, ul_count_col=None, dl_count_col=None):
        """
        打印 PDCP COUNT 丢包分析摘要（上下行对比表格）。

        直接输出可读的汇总信息和各 UE 详情，方便快速查看。
        """
        result = self.count_loss(ul_count_col=ul_count_col,
                                 dl_count_col=dl_count_col)
        for direction, res in [('UL（上行）', result['ul']),
                                ('DL（下行）', result['dl'])]:
            print(f"\n{'=' * 50}")
            print(f"  PDCP COUNT 丢包分析 — {direction}")
            print(f"{'=' * 50}")
            print(f"  字段       : {res['col_used']}")
            rbid_col = res.get('rbid_col_used')
            if rbid_col:
                print(f"  RbId字段   : {rbid_col}")
            print(f"  收到总包数 : {res['total_pkts']:,}")
            print(f"  丢包估算   : {res['total_lost']:,}  （事件数: {res['loss_events']}）")
            print(f"  丢包率     : {res['loss_rate'] * 100:.4f}%")
            if res['dup_pkts'] > 0:
                print(f"  重复包(异常): {res['dup_pkts']:,}")

            pu = res['per_instance']
            if not pu.empty:
                lost_ue = pu[pu['total_lost'] > 0]
                if lost_ue.empty:
                    print("  ✅ 各 PDCP 实例均无 COUNT 跳变，无丢包")
                else:
                    print(f"\n  有丢包的 PDCP 实例（共 {len(lost_ue)} 个）：")
                    show_cols = [c for c in ['UEGID', 'rbId', 'total_pkts', 'total_lost',
                                              'loss_rate', 'loss_events'] if c in lost_ue.columns]
                    print(lost_ue[show_cols].to_string(index=False))

            detail = res['loss_detail']
            if not detail.empty:
                print(f"\n  丢包事件明细（前 20 条）：")
                show_cols = [c for c in ['UEGID', 'rbId', 'prev_count', 'curr_count',
                                         'gap', 'lost_pkts', 'airtime_curr',
                                         'local_time'] if c in detail.columns]
                print(detail[show_cols].head(20).to_string(index=False))

    def tcp_summary(self):
        """打印 TCP 上下行分析摘要"""
        print("=== PDCP TCP UL ===")
        print(self.tcp_ul.describe())
        print("\n=== PDCP TCP DL ===")
        print(self.tcp_dl.describe())

    def icmp_summary(self):
        """打印 ICMP ping RTT 摘要"""
        print("=== PDCP ICMP (ping RTT) ===")
        print(self.icmp.describe())


# ============================================================
# PdcpTcp — TCP 报文分析（上下行通用）
# ============================================================

# TCP Flag 位掩码
_TCP_FLAG_FIN = 0x01
_TCP_FLAG_SYN = 0x02
_TCP_FLAG_RST = 0x04
_TCP_FLAG_PSH = 0x08
_TCP_FLAG_ACK = 0x10
_TCP_FLAG_URG = 0x20


class PdcpTcp:
    """
    TCP 报文分析类（上下行通用）。

    支持从 UL 日志（UL_DATA_PDU.*）或 DL 日志（DL_DATA_SDU.*）中
    提取 TCP 字段，进行流量、端口、重传、标志位统计。

    有效 TCP 报文判断：u32seq != 0

    TCP Flag（仅 DL 日志有 u8tcpFlag）：
        bit0=FIN  bit1=SYN  bit2=RST  bit3=PSH  bit4=ACK  bit5=URG

    Parameters:
        directory  : 日志目录
        direction  : 'ul' 或 'dl'（决定加载哪个文件前缀、使用哪组字段）
        id_filter  : dict，如 {'UEGID': [23]}
        time_interval: [start_str, end_str]
    """

    # UL 字段（UL_DATA_PDU.*，无 tcpFlag）
    _UL_PREFIX   = 'UL_DATA_PDU'
    _UL_SRCPORT  = 'UL_DATA_PDU.u16srcport'
    _UL_DSTPORT  = 'UL_DATA_PDU.u16dstport'
    _UL_SEQ      = 'UL_DATA_PDU.u32seq'
    _UL_ACKNO    = 'UL_DATA_PDU.u32ackno'
    _UL_DATALEN  = 'UL_DATA_PDU.u16dataLen'

    # DL 字段（DL_DATA_SDU.*，含 tcpFlag）
    _DL_PREFIX   = 'DL_DATA_SDU'
    _DL_SRCPORT  = 'DL_DATA_SDU.u16srcport'
    _DL_DSTPORT  = 'DL_DATA_SDU.u16dstport'
    _DL_SEQ      = 'DL_DATA_SDU.u32seq'
    _DL_ACKNO    = 'DL_DATA_SDU.u32ackno'
    _DL_TCPFLAG  = 'DL_DATA_SDU.u8tcpFlag'
    _DL_DATALEN  = 'DL_DATA_SDU.u16dataLen'

    def __init__(self, directory, direction='dl', id_filter=None, time_interval=None):
        direction = direction.lower()
        if direction not in ('ul', 'dl'):
            raise ValueError("direction must be 'ul' or 'dl'")
        self._dir       = directory
        self._dir_str   = direction
        self._id_filter = id_filter or {}
        self._time_interval = time_interval
        self._df_raw    = None   # 原始加载 DataFrame（全字段）
        self._df_tcp    = None   # 已过滤只含 TCP 行

        if direction == 'ul':
            self._file_prefix = NR_FILE_PDCP_UL
            self._col_srcport = self._UL_SRCPORT
            self._col_dstport = self._UL_DSTPORT
            self._col_seq     = self._UL_SEQ
            self._col_ackno   = self._UL_ACKNO
            self._col_datalen = self._UL_DATALEN
            self._col_flag    = None   # UL 日志无 tcpFlag
        else:
            self._file_prefix = NR_FILE_PDCP_DL
            self._col_srcport = self._DL_SRCPORT
            self._col_dstport = self._DL_DSTPORT
            self._col_seq     = self._DL_SEQ
            self._col_ackno   = self._DL_ACKNO
            self._col_datalen = self._DL_DATALEN
            self._col_flag    = self._DL_TCPFLAG

    # ----------------------------------------------------------
    # 内部：加载数据
    # ----------------------------------------------------------

    def _raw(self):
        """加载原始日志 DataFrame（含所有字段）。"""
        if self._df_raw is None:
            self._df_raw = _load_csvs(
                self._dir, self._file_prefix,
                id_filter=self._id_filter,
                time_interval=self._time_interval,
            )
        return self._df_raw

    def _tcp(self):
        """过滤出 TCP 行（u32seq != 0）。"""
        if self._df_tcp is None:
            df = self._raw()
            if df.empty or self._col_seq not in df.columns:
                self._df_tcp = pd.DataFrame()
            else:
                self._df_tcp = df[df[self._col_seq].fillna(0) != 0].copy()
        return self._df_tcp

    # ----------------------------------------------------------
    # 公开 API
    # ----------------------------------------------------------

    @property
    def direction(self):
        """返回方向字符串 'ul' 或 'dl'。"""
        return self._dir_str

    def has_tcp(self):
        """是否存在 TCP 报文。"""
        return not self._tcp().empty

    def tcp_count(self):
        """TCP 报文总数。"""
        return len(self._tcp())

    def port_stats(self, top_n=10):
        """
        端口统计：各 (srcport, dstport) 对的报文数量排名。

        Returns:
            DataFrame: columns=['srcport', 'dstport', 'count']，按 count 降序
        """
        df = self._tcp()
        if df.empty:
            return pd.DataFrame(columns=['srcport', 'dstport', 'count'])
        src = self._col_srcport
        dst = self._col_dstport
        avail = [c for c in [src, dst] if c in df.columns]
        if len(avail) < 2:
            return pd.DataFrame(columns=['srcport', 'dstport', 'count'])
        grp = df.groupby([src, dst]).size().reset_index(name='count')
        grp.columns = ['srcport', 'dstport', 'count']
        return grp.sort_values('count', ascending=False).head(top_n).reset_index(drop=True)

    def flag_stats(self):
        """
        TCP Flag 统计（仅 DL 方向有效，UL 返回空字典）。

        Returns:
            dict: {'SYN': n, 'ACK': n, 'RST': n, 'FIN': n, 'PSH': n, 'URG': n}
        """
        if self._col_flag is None:
            return {}
        df = self._tcp()
        if df.empty or self._col_flag not in df.columns:
            return {}
        flags = df[self._col_flag].fillna(0).astype(int)
        return {
            'FIN': int((flags & _TCP_FLAG_FIN).astype(bool).sum()),
            'SYN': int((flags & _TCP_FLAG_SYN).astype(bool).sum()),
            'RST': int((flags & _TCP_FLAG_RST).astype(bool).sum()),
            'PSH': int((flags & _TCP_FLAG_PSH).astype(bool).sum()),
            'ACK': int((flags & _TCP_FLAG_ACK).astype(bool).sum()),
            'URG': int((flags & _TCP_FLAG_URG).astype(bool).sum()),
        }

    def retransmit_count(self):
        """
        重传估算：同一 (srcport, dstport, seq, datalen) 四元组出现超过 1 次的报文数量之和。

        重传判断逻辑（上下行通用）：
        1. 纯ACK包（datalen=0）：SEQ不变是正常行为，SEQ重复不是重传
        2. 数据包（datalen>0）：SEQ重复才是真正的重传

        TCP双向传输场景：
        - 下行灌包：下行数据包 + 上行纯ACK包
        - 上行灌包：上行数据包 + 下行纯ACK包
        - 双向同时：上下行都有数据包 + 纯ACK包

        关键判断：通过数据长度（datalen）区分数据包和纯ACK包

        注意：此方法基于 PDCP 层 seq 号去重，仅为粗略估算。
             同一 TCP seq 重复出现在同一方向日志中即计为重传候选。

        Returns:
            int: 疑似重传报文数
        """
        df = self._tcp()
        src = self._col_srcport
        dst = self._col_dstport
        seq = self._col_seq
        datalen = self._col_datalen
        ack = self._col_ackno

        avail = [c for c in [src, dst, seq] if c in df.columns]
        if len(avail) < 3:
            return 0

        # 必须有数据长度字段，否则无法区分数据包和纯ACK包
        has_datalen = datalen in df.columns
        if not has_datalen:
            # 没有数据长度字段，无法区分数据包和纯ACK包
            # 保守策略：只统计SEQ重复，但可能包含纯ACK包
            counts = df.groupby(avail).size()
            repeated = counts[counts > 1]
            if repeated.empty:
                return 0
            return int((repeated - 1).sum())

        # 加入数据长度作为分组条件（关键！）
        avail.append(datalen)

        # 统计每个分组的出现次数
        counts = df.groupby(avail).size()

        # 找出重复的
        repeated = counts[counts > 1]

        if repeated.empty:
            return 0

        # 过滤掉纯ACK包（datalen=0）的重复
        retrans_count = 0
        for idx, count in repeated.items():
            if isinstance(idx, tuple):
                group_key = dict(zip(avail, idx))
                datalen_val = group_key.get(datalen, 0)
            else:
                group_key = {avail[0]: idx}
                datalen_val = 0

            # datalen=0是纯ACK包，SEQ重复是正常行为，不是重传
            if datalen_val == 0:
                continue

            # datalen>0是数据包，SEQ重复才是重传
            retrans_count += int(count - 1)

        return retrans_count

    def retransmit_rate(self):
        """
        重传率 = 疑似重传数 / TCP 报文总数。

        Returns:
            float: [0, 1]，无 TCP 数据时返回 None
        """
        total = self.tcp_count()
        if total == 0:
            return None
        return self.retransmit_count() / total

    def detect_tcp_retransmission(self, per_connection=True):
        """
        TCP 重传包检测：基于 TCP 序列号的字节流推进判断重传。

        原理：
          - 对同一条 TCP 连接，按时间排序后维护"发送方已推进到的最大字节位置"
            max_seq_end = 见过的最大 (seq + dataLen)
          - 某包的 seq + dataLen <= max_seq_end → 未带来新数据 → 纯ACK或重传
            - 其中 seq 已见过且 dataLen > 0 → 重传
            - dataLen == 0（或seq区间已完全覆盖）→ 纯ACK，不计入重传
          - 某包的 seq >= max_seq_end → 带来全新数据 → 原始数据包
          - 此判断不依赖固定的 dataLen 阈值，而是从 seq 字节流推断

        Args:
            per_connection: bool，是否按连接统计重传（True）或合并统计（False）

        Returns:
            dict: {
                'total_connections': int,        # 总连接数
                'connections_with_retrans': int, # 有重传的连接数
                'total_data_pkts': int,          # 原始数据包总数（seq推进的包）
                'total_retrans_pkts': int,       # 重传包总数
                'retrans_rate': float,           # 重传率（retrans / data_pkts）
                'retrans_details': DataFrame,    # 详细重传信息（仅 per_connection=True）
                'summary': DataFrame             # 按连接汇总的重传统计
            }
            retrans_details 列说明：
                - srcport, dstport: 端口对
                - UEGID: UE 标识
                - seq: TCP 序列号
                - data_len: 数据包长度（字节）
                - local_time: 时间戳
                - retrans_of_seq: 原始包的 seq（和本次重传的 seq 相同）

            summary 列说明：
                - srcport, dstport: 端口对
                - UEGID: UE 标识
                - total_pkts: 该连接总报文数（含纯ACK）
                - data_pkts: 推进了seq的原始数据包数
                - ack_pkts: 纯ACK包数（未推进seq）
                - retrans_pkts: 重传包数
                - retrans_rate: 重传率（retrans / data_pkts）
        """
        df = self._tcp()
        if df.empty:
            return {
                'total_connections': 0,
                'connections_with_retrans': 0,
                'total_data_pkts': 0,
                'total_retrans_pkts': 0,
                'retrans_rate': 0.0,
                'retrans_details': pd.DataFrame(),
                'summary': pd.DataFrame()
            }

        src = self._col_srcport
        dst = self._col_dstport
        seq = self._col_seq
        datalen_col = self._col_datalen

        required = [src, dst, seq]
        if not all(c in df.columns for c in required):
            return {
                'total_connections': 0,
                'connections_with_retrans': 0,
                'total_data_pkts': 0,
                'total_retrans_pkts': 0,
                'retrans_rate': 0.0,
                'retrans_details': pd.DataFrame(),
                'summary': pd.DataFrame()
            }

        extra = [c for c in ['UEGID', 'LocalTime', datalen_col] if c and c in df.columns]
        tcp_df = df[required + extra].copy()

        tcp_df[seq] = pd.to_numeric(tcp_df[seq], errors='coerce').fillna(0).astype(np.int64)
        tcp_df[src] = pd.to_numeric(tcp_df[src], errors='coerce').fillna(0).astype(int)
        tcp_df[dst] = pd.to_numeric(tcp_df[dst], errors='coerce').fillna(0).astype(int)
        if datalen_col in tcp_df.columns:
            tcp_df[datalen_col] = pd.to_numeric(tcp_df[datalen_col], errors='coerce').fillna(0).astype(int)

        # 只保留有效 TCP 报文（seq != 0）
        tcp_df = tcp_df[tcp_df[seq] != 0].copy()
        if tcp_df.empty:
            return {
                'total_connections': 0,
                'connections_with_retrans': 0,
                'total_data_pkts': 0,
                'total_retrans_pkts': 0,
                'retrans_rate': 0.0,
                'retrans_details': pd.DataFrame(),
                'summary': pd.DataFrame()
            }

        # 按时间排序（若有 LocalTime）
        if 'LocalTime' in tcp_df.columns:
            try:
                tcp_df['_t'] = pd.to_datetime(
                    tcp_df['LocalTime'].astype(str).str.strip(),
                    format='%H:%M:%S:%f', errors='coerce'
                )
                tcp_df = tcp_df.sort_values('_t').drop(columns=['_t'])
            except Exception:
                pass

        conn_cols = [src, dst]
        if 'UEGID' in tcp_df.columns:
            conn_cols.append('UEGID')

        total_connections = 0
        connections_with_retrans = 0
        total_data_pkts = 0
        total_retrans_pkts = 0
        retrans_details_list = []
        summary_list = []

        for conn_key, conn_group in tcp_df.groupby(conn_cols):
            total_connections += 1
            conn_group = conn_group.reset_index(drop=True)

            has_dlen = datalen_col in conn_group.columns

            max_seq_end = -1   # 该连接目前已推进到的最大字节位置
            data_pkts  = 0
            ack_pkts   = 0
            retrans_pkts = 0

            for _, row in conn_group.iterrows():
                s = int(row[seq])
                d = int(row[datalen_col]) if has_dlen else 0
                seq_end = s + d

                if seq_end <= max_seq_end:
                    # 未推进新字节
                    if d > 0:
                        # 带数据但没超过已知范围 → 重传
                        retrans_pkts += 1
                        if per_connection:
                            detail = {
                                src: int(conn_group[src].iloc[0]),
                                dst: int(conn_group[dst].iloc[0]),
                                'retrans_of_seq': s,
                                seq: s,
                                'data_len': d,
                            }
                            if 'UEGID' in row.index:
                                detail['UEGID'] = row['UEGID']
                            if 'LocalTime' in row.index:
                                detail['local_time'] = row['LocalTime']
                            retrans_details_list.append(detail)
                    else:
                        # dataLen == 0 或 seq范围已覆盖 → 纯ACK
                        ack_pkts += 1
                else:
                    # 推进了新字节 → 原始数据包
                    data_pkts += 1
                    max_seq_end = seq_end

            total_data_pkts  += data_pkts
            total_retrans_pkts += retrans_pkts

            conn_rate = retrans_pkts / data_pkts if data_pkts > 0 else 0.0
            if retrans_pkts > 0:
                connections_with_retrans += 1

            summary = {
                src: conn_key[0],
                dst: conn_key[1],
                'total_pkts':   len(conn_group),
                'data_pkts':    data_pkts,
                'ack_pkts':     ack_pkts,
                'retrans_pkts': retrans_pkts,
                'retrans_rate': conn_rate,
            }
            if 'UEGID' in tcp_df.columns:
                summary['UEGID'] = conn_key[2] if len(conn_key) > 2 else None
            summary_list.append(summary)

        retrans_rate = total_retrans_pkts / total_data_pkts if total_data_pkts > 0 else 0.0

        result = {
            'total_connections':     total_connections,
            'connections_with_retrans': connections_with_retrans,
            'total_data_pkts':       total_data_pkts,
            'total_retrans_pkts':    total_retrans_pkts,
            'retrans_rate':          retrans_rate,
        }

        if per_connection:
            col_mapping = {src: 'srcport', dst: 'dstport'}

            if retrans_details_list:
                rd_df = pd.DataFrame(retrans_details_list)
                rd_df.rename(columns=col_mapping, inplace=True)
                result['retrans_details'] = rd_df
            else:
                result['retrans_details'] = pd.DataFrame()

            if summary_list:
                sm_df = pd.DataFrame(summary_list)
                sm_df.rename(columns=col_mapping, inplace=True)
                result['summary'] = sm_df.sort_values('retrans_pkts', ascending=False).reset_index(drop=True)
            else:
                result['summary'] = pd.DataFrame()

        return result

    def flow_timeseries(self, time_bin=1):
        """
        TCP 流量时序：每 time_bin 秒的报文数量和字节数（bytes 仅在 dataLen 字段存在时有效）。

        Returns:
            DataFrame: index=time, columns=['pkt_count'] 或 ['pkt_count', 'bytes']
        """
        df = self._tcp()
        if df.empty or 'LocalTime' not in df.columns:
            return pd.DataFrame()

        have_len = self._col_datalen in df.columns
        want_cols = ['LocalTime'] + ([self._col_datalen] if have_len else [])
        sub = df[want_cols].copy()
        try:
            sub['_t'] = pd.to_datetime(sub['LocalTime'].str.strip(),
                                       format='%H:%M:%S:%f', errors='coerce')
        except Exception:
            return pd.DataFrame()
        sub = sub.dropna(subset=['_t']).set_index('_t').drop(columns=['LocalTime'])

        if have_len:
            result = pd.DataFrame({
                'pkt_count': sub.resample(f'{time_bin}s')[self._col_datalen].count(),
                'bytes':     sub.resample(f'{time_bin}s')[self._col_datalen].sum(),
            })
        else:
            # 没有 dataLen 时，用 index 计数
            result = sub.resample(f'{time_bin}s').size().rename('pkt_count').to_frame()
        return result

    def detect_tcp_packet_loss(self, per_connection=True, anomaly_gap=100000):
        """
        TCP 丢包检测：基于 TCP 序列号字节流的连续性分析。

        原理：
          - 对同一条 TCP 连接，按时间排序后维护"发送方已推进到的最大字节位置"
            max_seq_end = 见过的最大 (seq + dataLen)
          - seq + dataLen > max_seq_end 的包 → 原始数据包（推进了字节流）
          - seq + dataLen <= max_seq_end 且 dataLen > 0 → 重传/乱序（不参与丢包检测）
          - dataLen == 0 → 纯ACK（不参与丢包检测）
          - 不依赖固定的 dataLen 阈值，完全从 seq 字节流推断
          - 将原始数据包按 seq 升序排列后，逐段检测：
            gap = next_seq - (prev_seq + prev_dataLen)
            gap > 0 且 gap <= anomaly_gap → 真实丢包（缺失 gap 字节）
            gap > anomaly_gap → seq回绕/重连，单独输出到 anomaly_details

        Args:
            per_connection: bool，是否按连接输出详细信息（True）或只返回汇总（False）
            anomaly_gap: int，seq字节跳跃超过此阈值视为异常（默认100000字节）

        Returns:
            dict: {
                'total_connections': int,      # 总连接数
                'connections_with_loss': int,   # 有丢包的连接数
                'loss_events': int,             # 总丢包事件数（断点数）
                'loss_details': DataFrame,      # 详细丢包信息（仅 per_connection=True）
                'anomaly_details': DataFrame,   # 异常跳跃信息（seq回绕/重连等）
                'summary': DataFrame            # 按连接汇总的丢包统计
            }
            loss_details 列说明：
                - srcport, dstport: 端口对
                - UEGID: UE 标识
                - prev_seq: 前一个原始数据包的 seq
                - curr_seq: 当前原始数据包的 seq
                - seq_gap: curr_seq - (prev_seq + prev_dataLen)，即缺失字节数
                - missing_bytes: 同 seq_gap
                - local_time: curr_seq 对应的时间戳

            anomaly_details 列说明：
                - srcport, dstport, UEGID: 标识
                - prev_seq, curr_seq, seq_gap: seq 跳跃信息
                - local_time: 时间戳
                - note: 异常描述

            summary 列说明：
                - srcport, dstport: 端口对
                - total_pkts: 该连接总报文数
                - data_pkts: 推进了seq的原始数据包数
                - ack_pkts: 纯ACK包数（seq未推进）
                - retrans_pkts: 重传/乱序包数
                - loss_events: 丢包事件次数（断点数）
                - total_lost_bytes: 累计丢失字节数
        """
        df = self._tcp()
        if df.empty:
            return {
                'total_connections': 0,
                'connections_with_loss': 0,
                'loss_events': 0,
                'loss_details': pd.DataFrame(),
                'anomaly_details': pd.DataFrame(),
                'summary': pd.DataFrame()
            }

        src = self._col_srcport
        dst = self._col_dstport
        seq = self._col_seq
        datalen_col = self._col_datalen

        if not all(c in df.columns for c in [src, dst, seq]):
            return {
                'total_connections': 0,
                'connections_with_loss': 0,
                'loss_events': 0,
                'loss_details': pd.DataFrame(),
                'anomaly_details': pd.DataFrame(),
                'summary': pd.DataFrame()
            }

        extra_cols = [c for c in [datalen_col, 'UEGID', 'LocalTime'] if c and c in df.columns]
        tcp_df = df[[src, dst, seq] + extra_cols].copy()

        tcp_df[seq] = pd.to_numeric(tcp_df[seq], errors='coerce').fillna(0).astype(np.int64)
        tcp_df[src] = pd.to_numeric(tcp_df[src], errors='coerce').fillna(0).astype(int)
        tcp_df[dst] = pd.to_numeric(tcp_df[dst], errors='coerce').fillna(0).astype(int)
        if datalen_col and datalen_col in tcp_df.columns:
            tcp_df[datalen_col] = pd.to_numeric(tcp_df[datalen_col], errors='coerce').fillna(0).astype(int)

        tcp_df = tcp_df[tcp_df[seq] != 0].copy()
        if tcp_df.empty:
            return {
                'total_connections': 0,
                'connections_with_loss': 0,
                'loss_events': 0,
                'loss_details': pd.DataFrame(),
                'anomaly_details': pd.DataFrame(),
                'summary': pd.DataFrame()
            }

        # 按时间排序
        if 'LocalTime' in tcp_df.columns:
            try:
                tcp_df['_t'] = pd.to_datetime(
                    tcp_df['LocalTime'].astype(str).str.strip(),
                    format='%H:%M:%S:%f', errors='coerce'
                )
                tcp_df = tcp_df.sort_values('_t').drop(columns=['_t'])
            except Exception:
                pass

        conn_cols = [src, dst]
        if 'UEGID' in tcp_df.columns:
            conn_cols.append('UEGID')

        loss_details_list = []
        anomaly_details_list = []
        summary_list = []
        total_connections = 0
        connections_with_loss = 0
        total_loss_events = 0

        has_dlen = datalen_col and datalen_col in tcp_df.columns

        for conn_key, conn_group in tcp_df.groupby(conn_cols):
            total_connections += 1
            conn_group = conn_group.reset_index(drop=True)

            # ── step1：识别原始数据包（seq推进的包）──────────────────
            max_seq_end = -1
            data_pkts   = 0
            ack_pkts    = 0
            retrans_pkts = 0
            orig_list   = []   # [(seq_val, dlen, local_time), ...]

            for _, row in conn_group.iterrows():
                s = int(row[seq])
                d = int(row[datalen_col]) if has_dlen else 0
                seq_end = s + d

                if seq_end <= max_seq_end:
                    if d > 0:
                        retrans_pkts += 1
                    else:
                        ack_pkts += 1
                else:
                    data_pkts += 1
                    max_seq_end = seq_end
                    t = row['LocalTime'] if 'LocalTime' in row.index else None
                    orig_list.append((s, d, t))

            # ── step2：对原始数据包按seq升序，检测字节流断点 ─────────
            orig_list.sort(key=lambda x: x[0])

            conn_loss_events = 0
            conn_lost_bytes  = 0

            for i in range(1, len(orig_list)):
                prev_s, prev_d, _     = orig_list[i - 1]
                next_s, next_d, next_t = orig_list[i]

                expected = prev_s + prev_d
                gap = next_s - expected

                if gap <= 0:
                    continue  # 重叠/覆盖，不是丢包

                if gap > anomaly_gap:
                    anom = {
                        src: int(conn_group[src].iloc[0]),
                        dst: int(conn_group[dst].iloc[0]),
                        'prev_seq': prev_s,
                        'curr_seq': next_s,
                        'seq_gap':  gap,
                        'note':     'seq anomaly (wrap/reconnect)',
                    }
                    if 'UEGID' in conn_group.columns:
                        anom['UEGID'] = int(conn_group['UEGID'].iloc[0])
                    if next_t is not None:
                        anom['local_time'] = next_t
                    anomaly_details_list.append(anom)
                else:
                    conn_loss_events += 1
                    conn_lost_bytes  += gap
                    total_loss_events += 1
                    detail = {
                        src: int(conn_group[src].iloc[0]),
                        dst: int(conn_group[dst].iloc[0]),
                        'prev_seq':     prev_s,
                        'curr_seq':     next_s,
                        'seq_gap':      gap,
                        'missing_bytes': gap,
                    }
                    if 'UEGID' in conn_group.columns:
                        detail['UEGID'] = int(conn_group['UEGID'].iloc[0])
                    if next_t is not None:
                        detail['local_time'] = next_t
                    loss_details_list.append(detail)

            if conn_loss_events > 0:
                connections_with_loss += 1

            summary = {
                src: conn_key[0],
                dst: conn_key[1],
                'total_pkts':      len(conn_group),
                'data_pkts':       data_pkts,
                'ack_pkts':        ack_pkts,
                'retrans_pkts':    retrans_pkts,
                'loss_events':     conn_loss_events,
                'total_lost_bytes': conn_lost_bytes,
            }
            if 'UEGID' in tcp_df.columns:
                summary['UEGID'] = conn_key[2] if len(conn_key) > 2 else None
            summary_list.append(summary)

        result = {
            'total_connections':   total_connections,
            'connections_with_loss': connections_with_loss,
            'loss_events':         total_loss_events,
        }

        col_mapping = {src: 'srcport', dst: 'dstport'}

        if per_connection:
            if loss_details_list:
                ld_df = pd.DataFrame(loss_details_list)
                ld_df.rename(columns=col_mapping, inplace=True)
                result['loss_details'] = ld_df
            else:
                result['loss_details'] = pd.DataFrame()

            if anomaly_details_list:
                ad_df = pd.DataFrame(anomaly_details_list)
                ad_df.rename(columns=col_mapping, inplace=True)
                result['anomaly_details'] = ad_df
            else:
                result['anomaly_details'] = pd.DataFrame()

            if summary_list:
                sm_df = pd.DataFrame(summary_list)
                sm_df.rename(columns=col_mapping, inplace=True)
                result['summary'] = sm_df.sort_values('total_lost_bytes', ascending=False).reset_index(drop=True)
            else:
                result['summary'] = pd.DataFrame()

        return result

    def describe(self):
        """
        TCP 分析摘要。

        Returns:
            dict: {'direction', 'total_tcp_pkts', 'retransmit_count', 'retransmit_rate',
                   'top_ports', 'flag_stats', 'packet_loss_summary', 'retrans_summary'}
        """
        loss_result = self.detect_tcp_packet_loss(per_connection=True)
        retrans_result = self.detect_tcp_retransmission(per_connection=True)

        return {
            'direction':       self._dir_str,
            'total_tcp_pkts':  self.tcp_count(),
            'retransmit_count': self.retransmit_count(),
            'retransmit_rate':  self.retransmit_rate(),
            'top_ports':        self.port_stats(top_n=5).to_dict('records'),
            'flag_stats':       self.flag_stats(),
            'packet_loss_summary': {
                'total_connections': loss_result['total_connections'],
                'connections_with_loss': loss_result['connections_with_loss'],
                'total_loss_events': loss_result['loss_events'],
            },
            'retrans_summary': {
                'total_connections': retrans_result['total_connections'],
                'connections_with_retrans': retrans_result['connections_with_retrans'],
                'total_retrans_pkts': retrans_result['total_retrans_pkts'],
                'retrans_rate': retrans_result['retrans_rate'],
            }
        }


# ============================================================
# PdcpIcmp — ICMP ping RTT 分析（跨方向配对）
# ============================================================

# ICMP Type 值
_ICMP_ECHO_REQUEST = 8   # 终端 → 服务器（UL 方向）
_ICMP_ECHO_REPLY   = 0   # 服务器 → 终端（DL 方向）


def _airtime_to_datetime(df, datestr=None):
    """
    将 LocalTime 字段（格式 HH:MM:SS:mmm）解析为 datetime，
    返回带 '_t' 列的 DataFrame 副本，解析失败行的 '_t' 为 NaT。
    """
    df = df.copy()
    try:
        df['_t'] = pd.to_datetime(
            df['LocalTime'].str.strip(),
            format='%H:%M:%S:%f',
            errors='coerce',
        )
    except Exception:
        df['_t'] = pd.NaT
    return df


class PdcpIcmp:
    """
    ICMP ping RTT 分析类。

    原理：
      - UL 日志中 icmpType=8（Echo Request）由终端发出；
      - DL 日志中 icmpType=0（Echo Reply）是服务器回包；
      - 同一 icmpSeq（序列号）配对后，Reply 时间 - Request 时间 = 空口 RTT（严格说
        是 DU 侧可见的 RTT，包含上行空口 + 服务器处理 + 下行空口）。

    注意：
      - 时间戳来自 LocalTime（精度 ms），RTT 单位为毫秒（ms）。
      - 若同一 icmpSeq 出现多次（重传），取第一次 Request / 最后一次 Reply。
      - 超时未回包（无对应 Reply）的 Request 会在 timeout_ms 参数控制下标记为丢包。

    Parameters:
        directory    : 日志目录
        id_filter    : dict，如 {'UEGID': [23]}
        time_interval: [start_str, end_str]
        timeout_ms   : 判定 ping 超时的阈值（ms），默认 2000ms
    """

    _UL_SEQ      = 'UL_DATA_PDU.u32icmpSeq'
    _UL_TYPE     = 'UL_DATA_PDU.u32icmpType'
    _DL_SEQ      = 'DL_DATA_SDU.u32icmpSeq'
    _DL_TYPE     = 'DL_DATA_SDU.u32icmpType'

    def __init__(self, directory, id_filter=None, time_interval=None, timeout_ms=2000):
        self._dir           = directory
        self._id_filter     = id_filter or {}
        self._time_interval = time_interval
        self._timeout_ms    = timeout_ms
        self._df_ul         = None
        self._df_dl         = None
        self._df_rtt        = None   # 配对结果缓存

    # ----------------------------------------------------------
    # 内部：加载数据
    # ----------------------------------------------------------

    def _ul(self):
        if self._df_ul is None:
            self._df_ul = _load_csvs(
                self._dir, NR_FILE_PDCP_UL,
                id_filter=self._id_filter,
                time_interval=self._time_interval,
            )
        return self._df_ul

    def _dl(self):
        if self._df_dl is None:
            self._df_dl = _load_csvs(
                self._dir, NR_FILE_PDCP_DL,
                id_filter=self._id_filter,
                time_interval=self._time_interval,
            )
        return self._df_dl

    def _extract_icmp(self, df, seq_col, type_col, icmp_type_val):
        """
        从 df 中提取指定 ICMP 类型的报文，返回含 [icmpSeq, _t] 的 DataFrame。
        """
        if df.empty or seq_col not in df.columns or 'LocalTime' not in df.columns:
            return pd.DataFrame(columns=['icmpSeq', '_t'])

        # 过滤指定 type
        if type_col in df.columns:
            sub = df[df[type_col].fillna(-1) == icmp_type_val].copy()
        else:
            # 没有 type 字段时，退化为 seq != 0
            sub = df[df[seq_col].fillna(0) != 0].copy()

        if sub.empty:
            return pd.DataFrame(columns=['icmpSeq', '_t'])

        sub = _airtime_to_datetime(sub)
        sub = sub[sub[seq_col].fillna(0) != 0][['_t', seq_col]].dropna()
        sub.columns = ['_t', 'icmpSeq']
        sub['icmpSeq'] = sub['icmpSeq'].astype(int)
        return sub.reset_index(drop=True)

    def _pair(self):
        """
        配对 UL Echo Request 与 DL Echo Reply，计算 RTT（ms）。

        Returns:
            DataFrame: columns=['icmpSeq', 'req_t', 'rep_t', 'rtt_ms', 'timeout']
        """
        if self._df_rtt is not None:
            return self._df_rtt

        ul_df = self._ul()
        dl_df = self._dl()

        req = self._extract_icmp(ul_df, self._UL_SEQ, self._UL_TYPE, _ICMP_ECHO_REQUEST)
        rep = self._extract_icmp(dl_df, self._DL_SEQ, self._DL_TYPE, _ICMP_ECHO_REPLY)

        if req.empty:
            self._df_rtt = pd.DataFrame(
                columns=['icmpSeq', 'req_t', 'rep_t', 'rtt_ms', 'timeout'])
            return self._df_rtt

        # 同一 icmpSeq 出现多次时：Request 取最早，Reply 取最晚
        req_agg = req.groupby('icmpSeq')['_t'].min().reset_index()
        req_agg.columns = ['icmpSeq', 'req_t']

        if rep.empty:
            req_agg['rep_t']    = pd.NaT
            req_agg['rtt_ms']   = np.nan
            req_agg['timeout']  = True
            self._df_rtt = req_agg
            return self._df_rtt

        rep_agg = rep.groupby('icmpSeq')['_t'].max().reset_index()
        rep_agg.columns = ['icmpSeq', 'rep_t']

        merged = pd.merge(req_agg, rep_agg, on='icmpSeq', how='left')
        merged['rtt_ms'] = (merged['rep_t'] - merged['req_t']).dt.total_seconds() * 1000
        # 负 RTT（时钟抖动）或超时阈值
        merged['timeout'] = (
            merged['rep_t'].isna() |
            (merged['rtt_ms'] < 0) |
            (merged['rtt_ms'] > self._timeout_ms)
        )
        # 负/超时的 rtt_ms 置 NaN
        merged.loc[merged['timeout'], 'rtt_ms'] = np.nan

        self._df_rtt = merged.sort_values('req_t').reset_index(drop=True)
        return self._df_rtt

    # ----------------------------------------------------------
    # 公开 API
    # ----------------------------------------------------------

    def has_icmp(self):
        """是否存在 ICMP Echo Request。"""
        return not self._pair().empty

    def rtt_stats(self):
        """
        RTT 统计（单位 ms，排除超时）。

        Returns:
            dict: {'count', 'loss_count', 'loss_rate',
                   'mean_ms', 'p50_ms', 'p95_ms', 'p99_ms', 'max_ms'}
        """
        df = self._pair()
        if df.empty:
            return {}
        total   = len(df)
        loss    = int(df['timeout'].sum())
        valid   = df['rtt_ms'].dropna()
        result  = {
            'count':      total,
            'loss_count': loss,
            'loss_rate':  loss / total if total > 0 else 0.0,
        }
        if valid.empty:
            result.update({'mean_ms': None, 'p50_ms': None,
                           'p95_ms': None, 'p99_ms': None, 'max_ms': None})
        else:
            result.update({
                'mean_ms': float(valid.mean()),
                'p50_ms':  float(valid.quantile(0.50)),
                'p95_ms':  float(valid.quantile(0.95)),
                'p99_ms':  float(valid.quantile(0.99)),
                'max_ms':  float(valid.max()),
            })
        return result

    def rtt_timeseries(self):
        """
        RTT 时序（每个成功配对的 ping，按 req_t 排列）。

        Returns:
            DataFrame: index=icmpSeq, columns=['req_t', 'rep_t', 'rtt_ms', 'timeout']
        """
        df = self._pair()
        if df.empty:
            return pd.DataFrame()
        return df.set_index('icmpSeq')[['req_t', 'rep_t', 'rtt_ms', 'timeout']]

    def rtt_hist(self, bins=20):
        """
        RTT 分布直方图数据（单位 ms，排除超时）。

        Returns:
            (counts, bin_edges) — np.histogram 格式，无有效数据时返回 (None, None)
        """
        df = self._pair()
        s = df['rtt_ms'].dropna()
        if s.empty:
            return None, None
        return np.histogram(s, bins=bins)

    def loss_pings(self):
        """
        返回未收到 Reply 的 ping（超时/丢失）。

        Returns:
            DataFrame: columns=['icmpSeq', 'req_t']
        """
        df = self._pair()
        if df.empty:
            return pd.DataFrame(columns=['icmpSeq', 'req_t'])
        return df[df['timeout']][['icmpSeq', 'req_t']].reset_index(drop=True)

    def describe(self):
        """
        ICMP ping 分析摘要。

        Returns:
            dict: {'total_pings', 'loss_count', 'loss_rate',
                   'rtt_mean_ms', 'rtt_p50_ms', 'rtt_p95_ms', 'rtt_max_ms'}
        """
        stats = self.rtt_stats()
        if not stats:
            return {'total_pings': 0}
        return {
            'total_pings':  stats.get('count', 0),
            'loss_count':   stats.get('loss_count', 0),
            'loss_rate':    stats.get('loss_rate', 0.0),
            'rtt_mean_ms':  stats.get('mean_ms'),
            'rtt_p50_ms':   stats.get('p50_ms'),
            'rtt_p95_ms':   stats.get('p95_ms'),
            'rtt_max_ms':   stats.get('max_ms'),
        }
