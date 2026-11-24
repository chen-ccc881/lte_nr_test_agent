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

# 配置中文字体
matplotlib.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'Arial Unicode MS']
matplotlib.rcParams['axes.unicode_minus'] = False


class UlSchd():
    '''上行调度Log分析类'''

    def __init__(self, log, cell=None, uegid=None):
        self._type = const.NR_FILE_ULSCHD
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
                        
    def harqfail_cnt(self, time_bin=1):
        '''画图描述指定粒度下的harqfail次数

            Args:
                col_name: 待分析字段
                time_bin：统计粒度，默认为1s
            Returns：
                趋势图：x轴为时间粒度，y轴为调度UE次数
        '''
        col_name = ['CRCI.u8IsHarqFail']  
        return self._log.cnt_of_cols(col_name, time_bin=time_bin)
        
    def pathloss(self, time_bin=1, ax=None):
        col_name = ['PHR.u16PathLoss']
        return self._log.mean_of_cols(col_name, time_bin=time_bin)
    
    def singlerbsinr(self, time_bin=1):
        # fix: DU2.2 日志中 PUSCH_SINR.s16SingleRbSINR 已不存在，改用 SRS.s16SingleRbSINRRank1
        col_name = ['SRS.s16SingleRbSINRRank1']
        return self._log.mean_of_cols(col_name, time_bin=time_bin)


    def show_amc(self, time_bin=1):
        '''画图描述指定粒度下的调度RB数

            Args:
                time_bin：统计粒度，默认为1s
            Returns：
                趋势图：x轴为时间粒度，y轴为平均1rb_sinr, pl, delta, bler
        '''
        # fix: DU2.2 日志中 PUSCH_SINR 组无 s16RptSinr/s16SingleRbSINR 列
        # SINR 相关数据改从 CRCI.s16PuschSnr（宽带 SINR）和 SRS.s16SingleRbSINRRank1（单 RB SINR）读取
        col_name = ['CRCI.s16PuschSnr', 'SRS.s16SingleRbSINRRank1']
        return self._log.min_of_cols(col_name, time_bin=time_bin)

    def schd_uecnt(self, time_bin=1):
        '''画图描述指定粒度下的调度UE次数

            Args:
                col_name: 待分析字段
                time_bin：统计粒度，默认为1s
            Returns：
                趋势图：x轴为时间粒度，y轴为调度UE次数
        '''
        col_name = ['GRANT.u8HarqId']
        return self._log.cnt_of_cols(col_name, time_bin=time_bin)

    def schd_rbnum(self, time_bin=1):
        '''画图描述指定粒度下的调度RB数

            Args:
                col_name: 待分析字段
                time_bin：统计粒度，默认为1s
            Returns：
                趋势图：x轴为时间粒度，y轴为平均RB数
        '''
        col_name = ['GRANT.u16RbNum']
        return self._log.mean_of_cols(col_name, time_bin=time_bin)
    
    def schd_mcs(self, time_bin=1):
        '''画图描述指定粒度下的调度mcs

            Args:
                col_name: 待分析字段
                time_bin：统计粒度，默认为1s
            Returns：
                趋势图：x轴为时间粒度，y轴为平均RB数
        '''
        col_name = ['GRANT.u8Mcs']
        return self._log.mean_of_cols(col_name, time_bin=time_bin)
        
    def rpt_minbsr(self, lchgrp=7, time_bin=1):
        '''画图描述指定粒度下的report bsr

            Args:
                lchgrp: 待分析字段
                time_bin：统计粒度，默认为1s
            Returns：
                趋势图：x轴为时间粒度，y轴为平均RB数
        '''
        col_name = ['BSR.u32LchGrpBsr']
        filters = {'BSR.u32LchGrpId': [lchgrp]}
        return self._log.min_of_cols(col_name, time_bin=time_bin, filters=filters)
        
    def rpt_maxbsr(self, lchgrp=7, time_bin=1):
        '''画图描述指定粒度下的report bsr

            Args:
                lchgrp: 待分析lchgrpId
                time_bin：统计粒度，默认为1s
            Returns：
                趋势图：x轴为时间粒度，y轴为平均RB数
        '''
        col_name = ['BSR.u32LchGrpBsr']
        filters = {'BSR.u32LchGrpId': [lchgrp]}
        return self._log.max_of_cols(col_name, time_bin=time_bin, filters=filters)
        
    def show_rpt_bsr(self, lchgrp=3):
        '''画图描述report bsr

            Args:
                lchgrp: 待分析lchgrpId
            Returns：
                趋势图：x轴为时间粒度，y轴为平均RB数
        '''
        col = ['AirTime', 'BSR.u32LchGrpId', 'BSR.u32LchGrpBsr']
        filters = {'BSR.u32LchGrpId': [lchgrp]}
        rpt_bsr = self._log.get_data_of_cols(cols=col, val_filter=filters)
        rpt_bsr = rpt_bsr.set_index(col[0])[col[2]]
        rpt_bsr.plot()
        return

    def schdfail_reasons(self):
        '''汇总调度失败原因
        
            Args：
                无
            Returns：
                所有失败原因的总数以及所占比例
        '''
        col = ['SCHD_FAIL_RSN.u32UeSchdFailRsn']
        rlt = pd.Series(name='SchdFail_Cnt')
        data = self._log.get_data_of_cols(col)
        data = data[col[0]].dropna().astype(np.int32).value_counts()
        # fix: rlt 永远不为 None（刚赋值为 Series），三元判断 else 分支永不执行，简化为直接 add
        rlt = rlt.add(data, fill_value=0)
        rlt.index = [const.NR_SCHD_FAIL_RSNS[int(idx)] for idx in rlt.index]
        rlt.index.name = 'Fail_Rsn'
        return rlt
            
    def throuput(self, time_bin=1):
        ''' 输出流量图

            根据时间粒度，输出流量图
            Args：
                airtime_bin_size: 时间粒度s
            Return:
                流量图
        '''
        # fix: DU2.2 日志中 UL 文件无 TB.u16TbSize 列，改用 GRANT.u32TbSize
        col_name = ['GRANT.u32TbSize']
        return self._log.sum_of_cols(col_name, time_bin=time_bin)
        
    def bler_of_slot(self, time_bin=1):
        '''计算指定时间粒度下特定子帧的bler,按照传输方案分别计算

            Args:
                time_bin：统计粒度，默认为1s; 0表示时间粒度为log持续时间
            Returns：
                bler， DataFrame格式，列为传输方案，行为时间
        '''
        
        ack_cols = ['LocalTime', 'CRCI.u8AckInfo', 'CRCI.u32DemTime']
        data = self._log.get_data_of_cols(ack_cols, format_time=True)
        data = data.dropna(how='any')
        # fix: 删除冗余的空 DataFrame concat，直接使用 data

        df = data.set_index(ack_cols[0]).astype(int)
        #df = df[df[ack_cols[2]]%256 == slot] if slot < 20 else df

        def bler(data):
            if data.value_counts().size:
                vc = data.value_counts().reindex([0,1,2],fill_value=0)
                total = vc[0] + vc[1] + vc[2]
                return (vc[0] + vc[2]) * 100 / total if total != 0 else 0
            else:
                return 0
        
        if time_bin:
            # fix: SeriesGroupBy 没有 .resample() 方法，必然崩溃。
            # 正确做法：提取 slot 列，先 floor 到时间粒度，再双 groupby 聚合，最后 unstack
            tmp = pd.DataFrame({
                '_tb':   df.index.floor(str(time_bin) + 's'),
                '_slot': df[ack_cols[2]] % 256,
                '_ack':  df[ack_cols[1]],
            })
            rlt = tmp.groupby(['_tb', '_slot'])['_ack'].apply(bler).unstack('_slot')
            rlt.index.name = "time"
            rlt.columns.name = "slot"
        else:
            rlt = df[ack_cols[1]].groupby(df[ack_cols[2]] % 256).apply(bler)
            rlt.index.name = "slot"
        
        return rlt
    
    def sinr_of_slot(self, time_bin=1):
        '''计算指定时间粒度下特定子帧的sinr,按照传输方案分别计算

            Args:
                time_bin：统计粒度，默认为1s; 0表示时间粒度为log持续时间
            Returns：
                sinr， DataFrame格式，列为传输方案，行为时间
        '''
        
        ack_cols = ['LocalTime', 'CRCI.s16PuschSnr', 'CRCI.u32DemTime']
        data = self._log.get_data_of_cols(ack_cols, format_time=True)
        data = data.dropna(how='any')
        # fix: 删除冗余的空 DataFrame concat，直接使用 data

        rlt = data.set_index(ack_cols[0]).astype(int)
        rlt = rlt[ack_cols[1]]
        
        if time_bin:
            rlt = rlt.resample(str(time_bin)+'S').mean()
            rlt.index.name = "time"
            # fix: 删除 rlt.name = "slot" 死代码（被下面那行立即覆盖）
        else:
            rlt = rlt.mean()
            # fix: 删除 rlt.name = "slot" 死代码（被下面那行立即覆盖）
        
        rlt.name = "sinr of slot"
        return rlt

    def ni_of_slot(self, time_bin=None):
        '''计算指定时间粒度下特定时隙的ni

            Args:
                time_bin：统计粒度，默认为1s; 0表示时间粒度为log持续时间
            Returns：
                ni， DataFrame格式
        '''
        
        ack_cols = ['LocalTime', 'CRCI.u32DemTime', 'CRCI.s16PuschNI']
        data = self._log.get_data_of_cols(ack_cols, format_time=True)
        data = data.dropna(how='any')
        # fix: 删除冗余的空 DataFrame concat，直接使用 data

        data = data.set_index(ack_cols[0]).astype(int)
        
        if time_bin:
            # fix: SeriesGroupBy 没有 .resample() 方法，必然崩溃
            # 正确做法：先按 slot 和时间粒度分组，再聚合
            data['_slot'] = data[ack_cols[1]] % 256
            data['_tb'] = data.index.floor(str(time_bin) + 'S')
            rlt = data.groupby(['_tb', '_slot'])[ack_cols[2]].max().unstack('_slot')
            rlt.index.name = "time"
        else:
            # fix: 删除无效的 .name 赋值，直接按 slot 分组取最大值
            rlt = data[ack_cols[2]].groupby(data[ack_cols[1]] % 256).apply(max)
            rlt.index.name = "slot"
        
        # fix: time_bin 分支下 rlt 是 DataFrame，.name 赋值无效；else 分支下 rlt 是 Series，
        # 此处统一删除，让调用方自行命名（与 dlschd 保持一致）
        return rlt
    
    
    def mcs_of_slot(self, time_bin=None):
        '''计算指定时间粒度下特定时隙的mcs

            Args:
                time_bin：统计粒度，默认为1s; 0表示时间粒度为log持续时间
            Returns：
                mcs， DataFrame格式
        '''
        
        ack_cols = ['LocalTime', 'Slot', 'GRANT.u8Mcs']
        data = self._log.get_data_of_cols(ack_cols, format_time=True)
        data = data.dropna(how='any')
        # fix: 删除冗余的空 DataFrame concat，直接使用 data

        data = data.set_index(ack_cols[0]).astype(int)
        
        if time_bin:
            # fix: SeriesGroupBy 没有 .resample() 方法，必然崩溃
            # 正确做法：先按 slot 和时间粒度分组，再 mean 聚合
            data['_tb'] = data.index.floor(str(time_bin) + 'S')
            rlt = data.groupby(['_tb', ack_cols[1]])[ack_cols[2]].mean().unstack(ack_cols[1])
            rlt.index.name = "time"

        else:
            # fix: 删除无效的 .name 赋值，直接按 Slot 分组取均值
            rlt = data[ack_cols[2]].groupby(data[ack_cols[1]]).apply(np.mean)
            rlt.index.name = "slot"
        
        # fix: time_bin 分支下 rlt 是 DataFrame，.name 赋值无效，删除死代码
        return rlt

    def find_selfmaintain(self):
        '''查找是否存在自维护, 并输出相关信息'''

        cols = ['UEGID', 'CRCI.u32DemTime', 'CRCI.u8HarqId', 'CRCI.u8IsSelfMainTain']
        data = self._log.get_data_of_cols(cols)
        return data[data[cols[3]] == 1]

    def find_harqfail(self):
        '''查找是否存harqfail, 并输出相关信息'''

        cols = ['UEGID', 'CRCI.u32DemTime', 'CRCI.u8HarqId', 'CRCI.u8IsHarqFail']
        data = self._log.get_data_of_cols(cols)
        return data[data[cols[3]] == 1]

    # -----------------------------------------------------------------------
    # UL MIMO 分析方法（UlSchd 基类，小区级/UE级均可用）
    # -----------------------------------------------------------------------

    def ul_layers_trend(self, time_bin=1, ratio=True):
        '''上行调度层数时间趋势

        注意：UL 层数字段为 GRANT.u8Layers，部分基站版本可能无此字段。
        若字段不存在，返回空 DataFrame。

        Args:
            time_bin : 统计粒度（秒），默认 1s
            ratio    : True=占比，False=绝对次数

        Returns:
            DataFrame，索引为时间，列为 1/2/3/4 层
        '''
        cols = ['LocalTime', const.UL_LAYERS_COL]
        try:
            data = self._log.get_data_of_cols(cols, format_time=True)
        except Exception:
            return pd.DataFrame()
        if data.empty:
            return pd.DataFrame()
        data = data.dropna(how='any')
        if data.empty:
            return pd.DataFrame()
        data = data.set_index(cols[0]).astype(int)
        tb_floor = data.index.floor(str(time_bin) + 'S')
        tmp = pd.DataFrame({'_tb': tb_floor, '_layer': data[cols[1]].values})
        result = tmp.groupby(['_tb', '_layer']).size().unstack(fill_value=0)
        for lyr in const.MIMO_LAYERS:
            if lyr not in result.columns:
                result[lyr] = 0
        result = result[const.MIMO_LAYERS]
        if ratio:
            row_sum = result.sum(axis=1)
            result = result.div(row_sum, axis=0)
        result.index.name = 'time'
        result.columns.name = 'layers'
        return result

    def ul_layers_dist(self):
        '''上行调度层数整体分布（count + ratio%）

        Returns:
            DataFrame，索引为 layers(1~4)，列为 count / ratio(%)
        '''
        cols = [const.UL_LAYERS_COL]
        try:
            data = self._log.get_data_of_cols(cols).dropna()
        except Exception:
            return pd.DataFrame()
        if data.empty:
            return pd.DataFrame()
        counts = data[cols[0]].astype(int).value_counts().sort_index()
        counts = counts.reindex(const.MIMO_LAYERS, fill_value=0)
        total = counts.sum()
        ratio = (counts / total * 100).round(2) if total > 0 else counts * 0.0
        dist = pd.DataFrame({'count': counts, 'ratio(%)': ratio})
        dist.index.name = 'layers'
        return dist

    def ul_mcs_per_layer(self, time_bin=1):
        '''各上行调度层数下的平均 MCS 趋势

        Args:
            time_bin : 统计粒度（秒），默认 1s

        Returns:
            DataFrame，索引为时间，列为 layer1_mcs / layer2_mcs / ...
        '''
        cols = ['LocalTime', const.UL_LAYERS_COL, const.UL_MCS_COL]
        try:
            data = self._log.get_data_of_cols(cols, format_time=True)
        except Exception:
            return pd.DataFrame()
        if data.empty:
            return pd.DataFrame()
        data = data.dropna(how='any')
        data[const.UL_LAYERS_COL] = data[const.UL_LAYERS_COL].astype(int)
        data[const.UL_MCS_COL]    = data[const.UL_MCS_COL].astype(int)
        data = data.set_index(cols[0])
        data['_tb'] = data.index.floor(str(time_bin) + 'S')
        result = data.groupby(['_tb', const.UL_LAYERS_COL])[const.UL_MCS_COL].mean().unstack(const.UL_LAYERS_COL)
        result.columns = [f'layer{int(c)}_mcs' for c in result.columns]
        result.index.name = 'time'
        return result

    def ul_bler_per_layer(self, time_bin=1):
        '''各上行调度层数下的 BLER 趋势

        Args:
            time_bin : 统计粒度（秒），默认 1s

        Returns:
            DataFrame，索引为时间，列为 layer1_bler(%) / layer2_bler(%) / ...
        '''
        cols = ['LocalTime', const.UL_LAYERS_COL, const.UL_ACK_INFO_COL]
        try:
            data = self._log.get_data_of_cols(cols, format_time=True)
        except Exception:
            return pd.DataFrame()
        if data.empty:
            return pd.DataFrame()
        data = data.dropna(how='any')
        data[const.UL_LAYERS_COL]   = data[const.UL_LAYERS_COL].astype(int)
        data[const.UL_ACK_INFO_COL] = data[const.UL_ACK_INFO_COL].astype(int)
        data = data.set_index(cols[0])

        def _bler(s):
            vc = s.value_counts().reindex(const.ACK_VALUES, fill_value=0)
            total = vc.sum()
            return (vc[const.ACK_NACK] + vc[const.ACK_DTX]) * 100 / total if total > 0 else 0.0

        data['_tb'] = data.index.floor(str(time_bin) + 'S')
        result = (data.groupby(['_tb', const.UL_LAYERS_COL])[const.UL_ACK_INFO_COL]
                  .apply(_bler).unstack(const.UL_LAYERS_COL))
        result.columns = [f'layer{int(c)}_bler(%)' for c in result.columns]
        result.index.name = 'time'
        return result

    def ul_sinr_per_layer(self, time_bin=1):
        '''各上行调度层数下的 PUSCH SNR 趋势（dB）

        Args:
            time_bin : 统计粒度（秒），默认 1s

        Returns:
            DataFrame，索引为时间，列为 layer1_snr(dB) / layer2_snr(dB) / ...
        '''
        cols = ['LocalTime', const.UL_LAYERS_COL, const.UL_PUSCH_SNR_COL]
        try:
            data = self._log.get_data_of_cols(cols, format_time=True)
        except Exception:
            return pd.DataFrame()
        if data.empty:
            return pd.DataFrame()
        data = data.dropna(how='any')
        data[const.UL_LAYERS_COL]    = data[const.UL_LAYERS_COL].astype(int)
        # PUSCH SNR 原始值单位为 0.1dB
        data[const.UL_PUSCH_SNR_COL] = data[const.UL_PUSCH_SNR_COL] / const.DL_SINR_SCALE
        data = data.set_index(cols[0])
        data['_tb'] = data.index.floor(str(time_bin) + 'S')
        result = data.groupby(['_tb', const.UL_LAYERS_COL])[const.UL_PUSCH_SNR_COL].mean().unstack(const.UL_LAYERS_COL)
        result.columns = [f'layer{int(c)}_snr(dB)' for c in result.columns]
        result.index.name = 'time'
        return result

    def ul_mcs_bler_summary(self):
        '''各上行调度层数下的 MCS 和 BLER 汇总表（整体统计）

        Returns:
            DataFrame，索引为 layers(1~4)，列为：
              - schd_cnt : 调度次数
              - mcs_mean : 平均 MCS
              - mcs_min  : 最小 MCS
              - bler(%)  : 整体 BLER
        '''
        cols = [const.UL_LAYERS_COL, const.UL_MCS_COL, const.UL_ACK_INFO_COL]
        try:
            data = self._log.get_data_of_cols(cols).dropna(how='any')
        except Exception:
            return pd.DataFrame()
        if data.empty:
            return pd.DataFrame()

        data[const.UL_LAYERS_COL]   = data[const.UL_LAYERS_COL].astype(int)
        data[const.UL_MCS_COL]      = data[const.UL_MCS_COL].astype(int)
        data[const.UL_ACK_INFO_COL] = data[const.UL_ACK_INFO_COL].astype(int)

        def _bler(s):
            vc = s.value_counts().reindex(const.ACK_VALUES, fill_value=0)
            total = vc.sum()
            return (vc[const.ACK_NACK] + vc[const.ACK_DTX]) * 100 / total if total > 0 else 0.0

        grp = data.groupby(const.UL_LAYERS_COL)
        summary = pd.DataFrame({
            'schd_cnt': grp[const.UL_MCS_COL].count(),
            'mcs_mean': grp[const.UL_MCS_COL].mean().round(2),
            'mcs_min':  grp[const.UL_MCS_COL].min(),
            'bler(%)':  grp[const.UL_ACK_INFO_COL].apply(_bler).round(2),
        })
        summary.index.name = 'layers'
        summary = summary.reindex(const.MIMO_LAYERS)
        return summary

    def ul_srs_sinr_trend(self, time_bin=1):
        '''上行 SRS 单 RB SINR 时间趋势（dB，Rank1）

        SRS SINR 反映上行信道质量，是 UL MIMO 层数决策的核心依据。

        Args:
            time_bin : 统计粒度（秒），默认 1s

        Returns:
            DataFrame，索引为时间，列为 srs_sinr_mean/min/max(dB)
        '''
        cols = ['LocalTime', const.UL_SRS_SINR_COL]
        data = self._log.get_data_of_cols(cols, format_time=True)
        if data.empty:
            return pd.DataFrame()
        data = data.dropna(how='any')
        # SRS SINR 原始值单位为 0.1dB
        data[const.UL_SRS_SINR_COL] = data[const.UL_SRS_SINR_COL] / const.DL_SINR_SCALE
        data = data.set_index(cols[0])
        result = data.resample(str(time_bin) + 'S').agg(
            srs_sinr_mean=(const.UL_SRS_SINR_COL, 'mean'),
            srs_sinr_min=(const.UL_SRS_SINR_COL, 'min'),
            srs_sinr_max=(const.UL_SRS_SINR_COL, 'max'),
        )
        return result

class UlSchdCell(UlSchd):
    '''上行调度Log分析类'''

    def __init__(self, log, cell):
        super(UlSchdCell, self).__init__(log, cell)

class UlSchdUe(UlSchd):
    '''上行调度UE分析类'''

    def __init__(self, log, cell, uegid):
        super(UlSchdUe, self).__init__(log, cell, uegid)
        self._uegid = uegid
