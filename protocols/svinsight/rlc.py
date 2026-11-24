# coding=utf-8
"""
rlc.py — RLC 分析模块

支持分析 RLC 上行/下行日志（AM 模式），包括：
- 下行时延（PDCP → RLC 处理时延，空口时延）
- AM 窗口状态（Vta/Vts → 窗口深度/积压）
- 状态报文（STATUS PDU）分析：ACK/NACK 统计
- 重传/RLF 分析
- 流量统计

注意（AM 模式方向规定）：
    - RLC AM 下行数据 PDU → 在 RLC 下行日志（RLC_dlUeTtiInfo）中
    - RLC AM 上行状态 PDU（ACK/NACK）→ 也在 RLC 下行日志中（DL_STATUS_PDU）
    - RLC AM 上行数据 PDU → 在 RLC 上行日志（RLC_ulUeTtiInfo）中
    - RLC AM 下行状态 PDU（ACK/NACK 反馈给基站下行）→ 也在 RLC 上行日志中（UL_STATUS_PDU）
"""

import pandas as pd
import numpy as np
import os
import datetime

# 特殊 UEGID（广播/公共信道，不属于普通 UE）
SPECIAL_UEGID_MIN = 7201

# RLC 日志文件前缀
NR_FILE_RLC_UL = 'RLC_ulUeTtiInfo'
NR_FILE_RLC_DL = 'RLC_dlUeTtiInfo'

# RLC 模式
RLC_MODE_TM = 0
RLC_MODE_UM = 1   # 注意：实测数据 u8RlcMode=1 为 AM，与标准编号不同（以实际日志为准）
RLC_MODE_AM = 1   # 实际日志中 AM 模式对应值为 1

# PdcpCost 异常阈值（us）：超过此值视为无效/未赋值的计数器溢出
PDCP_COST_MAX_VALID_US = 10_000_000  # 10 秒，超过则滤除

# RLC AM SN 范围（18-bit：SN 域大小 = 262144）
RLC_AM_SN_MAX = 262144  # 2^18，用于窗口深度回绕修正


def _sn_depth(a, b, sn_max=RLC_AM_SN_MAX):
    """
    计算 RLC AM 窗口深度 = b - a（含 SN 回绕修正）。
    若 b - a < 0，则认为发生了 SN 回绕，修正为 b - a + sn_max。
    """
    d = b - a
    return d.where(d >= 0, d + sn_max)


# ============================================================
# 工具函数
# ============================================================

def _load_csvs(directory, prefix, id_filter=None, time_interval=None):
    """加载目录下所有匹配 prefix 前缀的 CSV 文件，合并为 DataFrame。"""
    files = sorted([f for f in os.listdir(directory) if f.endswith('.csv') and prefix in f])
    if not files:
        return pd.DataFrame()

    parts = []
    for fname in files:
        fpath = os.path.join(directory, fname)
        # 尝试多种编码，优先 UTF-8，然后 GBK/GB2312
        df = None
        for encoding in ['utf-8', 'gbk', 'gb2312', 'latin1']:
            try:
                df = pd.read_csv(fpath, na_values='-', encoding=encoding)
                break
            except (UnicodeDecodeError, UnicodeError):
                continue
        if df is None:
            print(f"警告: 无法解码文件 {fname}，跳过")
            continue
        
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

            def _parse(t):
                try:
                    h, mi, s, ms = t.replace(' ', '').split(':')
                    return datetime.datetime(int(y), int(m), int(d),
                                            int(h), int(mi), int(s), int(ms) * 1000)
                except Exception:
                    return pd.NaT

            df['_lt'] = df['LocalTime'].apply(_parse)
            t0 = pd.to_datetime(time_interval[0])
            t1 = pd.to_datetime(time_interval[1])
            df = df[(df['_lt'] >= t0) & (df['_lt'] <= t1)].drop(columns=['_lt'])
        parts.append(df)

    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True)


def _add_localtime_index(df, time_col='LocalTime'):
    """将 LocalTime 字符串（HH:MM:SS:fff）转为 datetime 并设置为索引，返回新 df。"""
    df = df.copy()
    try:
        df['_t'] = pd.to_datetime(df[time_col].str.strip(), format='%H:%M:%S:%f', errors='coerce')
    except Exception:
        df['_t'] = pd.NaT
    df = df.dropna(subset=['_t']).set_index('_t')
    if time_col in df.columns:
        df = df.drop(columns=[time_col])
    return df


# ============================================================
# RlcDl — RLC 下行分析
# ============================================================

class RlcDl:
    """
    RLC 下行日志分析类（RLC_dlUeTtiInfo）

    包含：
      - DL_DATA_PDU：下行数据 PDU（u32PdcpCost/u32RlcCost = 时延，us）
      - DL_STATUS_PDU：下行方向的上行状态报文（UE 发给基站的 ACK/NACK）
      - DL_ASSEM_INFO：下行组包信息（TbSize 等）
      - RLF_INFO：RLF（无线链路失败）事件

    AM 窗口字段（下行数据 PDU 视角）：
        u32Vta — 发送端已确认发送窗口下沿（tx_ack = Vta = 下行 AM 已 ACK 的 SN）
        u32Vts — 发送端发送窗口前沿（tx_send = Vts）
        窗口深度 = Vts - Vta（即等待 ACK 的包数）

    状态报文（DL_STATUS_PDU，即 UE 上行方向发出的 ACK/NACK）：
        u32Acksn       — ACK SN（截至此 SN 之前的数据已全部收到）
        u32FirstNackSn — 第一个 NACK 的 SN（=0 表示无 NACK，全部 ACK）
        u32Vrr         — 接收端期望接收的下一个 SN（Vrr = next expected）
        u32Vrh         — 接收端收到的最高 SN + 1（Vrh = highest received + 1）
    """

    DELAY_COLS = ['DL_DATA_PDU.u32PdcpCost', 'DL_DATA_PDU.u32RlcCost']
    WINDOW_COLS = ['DL_DATA_PDU.u32Vta', 'DL_DATA_PDU.u32Vts']
    STATUS_COLS = [
        'DL_STATUS_PDU.u32Acksn', 'DL_STATUS_PDU.u32FirstNackSn',
        'DL_STATUS_PDU.u32Vrr', 'DL_STATUS_PDU.u32Vrh',
        'DL_STATUS_PDU.u8LchId',
    ]
    RLF_COLS = [
        'RLF_INFO.u8lchId', 'RLF_INFO.u8rbId',
        'RLF_INFO.u8retranTimes', 'RLF_INFO.u8transmaxThresHold',
        'RLF_INFO.u32RlcSn',
    ]
    ASSEM_COLS = ['DL_ASSEM_INFO.u32TbSize', 'DL_ASSEM_INFO.b8IsNewTx']
    # RLC 实例粒度：UEGID + LchId + 方向（LCH 与 RB 1:1 映射）
    LCHID_COL = 'DL_DATA_PDU.u8LchId'

    def __init__(self, directory, id_filter=None, time_interval=None):
        self._dir = directory
        self._id_filter = id_filter or {}
        self._time_interval = time_interval
        self._df = None

    def _data(self):
        if self._df is None:
            self._df = _load_csvs(
                self._dir, NR_FILE_RLC_DL,
                id_filter=self._id_filter,
                time_interval=self._time_interval,
            )
        return self._df

    def _data_pdus(self):
        """仅返回有数据 PDU 的行（DL_DATA_PDU 字段非空）。"""
        df = self._data()
        key = 'DL_DATA_PDU.u32Vta'
        if key not in df.columns:
            return pd.DataFrame()
        return df[df[key].notna()].copy()

    def _status_pdus(self):
        """仅返回有状态 PDU 的行（DL_STATUS_PDU.u32Acksn 非空）。"""
        df = self._data()
        key = 'DL_STATUS_PDU.u32Acksn'
        if key not in df.columns:
            return pd.DataFrame()
        return df[df[key].notna()].copy()

    def _group_keys(self, df, lch_col=None):
        """
        返回分组键列表。RLC 实例粒度为 (UEGID, LchId)。
        若 LchId 列不存在，则仅按 UEGID 分组。
        """
        lch = lch_col or self.LCHID_COL
        has_uegid = 'UEGID' in df.columns
        has_lch = lch in df.columns
        if has_uegid and has_lch:
            return ['UEGID', lch]
        elif has_uegid:
            return ['UEGID']
        else:
            return None

    # ---- 时延分析 ----

    def dl_delay_stats(self, group_by_lch=False):
        """
        下行时延统计（单位 us）
        PdcpCost = PDCP 到 RLC 的处理时延；RlcCost = RLC 发送时延（空口）

        注意：PdcpCost 原始字段中存在极大值（可能是计数器未清零导致），
        本函数会自动过滤 > 10s 的异常值。

        Args:
            group_by_lch: 是否按 (UEGID, LchId) 分组统计，默认 False（全局统计）

        Returns:
            DataFrame: index=['mean','p50','p95','p99','max'], columns=[PdcpCost, RlcCost]
            若 group_by_lch=True，则返回 dict: { (UEGID, LchId): DataFrame }
        """
        df = self._data_pdus()
        if df.empty:
            return pd.DataFrame() if not group_by_lch else {}
        avail = [c for c in self.DELAY_COLS if c in df.columns]
        if not avail:
            return pd.DataFrame() if not group_by_lch else {}

        # 过滤 PdcpCost 异常值
        sub = df[avail + (['UEGID', self.LCHID_COL] if group_by_lch else [])].copy()
        if 'DL_DATA_PDU.u32PdcpCost' in sub.columns:
            sub['DL_DATA_PDU.u32PdcpCost'] = sub['DL_DATA_PDU.u32PdcpCost'].where(
                sub['DL_DATA_PDU.u32PdcpCost'] <= PDCP_COST_MAX_VALID_US)

        if not group_by_lch:
            sub = sub[avail].dropna(how='all')
            if sub.empty:
                return pd.DataFrame()
            result = sub.agg(['mean', 'median',
                              lambda x: x.quantile(0.95),
                              lambda x: x.quantile(0.99), 'max'])
            result.index = ['mean', 'p50', 'p95', 'p99', 'max']
            short = {c: c.split('.')[-1].replace('u32', '') for c in avail}
            result.rename(columns=short, inplace=True)
            return result

        # 按 (UEGID, LchId) 分组统计
        gkeys = self._group_keys(df)
        if not gkeys:
            return self.dl_delay_stats(group_by_lch=False)

        sub = sub.dropna(subset=avail, how='all')
        if sub.empty:
            return {}

        results = {}
        for key, grp in sub.groupby(gkeys):
            grp_clean = grp[avail].dropna(how='all')
            if grp_clean.empty:
                continue
            result = grp_clean.agg(['mean', 'median',
                                    lambda x: x.quantile(0.95),
                                    lambda x: x.quantile(0.99), 'max'])
            result.index = ['mean', 'p50', 'p95', 'p99', 'max']
            short = {c: c.split('.')[-1].replace('u32', '') for c in avail}
            result.rename(columns=short, inplace=True)
            results[key] = result
        return results

    def dl_delay_timeseries(self, time_bin=1):
        """
        下行时延时序（秒级均值，us）

        Returns:
            DataFrame: index=time, columns=[PdcpCost, RlcCost]
        """
        df = self._data_pdus()
        if df.empty or 'LocalTime' not in df.columns:
            return pd.DataFrame()
        cols = ['LocalTime'] + [c for c in self.DELAY_COLS if c in df.columns]
        sub = df[cols].copy()
        if 'DL_DATA_PDU.u32PdcpCost' in sub.columns:
            sub['DL_DATA_PDU.u32PdcpCost'] = sub['DL_DATA_PDU.u32PdcpCost'].where(
                sub['DL_DATA_PDU.u32PdcpCost'] <= PDCP_COST_MAX_VALID_US)
        sub = _add_localtime_index(sub)
        rlt = sub[[c for c in sub.columns if 'Cost' in c]].resample(f'{time_bin}s').mean()
        short = {c: c.split('.')[-1].replace('u32', '') for c in rlt.columns}
        rlt.rename(columns=short, inplace=True)
        return rlt

    # ---- AM 窗口分析 ----

    def window_depth_timeseries(self, time_bin=1):
        """
        下行 RLC AM 窗口深度时序（Vts - Vta）

        Returns:
            DataFrame: index=time, columns=[Vta, Vts, window_depth]
        """
        df = self._data_pdus()
        if df.empty or 'LocalTime' not in df.columns:
            return pd.DataFrame()
        avail = [c for c in self.WINDOW_COLS if c in df.columns]
        if len(avail) < 2:
            return pd.DataFrame()
        sub = df[['LocalTime'] + avail].dropna(subset=avail).copy()
        sub = _add_localtime_index(sub)
        sub['window_depth'] = _sn_depth(sub[avail[0]], sub[avail[1]])  # Vts - Vta（含回绕修正）
        rlt = sub.resample(f'{time_bin}s').mean()
        short = {c: c.split('.')[-1].replace('u32', '') for c in avail}
        rlt.rename(columns=short, inplace=True)
        return rlt

    def window_stats(self, group_by_lch=False):
        """
        下行 RLC AM 窗口统计（窗口深度分布）

        Args:
            group_by_lch: 是否按 (UEGID, LchId) 分组统计，默认 False

        Returns:
            dict: {'mean_depth', 'p95_depth', 'max_depth'} 或
            { (UEGID, LchId): {'mean_depth', 'p95_depth', 'max_depth'} }
        """
        df = self._data_pdus()
        avail = [c for c in self.WINDOW_COLS if c in df.columns]
        if len(avail) < 2:
            return {} if not group_by_lch else {}

        if not group_by_lch:
            sub = df[avail].dropna()
            if sub.empty:
                return {}
            depth = _sn_depth(sub.iloc[:, 0], sub.iloc[:, 1])
            return {
                'mean_depth': float(depth.mean()),
                'p95_depth': float(depth.quantile(0.95)),
                'max_depth': float(depth.max()),
            }

        # 按 (UEGID, LchId) 分组
        gkeys = self._group_keys(df)
        if not gkeys:
            return self.window_stats(group_by_lch=False)

        cols = gkeys + avail
        sub = df[[c for c in cols if c in df.columns]].dropna(subset=avail)
        if sub.empty:
            return {}

        results = {}
        for key, grp in sub.groupby(gkeys):
            depth = _sn_depth(grp[avail[0]], grp[avail[1]])
            results[key] = {
                'mean_depth': float(depth.mean()),
                'p95_depth': float(depth.quantile(0.95)),
                'max_depth': float(depth.max()),
            }
        return results

    # ---- 状态报文分析（下行日志中的上行 STATUS PDU） ----

    def status_pdu_stats(self, group_by_lch=False):
        """
        状态报文统计（DL 日志中的 UE 上行 ACK/NACK，即 DL_STATUS_PDU）

        Args:
            group_by_lch: 是否按 (UEGID, LchId) 分组统计，默认 False

        Returns:
            dict 或 { (UEGID, LchId): dict }
        """
        df = self._status_pdus()
        if df.empty:
            empty = {'total_status': 0, 'nack_count': 0, 'nack_ratio': 0.0}
            return empty if not group_by_lch else {}

        nack_col = 'DL_STATUS_PDU.u32FirstNackSn'
        vrr_col, vrh_col = 'DL_STATUS_PDU.u32Vrr', 'DL_STATUS_PDU.u32Vrh'

        if not group_by_lch:
            total = len(df)
            nack_count = int((df[nack_col].fillna(0) > 0).sum()) if nack_col in df.columns else 0
            result = {
                'total_status': total,
                'nack_count': nack_count,
                'nack_ratio': nack_count / total if total > 0 else 0.0,
            }
            if vrr_col in df.columns and vrh_col in df.columns:
                gap = (df[vrh_col] - df[vrr_col]).dropna()
                if len(gap):
                    result['rx_window_gap_mean'] = float(gap.mean())
                    result['rx_window_gap_max'] = float(gap.max())
            return result

        # 按 (UEGID, LchId) 分组 - 使用 DL_STATUS_PDU.u8LchId
        lch_col = 'DL_STATUS_PDU.u8LchId'
        has_uegid = 'UEGID' in df.columns
        has_lch = lch_col in df.columns
        if has_uegid and has_lch:
            gkeys = ['UEGID', lch_col]
        elif has_uegid:
            gkeys = ['UEGID']
        else:
            return {'_all': self.status_pdu_stats(group_by_lch=False)}

        results = {}
        for key, grp in df.groupby(gkeys):
            total = len(grp)
            nack_count = int((grp[nack_col].fillna(0) > 0).sum()) if nack_col in grp.columns else 0
            res = {
                'total_status': total,
                'nack_count': nack_count,
                'nack_ratio': nack_count / total if total > 0 else 0.0,
            }
            if vrr_col in grp.columns and vrh_col in grp.columns:
                gap = (grp[vrh_col] - grp[vrr_col]).dropna()
                if len(gap):
                    res['rx_window_gap_mean'] = float(gap.mean())
                    res['rx_window_gap_max'] = float(gap.max())
            results[key] = res
        return results

    def status_pdu_timeseries(self, time_bin=1):
        """
        状态报文时序（每秒 NACK 次数）

        Returns:
            DataFrame: index=time, columns=[status_count, nack_count]
        """
        df = self._status_pdus()
        if df.empty or 'LocalTime' not in df.columns:
            return pd.DataFrame()
        sub = df[['LocalTime']].copy()
        nack_col = 'DL_STATUS_PDU.u32FirstNackSn'
        if nack_col in df.columns:
            sub['nack'] = (df[nack_col].fillna(0) > 0).astype(int)
        else:
            sub['nack'] = 0
        sub['status'] = 1
        sub = _add_localtime_index(sub)
        rlt = sub.resample(f'{time_bin}s').sum()
        rlt.rename(columns={'status': 'status_count', 'nack': 'nack_count'}, inplace=True)
        return rlt

    # ---- RLF 分析 ----

    def rlf_events(self, group_by_lch=False):
        """
        RLF（Radio Link Failure）事件列表

        Args:
            group_by_lch: 是否按 (UEGID, LchId) 分组返回，默认 False

        Returns:
            DataFrame 或 dict: { (UEGID, LchId): DataFrame }
        """
        df = self._data()
        key = 'RLF_INFO.u8retranTimes'
        if key not in df.columns:
            return pd.DataFrame() if not group_by_lch else {}
        rlf = df[df[key].notna()]
        if rlf.empty:
            return pd.DataFrame() if not group_by_lch else {}

        avail = ['LocalTime', 'UEGID'] + [c for c in self.RLF_COLS if c in df.columns]
        result = rlf[[c for c in avail if c in rlf.columns]].copy()

        if not group_by_lch:
            return result

        # 按 (UEGID, LchId) 分组 - RLF_INFO 中有 u8lchId
        lch_col = 'RLF_INFO.u8lchId'
        has_uegid = 'UEGID' in result.columns
        has_lch = lch_col in result.columns
        if has_uegid and has_lch:
            gkeys = ['UEGID', lch_col]
        elif has_uegid:
            gkeys = ['UEGID']
        else:
            return {'_all': result}

        return {key: grp for key, grp in result.groupby(gkeys)}

    # ---- 组包/TbSize 分析 ----

    def tb_throughput(self, time_bin=1):
        """
        下行 TB 吞吐量时序（TbSize 之和，bytes/s 近似）

        Returns:
            Series: index=time, values=TbSize sum
        """
        df = self._data()
        col = 'DL_ASSEM_INFO.u32TbSize'
        if col not in df.columns or 'LocalTime' not in df.columns:
            return pd.Series(dtype=float, name='tb_throughput')
        sub = df[['LocalTime', col]].dropna().copy()
        sub = _add_localtime_index(sub)
        return sub[col].resample(f'{time_bin}s').sum().rename('tb_throughput')

    def describe(self):
        df = self._data()
        if df.empty:
            return "No RLC DL data found."
        uegids = sorted(df['UEGID'].dropna().unique().astype(int)) if 'UEGID' in df.columns else []
        lt = df['LocalTime'].dropna() if 'LocalTime' in df.columns else pd.Series()
        t_range = f"{lt.iloc[0]} ~ {lt.iloc[-1]}" if len(lt) >= 2 else 'N/A'
        data_rows = df['DL_DATA_PDU.u32Vta'].dropna().shape[0] if 'DL_DATA_PDU.u32Vta' in df.columns else 0
        status_rows = df['DL_STATUS_PDU.u32Acksn'].dropna().shape[0] if 'DL_STATUS_PDU.u32Acksn' in df.columns else 0
        return {
            'total_rows': len(df),
            'data_pdu_rows': data_rows,
            'status_pdu_rows': status_rows,
            'uegids': uegids,
            'time_range': t_range,
            'rlc_mode': 'AM (mode=1)',
        }


# ============================================================
# RlcUl — RLC 上行分析
# ============================================================

class RlcUl:
    """
    RLC 上行日志分析类（RLC_ulUeTtiInfo）

    包含：
      - UL_DATA_PDU：上行数据 PDU
          u32Vrr — 接收端期望下一个 SN（= received vars）
          u32Vrh — 接收端收到最高 SN + 1
          u32AirCost — 空口时延（us）
          u32DataLen — 数据长度（bytes）
      - UL_STATUS_PDU：上行日志中的下行状态报文（基站发给 UE 的 ACK/NACK，AM 模式）
          u32Acksn       — ACK SN
          u32FirstNackSn — 第一个 NACK SN（0=全 ACK）
          u32Vta         — 发送端 Vta（基站下行发送端已 ACK 的窗口下沿）
          u32Vts         — 发送端 Vts（基站下行发送端发送窗口前沿）
      - F1U_STATUS：F1U 状态
      - UL_TB_TIME_COST：TB 级时延统计
    """

    WINDOW_COLS = ['UL_DATA_PDU.u32Vrr', 'UL_DATA_PDU.u32Vrh']
    STATUS_COLS = [
        'UL_STATUS_PDU.u32Acksn', 'UL_STATUS_PDU.u32FirstNackSn',
        'UL_STATUS_PDU.u32Vta', 'UL_STATUS_PDU.u32Vts',
    ]
    AIR_COST_COL = 'UL_DATA_PDU.u32AirCost'
    F1U_COLS = ['F1U_STATUS.u32rcvbuff', 'F1U_STATUS.u32sendsducntinperiod']
    # RLC 实例粒度：UEGID + LchId + 方向
    LCHID_COL = 'UL_DATA_PDU.u8LchId'

    def __init__(self, directory, id_filter=None, time_interval=None):
        self._dir = directory
        self._id_filter = id_filter or {}
        self._time_interval = time_interval
        self._df = None

    def _data(self):
        if self._df is None:
            self._df = _load_csvs(
                self._dir, NR_FILE_RLC_UL,
                id_filter=self._id_filter,
                time_interval=self._time_interval,
            )
        return self._df

    def _data_pdus(self):
        df = self._data()
        key = 'UL_DATA_PDU.u32Vrr'
        if key not in df.columns:
            return pd.DataFrame()
        return df[df[key].notna()].copy()

    def _status_pdus(self):
        df = self._data()
        key = 'UL_STATUS_PDU.u32Acksn'
        if key not in df.columns:
            return pd.DataFrame()
        return df[df[key].notna()].copy()

    def _group_keys(self, df, lch_col=None):
        """
        返回分组键列表。RLC 实例粒度为 (UEGID, LchId)。
        若 LchId 列不存在，则仅按 UEGID 分组。
        """
        lch = lch_col or self.LCHID_COL
        has_uegid = 'UEGID' in df.columns
        has_lch = lch in df.columns
        if has_uegid and has_lch:
            return ['UEGID', lch]
        elif has_uegid:
            return ['UEGID']
        else:
            return None

    # ---- 空口时延 ----

    def air_cost_stats(self, group_by_lch=False):
        """
        上行空口时延统计（单位 us）

        Args:
            group_by_lch: 是否按 (UEGID, LchId) 分组统计，默认 False

        Returns:
            dict 或 { (UEGID, LchId): dict }
        """
        df = self._data_pdus()
        col = self.AIR_COST_COL
        if col not in df.columns:
            return {} if not group_by_lch else {}

        if not group_by_lch:
            s = df[col].dropna()
            if s.empty:
                return {}
            return {
                'mean': float(s.mean()),
                'p50': float(s.median()),
                'p95': float(s.quantile(0.95)),
                'p99': float(s.quantile(0.99)),
                'max': float(s.max()),
            }

        # 按 (UEGID, LchId) 分组
        gkeys = self._group_keys(df)
        if not gkeys:
            return self.air_cost_stats(group_by_lch=False)

        cols = gkeys + [col]
        sub = df[[c for c in cols if c in df.columns]].dropna(subset=[col])
        if sub.empty:
            return {}

        results = {}
        for key, grp in sub.groupby(gkeys):
            s = grp[col]
            results[key] = {
                'mean': float(s.mean()),
                'p50': float(s.median()),
                'p95': float(s.quantile(0.95)),
                'p99': float(s.quantile(0.99)),
                'max': float(s.max()),
            }
        return results

    def air_cost_timeseries(self, time_bin=1):
        """上行空口时延时序"""
        df = self._data_pdus()
        col = self.AIR_COST_COL
        if col not in df.columns or 'LocalTime' not in df.columns:
            return pd.Series(dtype=float, name='ul_air_cost_us')
        sub = df[['LocalTime', col]].dropna().copy()
        sub = _add_localtime_index(sub)
        return sub[col].resample(f'{time_bin}s').mean().rename('ul_air_cost_us')

    # ---- AM 窗口分析 ----

    def window_gap_timeseries(self, time_bin=1):
        """
        上行 RLC AM 接收窗口空洞时序（Vrh - Vrr，即接收窗口中已收到最高 SN 与期望 SN 的差）

        Returns:
            DataFrame: index=time, columns=[Vrr, Vrh, rx_gap]
        """
        df = self._data_pdus()
        avail = [c for c in self.WINDOW_COLS if c in df.columns]
        if len(avail) < 2 or 'LocalTime' not in df.columns:
            return pd.DataFrame()
        sub = df[['LocalTime'] + avail].dropna(subset=avail).copy()
        sub = _add_localtime_index(sub)
        sub['rx_gap'] = _sn_depth(sub[avail[0]], sub[avail[1]])  # Vrh - Vrr（含回绕修正）
        rlt = sub.resample(f'{time_bin}s').mean()
        short = {c: c.split('.')[-1].replace('u32', '') for c in avail}
        rlt.rename(columns=short, inplace=True)
        return rlt

    def window_stats(self, group_by_lch=False):
        """
        上行接收窗口统计

        Args:
            group_by_lch: 是否按 (UEGID, LchId) 分组统计，默认 False

        Returns:
            dict 或 { (UEGID, LchId): dict }
        """
        df = self._data_pdus()
        avail = [c for c in self.WINDOW_COLS if c in df.columns]
        if len(avail) < 2:
            return {} if not group_by_lch else {}

        if not group_by_lch:
            sub = df[avail].dropna()
            if sub.empty:
                return {}
            gap = _sn_depth(sub.iloc[:, 0], sub.iloc[:, 1])
            return {
                'mean_rx_gap': float(gap.mean()),
                'p95_rx_gap': float(gap.quantile(0.95)),
                'max_rx_gap': float(gap.max()),
            }

        # 按 (UEGID, LchId) 分组
        gkeys = self._group_keys(df)
        if not gkeys:
            return self.window_stats(group_by_lch=False)

        cols = gkeys + avail
        sub = df[[c for c in cols if c in df.columns]].dropna(subset=avail)
        if sub.empty:
            return {}

        results = {}
        for key, grp in sub.groupby(gkeys):
            gap = _sn_depth(grp[avail[0]], grp[avail[1]])
            results[key] = {
                'mean_rx_gap': float(gap.mean()),
                'p95_rx_gap': float(gap.quantile(0.95)),
                'max_rx_gap': float(gap.max()),
            }
        return results

    # ---- 状态报文分析（UL 日志中的下行 STATUS PDU） ----

    def status_pdu_stats(self, group_by_lch=False):
        """
        上行日志中的下行状态报文统计（基站发给 UE 的 ACK/NACK 状态包）

        Args:
            group_by_lch: 是否按 (UEGID, LchId) 分组统计，默认 False

        Returns:
            dict 或 { (UEGID, LchId): dict }
        """
        df = self._status_pdus()
        if df.empty:
            empty = {'total_status': 0, 'nack_count': 0, 'nack_ratio': 0.0}
            return empty if not group_by_lch else {}

        nack_col = 'UL_STATUS_PDU.u32FirstNackSn'
        vta_col, vts_col = 'UL_STATUS_PDU.u32Vta', 'UL_STATUS_PDU.u32Vts'

        if not group_by_lch:
            total = len(df)
            nack_count = int((df[nack_col].fillna(0) > 0).sum()) if nack_col in df.columns else 0
            result = {
                'total_status': total,
                'nack_count': nack_count,
                'nack_ratio': nack_count / total if total > 0 else 0.0,
            }
            if vta_col in df.columns and vts_col in df.columns:
                depth = _sn_depth(df[vta_col], df[vts_col]).dropna()
                if len(depth):
                    result['dl_tx_window_depth_mean'] = float(depth.mean())
                    result['dl_tx_window_depth_max'] = float(depth.max())
            return result

        # 按 (UEGID, LchId) 分组 - UL_STATUS_PDU 中无 u8LchId，仅按 UEGID
        has_uegid = 'UEGID' in df.columns
        if not has_uegid:
            return {'_all': self.status_pdu_stats(group_by_lch=False)}

        results = {}
        for key, grp in df.groupby(['UEGID']):
            total = len(grp)
            nack_count = int((grp[nack_col].fillna(0) > 0).sum()) if nack_col in grp.columns else 0
            res = {
                'total_status': total,
                'nack_count': nack_count,
                'nack_ratio': nack_count / total if total > 0 else 0.0,
            }
            if vta_col in grp.columns and vts_col in grp.columns:
                depth = _sn_depth(grp[vta_col], grp[vts_col]).dropna()
                if len(depth):
                    res['dl_tx_window_depth_mean'] = float(depth.mean())
                    res['dl_tx_window_depth_max'] = float(depth.max())
            results[(key, None)] = res  # UL_STATUS_PDU 无 LchId
        return results

    def status_pdu_timeseries(self, time_bin=1):
        """上行日志中状态报文时序（每秒 NACK 次数）"""
        df = self._status_pdus()
        if df.empty or 'LocalTime' not in df.columns:
            return pd.DataFrame()
        sub = df[['LocalTime']].copy()
        nack_col = 'UL_STATUS_PDU.u32FirstNackSn'
        sub['nack'] = (df[nack_col].fillna(0) > 0).astype(int) if nack_col in df.columns else 0
        sub['status'] = 1
        sub = _add_localtime_index(sub)
        rlt = sub.resample(f'{time_bin}s').sum()
        rlt.rename(columns={'status': 'status_count', 'nack': 'nack_count'}, inplace=True)
        return rlt

    # ---- F1U 接收缓冲 ----

    def f1u_buffer_timeseries(self, time_bin=1):
        """
        F1U 接收缓冲时序（上行方向，字节）

        Returns:
            DataFrame: index=time
        """
        df = self._data()
        cols = [c for c in self.F1U_COLS if c in df.columns]
        if not cols or 'LocalTime' not in df.columns:
            return pd.DataFrame()
        sub = df[['LocalTime'] + cols].dropna(subset=cols).copy()
        sub = _add_localtime_index(sub)
        rlt = sub[cols].resample(f'{time_bin}s').mean()
        short = {c: c.split('.')[-1].replace('u32', '') for c in cols}
        rlt.rename(columns=short, inplace=True)
        return rlt

    # ---- TB 异常统计 ----

    def tb_abnormal_stats(self):
        """
        TB 异常统计（UL_TB_TIME_COST.u32TbAbnormal）

        Returns:
            dict: {'total_tb', 'abnormal_tb', 'abnormal_ratio'}
        """
        df = self._data()
        col = 'UL_TB_TIME_COST.u32TbAbnormal'
        if col not in df.columns:
            return {}
        s = df[col].dropna()
        total = len(s)
        abnormal = int((s > 0).sum())
        return {
            'total_tb': total,
            'abnormal_tb': abnormal,
            'abnormal_ratio': abnormal / total if total > 0 else 0.0,
        }

    def describe(self):
        df = self._data()
        if df.empty:
            return "No RLC UL data found."
        uegids = sorted(df['UEGID'].dropna().unique().astype(int)) if 'UEGID' in df.columns else []
        lt = df['LocalTime'].dropna() if 'LocalTime' in df.columns else pd.Series()
        t_range = f"{lt.iloc[0]} ~ {lt.iloc[-1]}" if len(lt) >= 2 else 'N/A'
        data_rows = df['UL_DATA_PDU.u32Vrr'].dropna().shape[0] if 'UL_DATA_PDU.u32Vrr' in df.columns else 0
        status_rows = df['UL_STATUS_PDU.u32Acksn'].dropna().shape[0] if 'UL_STATUS_PDU.u32Acksn' in df.columns else 0
        return {
            'total_rows': len(df),
            'data_pdu_rows': data_rows,
            'status_pdu_rows': status_rows,
            'uegids': uegids,
            'time_range': t_range,
            'rlc_mode': 'AM (mode=1)',
        }


# ============================================================
# RlcAnalysis — 组合入口
# ============================================================

class RlcAnalysis:
    """
    RLC 综合分析入口，同时管理上下行分析实例。

    Usage:
        rlc = RlcAnalysis(directory)
        rlc.dl.dl_delay_stats()
        rlc.ul.air_cost_stats()
    """

    def __init__(self, directory, uegid=None, cellid=None, time_interval=None):
        id_filter = {}
        if uegid is not None:
            id_filter['UEGID'] = [uegid] if not isinstance(uegid, list) else uegid
        if cellid is not None:
            id_filter['CellId'] = [cellid] if not isinstance(cellid, list) else cellid

        self.dl = RlcDl(directory, id_filter=id_filter if id_filter else None,
                        time_interval=time_interval)
        self.ul = RlcUl(directory, id_filter=id_filter if id_filter else None,
                        time_interval=time_interval)
        self._directory = directory

    @property
    def directory(self):
        return self._directory

    def summary(self):
        print("=== RLC DL ===")
        print(self.dl.describe())
        print("\n=== RLC UL ===")
        print(self.ul.describe())
