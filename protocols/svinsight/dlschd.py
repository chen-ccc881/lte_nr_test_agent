# coding=utf-8
try:
    import matplotlib
    import matplotlib.pyplot as plt
    import matplotlib.font_manager as _fm
except ImportError:  # 绘图为可选能力，数值分析不依赖 matplotlib
    class _MplShim:
        rcParams = {}
        @staticmethod
        def font_manager():
            return None
    matplotlib = _MplShim()
    plt = None
    _fm = None

import numpy as np
import pandas as pd
import const

class DlSchd():
    '''下行调度分析类'''

    def __init__(self, log, cell, uegid=None):
        self._type = const.NR_FILE_DLSCHD
        self._log = log
        self._id_filter = {}
        self._cell = cell
        if cell:
            self._id_filter['CellId'] = [cell.cellid]
        if uegid:
            self._id_filter['UEGID'] = [uegid]

    @property
    def type(self):
        return self._type

    @property
    def log(self):
        return self._log

    def bler_of_slot(self, time_bin=1):
        '''计算指定时间粒度下特定子帧的bler,按照传输方案分别计算

            Args:
                time_bin：统计粒度，默认为1s; 0表示时间粒度为log持续时间
            Returns：
                bler， DataFrame格式，列为传输方案，行为时间
        '''
        schd_cols = ['UEGID', 'SCHD.u32DemTime', 'SCHD.u8HarqId', 'Slot','LocalTime']
        ack_cols = ['UEGID', 'ACK.u32DemTime', 'ACK.u8HarqId', 'ACK.u8AckInfo']
        
        try:
            schd = self._log.get_data_of_cols(schd_cols, format_time=True)
            if schd is None or schd.empty:
                print(f"没有有效的调度数据")
                return pd.DataFrame()
            schd = schd.dropna(how='any')
        except Exception as e:
            print(f"获取调度数据时出错: {e}")
            return pd.DataFrame()
            
        try:
            ack = self._log.get_data_of_cols(ack_cols, format_time=False)
            if ack is None or ack.empty:
                print(f"没有有效的ACK数据")
                return pd.DataFrame()
            ack = ack.dropna(how='any')
        except Exception as e:
            print(f"获取ACK数据时出错: {e}")
            return pd.DataFrame()
        
        try:
            rlt = pd.merge(schd, ack, left_on=schd_cols[:3], right_on=ack_cols[:3], sort=False)
            rlt = rlt.dropna(how='any')
        except Exception as e:
            print(f"合并数据时出错: {e}")
            return pd.DataFrame()
        
        try:
            rlt = rlt.set_index(schd_cols[4])
            rlt = rlt.astype(int)
        except (ValueError, KeyError) as e:
            print(f"设置索引时出错: {e}")
            return pd.DataFrame()
            
        def bler(data):
            """计算BLER"""
            if data is None or (hasattr(data, 'empty') and data.empty):
                return 0
            vc = data.value_counts()
            vc = vc.reindex(const.ACK_VALUES, fill_value=0)
            total = vc.sum()
            if total == 0:
                return 0
            bler_value = (vc[const.ACK_NACK] + vc[const.ACK_DTX]) * 100 / total
            return max(0.0, min(100.0, bler_value))

        try:
            if time_bin:
                # fix: SeriesGroupBy 没有 .resample() 方法，必然崩溃。
                # 正确做法：提取 slot 列，先 floor 到时间粒度，再双 groupby 聚合，最后 unstack
                slot_col = rlt[schd_cols[3]]
                ack_col  = rlt[ack_cols[3]]
                tb_col   = rlt.index.floor(str(time_bin) + 's')
                tmp = pd.DataFrame({'_tb': tb_col, '_slot': slot_col, '_ack': ack_col})
                rlt = tmp.groupby(['_tb', '_slot'])['_ack'].apply(bler).unstack('_slot')
                rlt.index.name = "time"
                rlt.columns.name = "slot"
            else:
                rlt = rlt[ack_cols[3]].groupby(rlt[schd_cols[3]]).apply(bler)
                rlt.index.name = "slot"
        except Exception as e:
            print(f"重采样计算BLER时出错: {e}")
            return pd.DataFrame()
        
        return rlt

    def harq_txcnt(self):
        '''计算HARQ传输次数分布

            Args:
                
            Returns：
                harq传输次数， DataFrame格式
        '''
        cols = ['SCHD.u8TimeAllocIdx','SCHD.u8TxCnt']
        rlt = self._log.get_data_of_cols(cols).dropna(how='any')
        rlt = rlt[cols[1]].groupby(rlt[cols[0]]==7)
        
        def bler(data):
            if data.value_counts().size:
                vc = data.value_counts().reindex([0,1,2,4],fill_value=0)
                total = vc.sum()
                # fix: total==0 时返回全零 Series，语义正确
                return vc * 100 / total if total else pd.Series(0, index=vc.index)
            else:
                return 0

        rlt = rlt.apply(bler).unstack(0)
        rlt.index.name = "harq_tx_cnt"
        rlt.columns.name = "slot_type"
        # fix: unstack(0) 后列由实际分组键决定（False/True），列数可能不是2
        # 使用 rename 映射，避免因列数不匹配导致 Length mismatch
        col_map = {False: 'D', True: 'S'}
        rlt.columns = [col_map.get(c, str(c)) for c in rlt.columns]
        
        return rlt


    def mcs_of_slot(self):
        '''计算特定子帧的mcs

            Args:
                None
            Returns：
                mcs， DataFrame格式
        '''
        cols = ['Slot','SCHD.u8Mcs']
        data = self._log.get_data_of_cols(cols).dropna(how='any')
        return data[cols[1]].groupby(data[cols[0]]).mean()

    def mimo_layers(self, time_bin=1, ratio=True):
        '''DL MIMO 调度层数时间趋势

        按时间粒度统计各层次（1~4层）的调度次数或占比。

        Args:
            time_bin : 统计粒度（秒），默认 1s
            ratio    : True=返回占比（0~1），False=返回绝对计数

        Returns:
            DataFrame，索引为时间，列为 1/2/3/4 层，值为占比或计数
        '''
        cols = ['LocalTime', const.DL_LAYERS_COL]
        rlt = self._log.get_data_of_cols(cols, format_time=True).dropna(how='any')
        if rlt.empty:
            return pd.DataFrame()
        rlt = rlt.set_index(cols[0]).astype(int)
        rlt = rlt[cols[1]]
        tb_floor = rlt.index.floor(str(time_bin) + 'S')
        tmp = pd.DataFrame({'_tb': tb_floor, '_layer': rlt.values})
        rlt = tmp.groupby(['_tb', '_layer']).size().unstack(fill_value=0)
        for lyr in const.MIMO_LAYERS:
            if lyr not in rlt.columns:
                rlt[lyr] = 0
        rlt = rlt[const.MIMO_LAYERS]
        if ratio:
            row_sum = rlt.sum(axis=1)
            rlt = rlt.div(row_sum, axis=0)
        rlt.index.name = 'time'
        rlt.columns.name = 'layers'
        return rlt

    def mimo_layers_dist(self):
        '''DL MIMO 调度层数整体分布（count + ratio%）

        不按时间分段，返回整体统计，用于快速了解各层调度比例。

        Returns:
            DataFrame，索引为 layers(1~4)，列为 count / ratio(%)
        '''
        cols = [const.DL_LAYERS_COL]
        data = self._log.get_data_of_cols(cols).dropna()
        if data.empty:
            return pd.DataFrame()
        counts = data[cols[0]].astype(int).value_counts().sort_index()
        # 补齐缺失层
        counts = counts.reindex(const.MIMO_LAYERS, fill_value=0)
        total = counts.sum()
        ratio = (counts / total * 100).round(2) if total > 0 else counts * 0.0
        dist = pd.DataFrame({'count': counts, 'ratio(%)': ratio})
        dist.index.name = 'layers'
        return dist

    def rpt_csi(self, ri=1, time_bin=1):
        '''UE 上报 CSI 中指定 RI 的 CQI 趋势

        Args:
            ri       : 过滤的 RI 值（1/2/3/4），默认 1
            time_bin : 统计粒度（秒），默认 1s

        Returns:
            DataFrame，索引为时间，列为 CSI.u8RptRI / CSI.u8RptWideCqi（计数）
        '''
        cols = ['LocalTime', const.CSI_RPT_RI_COL, const.CSI_RPT_WIDE_CQI_COL]
        val_filter = {cols[1]: [ri]}
        rlt = self._log.get_data_of_cols(cols, val_filter=val_filter, format_time=True).dropna(how='any')
        if rlt.empty:
            return pd.DataFrame()
        rlt = rlt.set_index(cols[0])
        rlt = rlt[cols[1:]]
        return rlt.resample(str(time_bin) + 'S').count()

    def csi_stats(self, time_bin=1):
        '''UE 上报 CSI 综合统计：RI 分布趋势 + 宽带 CQI 均值趋势

        按时间粒度汇总：
          - ri_mean      : RI 均值趋势（反映 UE 信道条件）
          - cqi_mean     : 宽带 CQI 均值趋势
          - cqi_min/max  : CQI 范围（反映信道波动）
          - pmi_mode     : PMI 众数（最常用预编码方向，若字段存在）

        Args:
            time_bin : 统计粒度（秒），默认 1s

        Returns:
            DataFrame，索引为时间
        '''
        cols = ['LocalTime', const.CSI_RPT_RI_COL, const.CSI_RPT_WIDE_CQI_COL]
        # PMI 字段可能不存在，单独尝试
        data = self._log.get_data_of_cols(cols, format_time=True)
        if data.empty:
            return pd.DataFrame()
        data = data.dropna(subset=cols[1:])
        data = data.set_index(cols[0]).astype(int)
        result = data.resample(str(time_bin) + 'S').agg(
            ri_mean=(const.CSI_RPT_RI_COL, 'mean'),
            cqi_mean=(const.CSI_RPT_WIDE_CQI_COL, 'mean'),
            cqi_min=(const.CSI_RPT_WIDE_CQI_COL, 'min'),
            cqi_max=(const.CSI_RPT_WIDE_CQI_COL, 'max'),
        )
        return result

    def csi_ri_distribution(self):
        '''UE 上报 RI 整体分布（count + ratio%）

        用于快速了解：UE 主要以几层模式工作（反映信道质量上限）

        Returns:
            DataFrame，索引为 ri(1~4)，列为 count / ratio(%)
        '''
        cols = [const.CSI_RPT_RI_COL]
        data = self._log.get_data_of_cols(cols).dropna()
        if data.empty:
            return pd.DataFrame()
        counts = data[cols[0]].astype(int).value_counts().sort_index()
        counts = counts.reindex(const.MIMO_LAYERS, fill_value=0)
        total = counts.sum()
        ratio = (counts / total * 100).round(2) if total > 0 else counts * 0.0
        dist = pd.DataFrame({'count': counts, 'ratio(%)': ratio})
        dist.index.name = 'ri'
        return dist

    def csi_cqi_distribution(self):
        '''UE 上报宽带 CQI 整体分布（count + ratio%）

        CQI 分布反映信道质量：
          - CQI ≥ 10 → 信道好，可调高 MCS
          - CQI ≤ 5  → 信道差，需降 MCS 或减层

        Returns:
            DataFrame，索引为 cqi(1~15)，列为 count / ratio(%)
        '''
        cols = [const.CSI_RPT_WIDE_CQI_COL]
        data = self._log.get_data_of_cols(cols).dropna()
        if data.empty:
            return pd.DataFrame()
        data_int = data[cols[0]].astype(int)
        data_int = data_int[(data_int >= const.CSI_CQI_VALID_RANGE[0]) &
                            (data_int <= const.CSI_CQI_VALID_RANGE[1])]
        counts = data_int.value_counts().sort_index()
        counts = counts.reindex(range(const.CSI_CQI_VALID_RANGE[0],
                                     const.CSI_CQI_VALID_RANGE[1] + 1), fill_value=0)
        total = counts.sum()
        ratio = (counts / total * 100).round(2) if total > 0 else counts * 0.0
        dist = pd.DataFrame({'count': counts, 'ratio(%)': ratio})
        dist.index.name = 'cqi'
        return dist

    def amc(self, layer=1, time_bin=1):
        '''指定层数下的 AMC 参数趋势（InnerSINR / DeltaSINR / MCS）

        Args:
            layer    : 调度层数（1~4），默认 1
            time_bin : 统计粒度（秒），默认 1s

        Returns:
            DataFrame，索引为时间，列为 AMC.s16InnerSinr / AMC.s16DeltaSinr / SCHD.u8Mcs
        '''
        cols = ['LocalTime', const.DL_LAYERS_COL,
                const.DL_AMC_INNER_SINR_COL, const.DL_AMC_DELTA_SINR_COL,
                const.DL_MCS_COL]
        val_filter = {cols[1]: [layer]}
        rlt = self._log.get_data_of_cols(cols, val_filter=val_filter, format_time=True).dropna(how='any')
        if rlt.empty:
            return pd.DataFrame()
        rlt = rlt.set_index(cols[0])
        rlt = rlt[cols[2:]]
        return rlt.resample(str(time_bin) + 'S').mean()
         
    def schd_ue_cnt(self, time_bin=1):
        '''画图描述指定粒度下的调度UE次数

            Args:
                col_name: 待分析字段
                time_bin：统计粒度，默认为1s
            Returns：
                趋势图：x轴为时间粒度，y轴为调度UE次数
        '''
        col_name = ['SCHD.u8HarqId']
        return self._log.cnt_of_cols(col_name, time_bin=time_bin)
        

    def schd_rbnum(self, time_bin=1):
        '''画图描述指定粒度下的调度RB数

            Args:
                col_name: 待分析字段
                time_bin：统计粒度，默认为1s
            Returns：
                趋势图：x轴为时间粒度，y轴为平均RB数
        '''
        col_name = ['SCHD.u16RbNum']
        return self._log.sum_of_cols(col_name, time_bin=time_bin)
        

    def schdfail_reasons(self, time_bin=1):
        '''汇总调度失败原因

            Args：
                无
            Returns：
                所有失败原因的总数以及所占比例
        '''
        col_name = ['SCHD_FAIL_RSN.u32UeSchdFailRsn']
        return self._log.value_count_of_col(col_name, time_bin=time_bin)

    def throuput(self, time_bin=1):
        ''' 输出流量图

            根据时间粒度，输出流量图
            Args：
                time_bin: 时间粒度s
            Return:
                流量图
        '''
        col_name = ['SCHD.u32TbSize']
        return self._log.sum_of_cols(col_name, time_bin=time_bin)
    
    def dtx_cnt(self, time_bin=1):
        '''画图描述指定粒度下的dtx次数

            Args:
                col_name: 待分析字段
                time_bin：统计粒度，默认为1s
            Returns：
                趋势图：x轴为时间粒度，y轴为调度UE次数
        '''
        col_name = ['ACK.u8AckInfo']
        filters = {col_name[0]: [2]}
        return self._log.cnt_of_cols(col_name, time_bin=time_bin, filters=filters)
        
    def harqfail_cnt(self, time_bin=1):
        '''画图描述指定粒度下的harqfail次数

            Args:
                col_name: 待分析字段
                time_bin：统计粒度，默认为1s
            Returns：
                趋势图：x轴为时间粒度，y轴为调度UE次数
        '''
        col_name = ['ACK.u8IsHarqFail']
        filters = {col_name[0]: [1]}
        return self._log.cnt_of_cols(col_name, time_bin=time_bin, filters=filters)

    def selfmaintain_cnt(self, time_bin=1):
        '''画图描述指定粒度下的自维护次数

            Args:
                time_bin：统计粒度，默认为1s
            Returns：
                趋势图：x轴为时间粒度，y轴为调度UE次数
        '''

        col_name = ['ACK.u8IsSelfMainTain']
        filters = {col_name[0]: [1]}
        return self._log.cnt_of_cols(col_name, time_bin=time_bin, filters=filters)

        
    
class DlSchdCell(DlSchd):
    '''下行调度分析类'''

    def __init__(self, log, cell):
        super(DlSchdCell, self).__init__(log, cell)

    def describle_dtx(self):
        '''DTX时隙的UE调度组合
        '''
        
        cols = ['UEGID', 'ACK.u8AckInfo', 'ACK.u32DemTime']
        rlt = self._log.get_data_of_cols(cols).dropna(how='any')
        rlt = rlt[rlt[cols[1]] == 2].astype('uint')
        rlt = rlt.groupby(cols[2])[cols[0]].apply(lambda x: x.value_counts()).unstack().fillna(-1)
        rlt.reset_index(inplace=True)
        for col in rlt.columns[1:]:
            rlt.plot.scatter(x=cols[2], y=col,figsize=(15,5))
        return rlt

class DlSchdUe(DlSchd):
    '''下行调度UE分析类'''

    def __init__(self, log, cell, uegid):
        super(DlSchdUe, self).__init__(log, cell, uegid)
        self._uegid = uegid

    def show_ta(self, time_bin=1):
        '''按时间粒度统计 TA 测量与调整趋势

        计算逻辑：
          - TAC 命令: SCHD.u8Tac - TAC_OFFSET，255(无效) 映射为 0
          - rptTA: TA.as16RptTa / TA_RPT_SCALE，换算为 Ts 单位

        Args:
            time_bin: 统计粒度（秒），默认 1s

        Returns:
            DataFrame, 索引为时间, 列为:
              - tac_sum   : 该周期内 TAC 调整命令之和（正=提前，负=推迟）
              - rpt_ta_mean: 该周期内上报 TA 均值（Ts 单位 / 4）
        '''
        cols = ['LocalTime', const.TA_TAC_COL, const.TA_RPT_TA_COL]
        rlt = self._log.get_data_of_cols(cols, format_time=True)
        if rlt.empty:
            return pd.DataFrame()
        rlt = rlt.dropna(subset=[cols[1], cols[2]])
        rlt[cols[1]] = rlt[cols[1]].apply(
            lambda x: x - const.TAC_OFFSET if x != const.TAC_INVALID_VALUE else 0)
        rlt[cols[2]] = rlt[cols[2]] / const.TA_RPT_SCALE
        rlt = rlt.set_index(cols[0])
        rlt = rlt.rename(columns={cols[1]: 'tac_sum', cols[2]: 'rpt_ta_mean'})
        return rlt.resample(str(time_bin) + 'S').agg({'tac_sum': 'sum', 'rpt_ta_mean': 'mean'})

    def ta_stats(self, time_bin=1):
        '''TA 测量与调整综合统计

        在 show_ta 基础上，额外输出：
          - tac_nonzero_cnt : 周期内 TAC != 0（实际下发调整命令）的次数
          - tac_large_cnt   : 周期内 |TAC| >= TA_LARGE_THRESH 的次数（大步调整）
          - rpt_ta_max      : 周期内上报 TA 最大值
          - rpt_ta_min      : 周期内上报 TA 最小值
          - rpt_ta_std      : 周期内上报 TA 标准差（衡量波动程度）

        Args:
            time_bin: 统计粒度（秒），默认 1s

        Returns:
            DataFrame, 索引为时间
        '''
        cols = ['LocalTime', const.TA_TAC_COL, const.TA_RPT_TA_COL]
        rlt = self._log.get_data_of_cols(cols, format_time=True)
        if rlt.empty:
            return pd.DataFrame()
        rlt = rlt.dropna(subset=[cols[1], cols[2]])
        rlt['_tac'] = rlt[cols[1]].apply(
            lambda x: x - const.TAC_OFFSET if x != const.TAC_INVALID_VALUE else 0)
        rlt['_rpt_ta'] = rlt[cols[2]] / const.TA_RPT_SCALE
        rlt = rlt.set_index(cols[0])
        result = rlt.resample(str(time_bin) + 'S').agg(
            tac_sum=('_tac', 'sum'),
            tac_mean=('_tac', 'mean'),
            rpt_ta_mean=('_rpt_ta', 'mean'),
            rpt_ta_max=('_rpt_ta', 'max'),
            rpt_ta_min=('_rpt_ta', 'min'),
            rpt_ta_std=('_rpt_ta', 'std'),
        )
        # 非零 TAC 次数 & 大步调整次数需要自定义 agg
        def _nonzero_cnt(s):
            return (s != 0).sum()
        def _large_cnt(s):
            return (s.abs() >= const.TA_LARGE_THRESH).sum()
        extra = rlt.resample(str(time_bin) + 'S').agg(
            tac_nonzero_cnt=('_tac', _nonzero_cnt),
            tac_large_cnt=('_tac', _large_cnt),
        )
        return pd.concat([result, extra], axis=1)

    def ta_adjust_events(self):
        '''提取每次 TAC 非零（实际发生调整）的详细事件列表

        用于精确定位哪些时刻发生了较大的 TA 调整。

        Returns:
            DataFrame, 包含列:
              - LocalTime  : 调整发生的时刻
              - tac        : 本次调整量（已减 TAC_OFFSET，正=提前，负=推迟）
              - rpt_ta     : 当时上报的 TA 值（Ts/4 单位）
        '''
        cols = ['LocalTime', const.TA_TAC_COL, const.TA_RPT_TA_COL]
        data = self._log.get_data_of_cols(cols, format_time=True)
        if data.empty:
            return pd.DataFrame()
        data = data.dropna(subset=[cols[1], cols[2]])
        data['tac'] = data[cols[1]].apply(
            lambda x: x - const.TAC_OFFSET if x != const.TAC_INVALID_VALUE else 0)
        data['rpt_ta'] = data[cols[2]] / const.TA_RPT_SCALE
        # 只保留 TAC 实际发生变化（非 0）的事件
        data = data[data['tac'] != 0][['LocalTime', 'tac', 'rpt_ta']].reset_index(drop=True)
        return data

    def bsr(self):
        '''判断UE的bsr是否充足,schdbsr <= rlcbsr'''
        cols = ['LocalTime','LCH_SCHD.u32RlcRptBsr', 'LCH_SCHD.u32SchdBsr']
        data = self._log.get_data_of_cols(cols,format_time=True)
        data = data.set_index(cols[0])
        data.plot()
        return data

    def bsr_change_events(self):
        '''BSR change 事件列表（RLC 通知 MAC 的 BSR 状态变化）

        BSRCHANGE 结构体字段：
          - BSRCHANGE.u8LchId     : 逻辑信道 ID
          - BSRCHANGE.b8LchHasBsr : 该 LCH 是否有 BSR（1=有BSR，0=无BSR）

        用途：
          - 识别 BSR 从无到有（b8LchHasBsr=1）→ 新数据到达，触发调度
          - 识别 BSR 从有到无（b8LchHasBsr=0）→ 数据发送完毕，释放资源

        Returns:
            DataFrame: 包含 LocalTime, u8LchId, b8LchHasBsr 列
        '''
        cols = ['LocalTime', 'BSRCHANGE.u8LchId', 'BSRCHANGE.b8LchHasBsr']
        data = self._log.get_data_of_cols(cols, format_time=True)
        if data.empty:
            return pd.DataFrame()
        # 只保留有效的 BSR change 事件（LchId 非空）
        data = data.dropna(subset=['BSRCHANGE.u8LchId'])
        return data

    def bsr_change_stats(self, time_bin=1):
        '''BSR change 事件统计（按时间粒度）

        Args:
            time_bin: 统计粒度（秒），默认 1s

        Returns:
            DataFrame: 索引为时间，列为：
              - bsr_appear_cnt  : BSR 从无到有次数（b8LchHasBsr=1）
              - bsr_clear_cnt   : BSR 从有到无次数（b8LchHasBsr=0）
              - bsr_change_total: BSR 变化总次数
        '''
        cols = ['LocalTime', 'BSRCHANGE.u8LchId', 'BSRCHANGE.b8LchHasBsr']
        data = self._log.get_data_of_cols(cols, format_time=True)
        if data.empty:
            return pd.DataFrame()
        data = data.dropna(subset=['BSRCHANGE.u8LchId'])
        if data.empty:
            return pd.DataFrame()

        data = data.set_index('LocalTime')
        data['BSRCHANGE.b8LchHasBsr'] = data['BSRCHANGE.b8LchHasBsr'].astype(int)

        # 按时间粒度统计
        tb_floor = data.index.floor(str(time_bin) + 'S')
        tmp = pd.DataFrame({
            '_tb': tb_floor,
            '_has_bsr': data['BSRCHANGE.b8LchHasBsr'].values
        })

        result = tmp.groupby('_tb').agg(
            bsr_appear_cnt=('_has_bsr', lambda x: (x == 1).sum()),
            bsr_clear_cnt=('_has_bsr', lambda x: (x == 0).sum()),
        )
        result['bsr_change_total'] = result['bsr_appear_cnt'] + result['bsr_clear_cnt']
        result.index.name = 'time'
        return result

    def lch_schd_stats(self, time_bin=1):
        '''LCH 调度组包信息统计（按承载粒度）

        LCH_SCHD 结构体字段：
          - LCH_SCHD.u8LchId      : 逻辑信道 ID
          - LCH_SCHD.u32RlcRptBsr : RLC 上报的 BSR（字节数）
          - LCH_SCHD.u32SchdBsr   : 调度器实际使用的 BSR（字节数）

        用途：
          - 对比 RlcRptBsr vs SchdBsr，判断调度器是否充分利用 BSR
          - 识别 BSR 充足但调度不足（SchdBsr << RlcRptBsr）的情况

        Args:
            time_bin: 统计粒度（秒），默认 1s

        Returns:
            DataFrame: 索引为时间，列为各 LCH 的 BSR 统计
        '''
        cols = ['LocalTime', 'LCH_SCHD.u8LchId',
                'LCH_SCHD.u32RlcRptBsr', 'LCH_SCHD.u32SchdBsr']
        data = self._log.get_data_of_cols(cols, format_time=True)
        if data.empty:
            return pd.DataFrame()
        data = data.dropna(subset=['LCH_SCHD.u8LchId'])
        if data.empty:
            return pd.DataFrame()

        data = data.set_index('LocalTime')

        # 按时间粒度和 LCH 聚合
        tb_floor = data.index.floor(str(time_bin) + 'S')
        tmp = pd.DataFrame({
            '_tb': tb_floor,
            '_lch': data['LCH_SCHD.u8LchId'].values,
            '_rlc_bsr': data['LCH_SCHD.u32RlcRptBsr'].values,
            '_schd_bsr': data['LCH_SCHD.u32SchdBsr'].values,
        })

        result = tmp.groupby(['_tb', '_lch']).agg(
            rlc_bsr_mean=('_rlc_bsr', 'mean'),
            rlc_bsr_max=('_rlc_bsr', 'max'),
            schd_bsr_mean=('_schd_bsr', 'mean'),
            schd_bsr_max=('_schd_bsr', 'max'),
        )
        # 计算 BSR 利用率
        result['bsr_util_ratio'] = (result['schd_bsr_mean'] /
                                    result['rlc_bsr_mean'].replace(0, float('nan'))).round(3)
        return result

    def get_idx_of_lastschd(self, curtime):
        '''距离当前时间往前的最近一次调度索引'''
        return self._log.get_idx_of_last_cols(curtime, ['SCHD.u8HarqId'], how='all')

    # -----------------------------------------------------------------------
    # DL MIMO 分析方法
    # -----------------------------------------------------------------------

    def dl_layers_trend(self, time_bin=1, ratio=True):
        '''下行调度层数时间趋势（单 UE 视角）

        Args:
            time_bin : 统计粒度（秒），默认 1s
            ratio    : True=占比，False=绝对次数

        Returns:
            DataFrame，索引为时间，列为 1/2/3/4 层
        '''
        return self.mimo_layers(time_bin=time_bin, ratio=ratio)

    def dl_mcs_per_layer(self, time_bin=1):
        '''各调度层数下的平均 MCS 趋势

        分析 MIMO 自适应效果：多层调度时 MCS 是否因信道能力被合理利用
        （理想情况：层数增加时 MCS 保持稳定或略降，而非大幅下降）

        Args:
            time_bin : 统计粒度（秒），默认 1s

        Returns:
            DataFrame，索引为时间，列为 layer1_mcs / layer2_mcs / ...
        '''
        cols = ['LocalTime', const.DL_LAYERS_COL, const.DL_MCS_COL]
        data = self._log.get_data_of_cols(cols, format_time=True)
        if data.empty:
            return pd.DataFrame()
        data = data.dropna(how='any').astype({const.DL_LAYERS_COL: int,
                                              const.DL_MCS_COL: int})
        data = data.set_index(cols[0])
        data['_tb'] = data.index.floor(str(time_bin) + 'S')
        result = data.groupby(['_tb', const.DL_LAYERS_COL])[const.DL_MCS_COL].mean().unstack(const.DL_LAYERS_COL)
        result.columns = [f'layer{int(c)}_mcs' for c in result.columns]
        result.index.name = 'time'
        return result

    def dl_bler_per_layer(self, time_bin=1):
        '''各调度层数下的 BLER 趋势

        BLER 与层数的关系：
          - 若高层调度时 BLER 明显偏高 → 说明多层下 MCS 偏激进或信道不支持
          - BLER 稳定在 10% 左右 → AMC 工作正常

        Args:
            time_bin : 统计粒度（秒），默认 1s

        Returns:
            DataFrame，索引为时间，列为 layer1_bler(%) / layer2_bler(%) / ...
        '''
        # 需要关联 SCHD（含层数）和 ACK（含 AckInfo）
        schd_cols = ['UEGID', const.DL_DEMTIME_COL, const.DL_HARQID_COL,
                     const.DL_LAYERS_COL, 'LocalTime']
        ack_cols  = ['UEGID', const.DL_ACK_DEMTIME_COL, const.DL_ACK_HARQID_COL,
                     const.DL_ACK_INFO_COL]
        try:
            schd = self._log.get_data_of_cols(schd_cols, format_time=True).dropna(how='any')
            ack  = self._log.get_data_of_cols(ack_cols, format_time=False).dropna(how='any')
        except Exception as e:
            print(f'dl_bler_per_layer: 数据加载失败 {e}')
            return pd.DataFrame()
        if schd.empty or ack.empty:
            return pd.DataFrame()

        merged = pd.merge(schd, ack,
                          left_on=schd_cols[:3], right_on=ack_cols[:3],
                          sort=False).dropna(how='any')
        if merged.empty:
            return pd.DataFrame()

        merged = merged.set_index(schd_cols[4])
        merged[const.DL_LAYERS_COL] = merged[const.DL_LAYERS_COL].astype(int)
        merged[const.DL_ACK_INFO_COL] = merged[const.DL_ACK_INFO_COL].astype(int)

        def _bler(s):
            vc = s.value_counts().reindex(const.ACK_VALUES, fill_value=0)
            total = vc.sum()
            return (vc[const.ACK_NACK] + vc[const.ACK_DTX]) * 100 / total if total > 0 else 0.0

        tb_floor = merged.index.floor(str(time_bin) + 'S')
        tmp = pd.DataFrame({
            '_tb':    tb_floor,
            '_layer': merged[const.DL_LAYERS_COL].values,
            '_ack':   merged[const.DL_ACK_INFO_COL].values,
        })
        result = tmp.groupby(['_tb', '_layer'])['_ack'].apply(_bler).unstack('_layer')
        result.columns = [f'layer{int(c)}_bler(%)' for c in result.columns]
        result.index.name = 'time'
        return result

    def dl_sinr_per_layer(self, time_bin=1):
        '''各调度层数下的内环 SINR 趋势（AMC.s16InnerSinr，单位 dB）

        SINR 随层数变化的规律：
          - 多层时 SINR 应与单层相近（空分复用充分利用信道）
          - 若多层时 SINR 明显下降 → 信道不适合多层，应减层

        Args:
            time_bin : 统计粒度（秒），默认 1s

        Returns:
            DataFrame，索引为时间，列为 layer1_sinr(dB) / layer2_sinr(dB) / ...
        '''
        cols = ['LocalTime', const.DL_LAYERS_COL, const.DL_AMC_INNER_SINR_COL]
        data = self._log.get_data_of_cols(cols, format_time=True)
        if data.empty:
            return pd.DataFrame()
        data = data.dropna(how='any')
        data[const.DL_LAYERS_COL] = data[const.DL_LAYERS_COL].astype(int)
        data[const.DL_AMC_INNER_SINR_COL] = data[const.DL_AMC_INNER_SINR_COL] / const.DL_SINR_SCALE
        data = data.set_index(cols[0])
        data['_tb'] = data.index.floor(str(time_bin) + 'S')
        result = data.groupby(['_tb', const.DL_LAYERS_COL])[const.DL_AMC_INNER_SINR_COL].mean().unstack(const.DL_LAYERS_COL)
        result.columns = [f'layer{int(c)}_sinr(dB)' for c in result.columns]
        result.index.name = 'time'
        return result

    def dl_mcs_bler_summary(self):
        '''各调度层数下的 MCS 和 BLER 汇总表（整体统计，不分时间）

        输出：每层的平均 MCS、BLER、调度次数，用于快速评估各层调度质量。

        Returns:
            DataFrame，索引为 layers(1~4)，列为：
              - schd_cnt  : 调度次数
              - mcs_mean  : 平均 MCS
              - mcs_min   : 最小 MCS（反映信道最差时情况）
              - bler(%)   : 整体 BLER
        '''
        schd_cols = ['UEGID', const.DL_DEMTIME_COL, const.DL_HARQID_COL,
                     const.DL_LAYERS_COL, const.DL_MCS_COL]
        ack_cols  = ['UEGID', const.DL_ACK_DEMTIME_COL, const.DL_ACK_HARQID_COL,
                     const.DL_ACK_INFO_COL]
        try:
            schd = self._log.get_data_of_cols(schd_cols).dropna(how='any')
            ack  = self._log.get_data_of_cols(ack_cols).dropna(how='any')
        except Exception as e:
            print(f'dl_mcs_bler_summary: 数据加载失败 {e}')
            return pd.DataFrame()
        if schd.empty or ack.empty:
            return pd.DataFrame()

        merged = pd.merge(schd, ack,
                          left_on=schd_cols[:3], right_on=ack_cols[:3],
                          sort=False).dropna(how='any')
        if merged.empty:
            return pd.DataFrame()

        merged[const.DL_LAYERS_COL]   = merged[const.DL_LAYERS_COL].astype(int)
        merged[const.DL_MCS_COL]      = merged[const.DL_MCS_COL].astype(int)
        merged[const.DL_ACK_INFO_COL] = merged[const.DL_ACK_INFO_COL].astype(int)

        def _bler(s):
            vc = s.value_counts().reindex(const.ACK_VALUES, fill_value=0)
            total = vc.sum()
            return (vc[const.ACK_NACK] + vc[const.ACK_DTX]) * 100 / total if total > 0 else 0.0

        grp = merged.groupby(const.DL_LAYERS_COL)
        summary = pd.DataFrame({
            'schd_cnt': grp[const.DL_MCS_COL].count(),
            'mcs_mean': grp[const.DL_MCS_COL].mean().round(2),
            'mcs_min':  grp[const.DL_MCS_COL].min(),
            'bler(%)':  grp[const.DL_ACK_INFO_COL].apply(_bler).round(2),
        })
        summary.index.name = 'layers'
        # 补齐缺失层
        summary = summary.reindex(const.MIMO_LAYERS)
        return summary

    def dl_mimo_assess(self, time_bin=1):
        '''DL MIMO 自适应合理性评估

        综合对比 UE 上报 RI 与实际调度层数，判断 MIMO 自适应是否合理：

        评估维度：
          1. schd_layers_mean  : 平均调度层数
          2. csi_ri_mean       : UE 上报平均 RI
          3. layer_util_ratio  : 调度层数/RI（越接近1越合理）
          4. cqi_mean          : 宽带 CQI 均值（反映信道质量）
          5. inner_sinr_mean   : AMC 内环 SINR 均值（dB）
          6. verdict           : 诊断建议（under/over/ok）

        判断逻辑：
          - layer_util_ratio < MIMO_LAYER_UNDER_THRESH → 层数利用不足（under-utilization）
          - 实际层数 > RI → 层数过高（over-utilization，信道不支持）
          - 在合理范围内 → ok

        Args:
            time_bin : 统计粒度（秒），默认 1s

        Returns:
            DataFrame，索引为时间，多列评估指标
        '''
        # 1) 调度层数趋势
        schd_cols = ['LocalTime', const.DL_LAYERS_COL, const.DL_AMC_INNER_SINR_COL]
        schd_data = self._log.get_data_of_cols(schd_cols, format_time=True)
        if schd_data.empty:
            return pd.DataFrame()
        schd_data = schd_data.dropna(how='any')
        schd_data[const.DL_LAYERS_COL] = schd_data[const.DL_LAYERS_COL].astype(int)
        schd_data[const.DL_AMC_INNER_SINR_COL] = (
            schd_data[const.DL_AMC_INNER_SINR_COL] / const.DL_SINR_SCALE)
        schd_data = schd_data.set_index('LocalTime')

        # 2) CSI RI / CQI 趋势
        csi_cols = ['LocalTime', const.CSI_RPT_RI_COL, const.CSI_RPT_WIDE_CQI_COL]
        csi_data = self._log.get_data_of_cols(csi_cols, format_time=True)
        csi_data = csi_data.dropna(how='any')
        if not csi_data.empty:
            csi_data[const.CSI_RPT_RI_COL]      = csi_data[const.CSI_RPT_RI_COL].astype(int)
            csi_data[const.CSI_RPT_WIDE_CQI_COL] = csi_data[const.CSI_RPT_WIDE_CQI_COL].astype(int)
            csi_data = csi_data.set_index('LocalTime')

        tb_s = str(time_bin) + 'S'
        schd_agg = schd_data.resample(tb_s).agg(
            schd_layers_mean=(const.DL_LAYERS_COL, 'mean'),
            inner_sinr_mean=(const.DL_AMC_INNER_SINR_COL, 'mean'),
        )
        if not csi_data.empty:
            csi_agg = csi_data.resample(tb_s).agg(
                csi_ri_mean=(const.CSI_RPT_RI_COL, 'mean'),
                cqi_mean=(const.CSI_RPT_WIDE_CQI_COL, 'mean'),
            )
            result = pd.concat([schd_agg, csi_agg], axis=1)
        else:
            result = schd_agg
            result['csi_ri_mean'] = float('nan')
            result['cqi_mean']    = float('nan')

        # 3) 计算层数利用率
        result['layer_util_ratio'] = (
            result['schd_layers_mean'] / result['csi_ri_mean'].replace(0, float('nan'))
        ).round(3)

        # 4) 诊断建议
        def _verdict(row):
            util = row.get('layer_util_ratio', float('nan'))
            if pd.isna(util):
                return 'unknown'
            if util < const.MIMO_LAYER_UNDER_THRESH:
                return 'under'   # 层数利用不足
            if util > 1.05:      # 调度层数略超 RI（RI 更新有延迟，5% 容差）
                return 'over'    # 层数过高
            return 'ok'

        result['verdict'] = result.apply(_verdict, axis=1)
        result.index.name = 'time'
        return result

    def pucch_sinr_blow(self, fmt, thresh=-5, time_bin=1):
        '''统计 PUCCH SINR 低于阈值的次数趋势

        Args:
            fmt      : PUCCH format（0/1/2/3/4）
            thresh   : SINR 阈值（dB），默认 -5dB
            time_bin : 统计粒度（秒），默认 1s

        Returns:
            Series, 索引为时间，值为低 SINR 次数
        '''
        cols = ['LocalTime', const.PUCCH_PC_FORMAT_COL, const.PUCCH_PC_RPT_SINR_COL]
        data = self._log.get_data_of_cols(cols, format_time=True)
        if data.empty:
            return pd.Series(dtype=float)
        # SINR 原始值单位为 0.1dB，换算为 dB 后与阈值比较
        sinr_thresh_raw = thresh * const.PUCCH_SINR_SCALE
        data = data[(data[cols[1]] == fmt) & (data[cols[2]] <= sinr_thresh_raw)]
        data = data.set_index(cols[0])
        return data[cols[2]].resample(str(time_bin) + 'S').count()

    def pucch_pc(self, fmt=255, time_bin=1):
        '''PUCCH 功控核心趋势：测量 SINR / RSRP 及 TPC 命令统计

        按时间粒度汇总，输出：
          - rpt_sinr_mean    : 上报 SINR 均值（dB）
          - adj_sinr_mean    : 调整后 SINR 均值（dB）
          - filter_sinr_mean : 滤波 SINR 均值（dB）
          - rsrp_mean        : RSRP 均值（dBm）
          - ni_mean          : 干扰噪声 NI 均值
          - ant0_pwr_mean    : 天线 0 功率均值
          - tpc_mean         : TPC 命令均值（原始 dB，-1/0/1/3）

        Args:
            fmt      : PUCCH format，默认 255（全部 format）
            time_bin : 统计粒度（秒），默认 1s

        Returns:
            DataFrame, 索引为时间
        '''
        cols = [
            'LocalTime',
            const.PUCCH_PC_FORMAT_COL,
            const.PUCCH_PC_TPC_COL,
            const.PUCCH_PC_RPT_SINR_COL,
            const.PUCCH_PC_ADJ_SINR_COL,
            const.PUCCH_PC_FILTER_SINR_COL,
            const.PUCCH_PC_RSRP_COL,
            const.PUCCH_PC_NI_COL,
            const.PUCCH_PC_ANT0_PWR_COL,
        ]
        data = self._log.get_data_of_cols(cols, format_time=True)
        if data.empty:
            return pd.DataFrame()
        if fmt != const.PUCCH_FMT_ALL:
            data = data[data[const.PUCCH_PC_FORMAT_COL] == fmt]
        if data.empty:
            return pd.DataFrame()

        # TPC index 转换为 dB
        data['_tpc_db'] = data[const.PUCCH_PC_TPC_COL].map(const.PUCCH_TPC_TABLE)

        # SINR / RSRP 换算为 dB / dBm
        for col in [const.PUCCH_PC_RPT_SINR_COL,
                    const.PUCCH_PC_ADJ_SINR_COL,
                    const.PUCCH_PC_FILTER_SINR_COL]:
            if col in data.columns:
                data[col] = data[col] / const.PUCCH_SINR_SCALE
        if const.PUCCH_PC_RSRP_COL in data.columns:
            data[const.PUCCH_PC_RSRP_COL] = data[const.PUCCH_PC_RSRP_COL] / const.PUCCH_RSRP_SCALE

        data = data.set_index(cols[0])
        agg_map = {}
        col_rename = {}
        for col, name in [
            (const.PUCCH_PC_RPT_SINR_COL,    'rpt_sinr_mean'),
            (const.PUCCH_PC_ADJ_SINR_COL,    'adj_sinr_mean'),
            (const.PUCCH_PC_FILTER_SINR_COL, 'filter_sinr_mean'),
            (const.PUCCH_PC_RSRP_COL,        'rsrp_mean'),
            (const.PUCCH_PC_NI_COL,          'ni_mean'),
            (const.PUCCH_PC_ANT0_PWR_COL,    'ant0_pwr_mean'),
            ('_tpc_db',                       'tpc_db_mean'),
        ]:
            if col in data.columns:
                agg_map[col] = 'mean'
                col_rename[col] = name
        result = data.resample(str(time_bin) + 'S').agg(agg_map)
        result = result.rename(columns=col_rename)
        if '_tpc_db' in result.columns:
            result = result.rename(columns={'_tpc_db': 'tpc_db_mean'})
        return result

    def pucch_pc_stats(self, fmt=255, time_bin=1):
        '''PUCCH 功控详细统计（包含 SINR/RSRP 波动与 TPC 命令分布）

        在 pucch_pc 基础上额外输出：
          - rpt_sinr_min/max/std : 上报 SINR 最小/最大/标准差（dB）
          - rsrp_min/max         : RSRP 最小/最大值（dBm）
          - tpc_neg1_cnt         : TPC=-1dB（降功率）次数
          - tpc_0_cnt            : TPC=0dB（维持）次数
          - tpc_pos1_cnt         : TPC=+1dB（升功率）次数
          - tpc_pos3_cnt         : TPC=+3dB（大幅升功率）次数

        Args:
            fmt      : PUCCH format，255 表示全部 format
            time_bin : 统计粒度（秒），默认 1s

        Returns:
            DataFrame, 索引为时间
        '''
        cols = [
            'LocalTime',
            const.PUCCH_PC_FORMAT_COL,
            const.PUCCH_PC_TPC_COL,
            const.PUCCH_PC_RPT_SINR_COL,
            const.PUCCH_PC_ADJ_SINR_COL,
            const.PUCCH_PC_RSRP_COL,
        ]
        data = self._log.get_data_of_cols(cols, format_time=True)
        if data.empty:
            return pd.DataFrame()
        if fmt != const.PUCCH_FMT_ALL:
            data = data[data[const.PUCCH_PC_FORMAT_COL] == fmt]
        if data.empty:
            return pd.DataFrame()

        data['_tpc_db'] = data[const.PUCCH_PC_TPC_COL].map(const.PUCCH_TPC_TABLE)
        for col in [const.PUCCH_PC_RPT_SINR_COL, const.PUCCH_PC_ADJ_SINR_COL]:
            if col in data.columns:
                data[col] = data[col] / const.PUCCH_SINR_SCALE
        if const.PUCCH_PC_RSRP_COL in data.columns:
            data[const.PUCCH_PC_RSRP_COL] = data[const.PUCCH_PC_RSRP_COL] / const.PUCCH_RSRP_SCALE

        data = data.set_index(cols[0])

        # 基础统计
        agg_dict = {}
        col_names = {}
        for col, names in [
            (const.PUCCH_PC_RPT_SINR_COL, ('rpt_sinr_mean', 'rpt_sinr_min', 'rpt_sinr_max', 'rpt_sinr_std')),
            (const.PUCCH_PC_ADJ_SINR_COL, ('adj_sinr_mean', None, None, None)),
            (const.PUCCH_PC_RSRP_COL,     ('rsrp_mean', 'rsrp_min', 'rsrp_max', None)),
        ]:
            if col in data.columns:
                agg_dict[col] = ['mean', 'min', 'max', 'std']

        if not agg_dict:
            return pd.DataFrame()

        result = data.resample(str(time_bin) + 'S').agg(agg_dict)
        # 展平多级列名
        result.columns = ['_'.join(filter(None, c)).rstrip('_') for c in result.columns]

        # TPC 命令分布计数
        def _tpc_cnt(val):
            return lambda s: (s == val).sum()
        tpc_agg = data.resample(str(time_bin) + 'S').agg(
            tpc_neg1_cnt=('_tpc_db', _tpc_cnt(-1)),
            tpc_0_cnt=('_tpc_db', _tpc_cnt(0)),
            tpc_pos1_cnt=('_tpc_db', _tpc_cnt(1)),
            tpc_pos3_cnt=('_tpc_db', _tpc_cnt(3)),
        )
        return pd.concat([result, tpc_agg], axis=1)

    def pucch_tpc_distribution(self, fmt=255):
        '''统计 PUCCH TPC 命令的整体分布（不按时间分段）

        用于快速判断功控方向：
          - 若 TPC=-1 占比高 → UE 功率偏高，SINR 偏好
          - 若 TPC=+3 占比高 → UE 功率不足，SINR 偏低

        Args:
            fmt: PUCCH format，255 表示全部 format

        Returns:
            DataFrame, 列为 tpc_db(-1/0/1/3), 行为 [count, ratio(%)]
        '''
        cols = [const.PUCCH_PC_FORMAT_COL, const.PUCCH_PC_TPC_COL]
        data = self._log.get_data_of_cols(cols)
        if data.empty:
            return pd.DataFrame()
        if fmt != const.PUCCH_FMT_ALL:
            data = data[data[cols[0]] == fmt]
        if data.empty:
            return pd.DataFrame()
        data['_tpc_db'] = data[cols[1]].map(const.PUCCH_TPC_TABLE)
        counts = data['_tpc_db'].value_counts().sort_index()
        total = counts.sum()
        ratio = (counts / total * 100).round(2) if total > 0 else counts * 0.0
        dist = pd.DataFrame({'count': counts, 'ratio(%)': ratio})
        dist.index.name = 'tpc_db'
        return dist

