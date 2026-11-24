# coding=utf-8
import numpy as np
import pandas as pd
import const
from dlschd import DlSchdUe
from ulschd import UlSchdUe
from srs import SrsUe
from pdcp import PdcpAnalysis
from rlc import RlcAnalysis
# 配置中文字体（matplotlib 仅绘图所需，缺失时降级为空壳，数值分析不依赖）
try:
    import matplotlib
    import matplotlib.pyplot as plt
    import matplotlib.font_manager as _fm
    try:
        _preferred = ['Microsoft YaHei', 'SimHei', 'STXihei', 'SimSun',
                      'STSong', 'FangSong', 'KaiTi', 'WenQuanYi Micro Hei',
                      'Noto Sans CJK SC', 'Source Han Sans CN']
        _available = {f.name for f in _fm.fontManager.ttflist}
        for _font in _preferred:
            if _font in _available:
                matplotlib.rcParams['font.sans-serif'] = [_font, 'DejaVu Sans']
                matplotlib.rcParams['axes.unicode_minus'] = False
                break
        else:
            matplotlib.rcParams['font.sans-serif'] = ['DejaVu Sans']
            matplotlib.rcParams['axes.unicode_minus'] = False
    except Exception:
        pass
except ImportError:
    class _MplShim:
        rcParams = {}
    matplotlib = _MplShim()
    plt = None
    _fm = None


class Ue(object):
    def __init__(self, ullog, dllog, srslog, cell, uegid):
        self._uegid = uegid
        self._cell = cell
        if dllog:
            self._dllog = dllog
            self._dl = DlSchdUe(dllog, cell, uegid)
        if ullog:
            self._ullog = ullog
            self._ul = UlSchdUe(ullog, cell, uegid)
        if srslog:
            self._srslog = srslog
            self._srs = SrsUe(srslog, cell, uegid)

        # PDCP / RLC 分析实例（按 uegid 过滤）
        # 日志目录从 cell 或 log 中取；若 cell 为 None（通过 nrlog.get_ue 创建），
        # 则尝试从 dllog/ullog 反推目录
        directory = self._resolve_directory(cell, dllog, ullog)
        cellid = cell.cellid if cell is not None else None
        if directory:
            self._pdcp = PdcpAnalysis(directory, uegid=uegid, cellid=cellid)
            self._rlc  = RlcAnalysis(directory, uegid=uegid, cellid=cellid)

    @staticmethod
    def _resolve_directory(cell, dllog, ullog):
        """从 cell 或 log 对象中解析日志目录路径"""
        if cell is not None and hasattr(cell, 'log') and hasattr(cell.log, 'directory'):
            return cell.log.directory
        for log in (dllog, ullog):
            if log is not None and hasattr(log, '_directory'):
                return log._directory
        return None

    @property
    def uegid(self):
        return self._uegid

    @property
    def dl(self):
        return getattr(self, '_dl', None)

    @property
    def ul(self):
        return getattr(self, '_ul', None)
    
    @property
    def srs(self):
        return getattr(self, '_srs', None)

    @property
    def pdcp(self):
        return getattr(self, '_pdcp', None)

    @property
    def rlc(self):
        return getattr(self, '_rlc', None)
    
    def show_livetime(self):
        '''
            画出UE在各小区存在的时间
        '''
        # fix: plt 在 ImportError 时未定义，提前检查避免 NameError
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            print("show_livetime 需要 matplotlib，请先安装：pip install matplotlib")
            return

        logs = []
        if hasattr(self, '_dl'):
            logs.append(self.dl.log)
        if hasattr(self, '_ul'):
            logs.append(self.ul.log)
        if hasattr(self, '_srs'):
            logs.append(self.srs.log)

        if not logs:
            return

        rlt = pd.DataFrame()
        cols = ['LocalTime', 'CellId']
        for log in logs:
            # fix: 补加 format_time=True，使 LocalTime 被解析为 datetime
            # 否则按字符串 groupby，分组结果无意义且极慢
            data = log.get_data_of_cols(cols, format_time=True)
            group_data = data[cols[1]].groupby(data[cols[0]]).apply(lambda x: x.value_counts())
            if group_data.empty:
                continue
            group_data = group_data.unstack(level=1, fill_value=0)
            rlt = rlt.add(group_data, fill_value=0)
        
        rlt[rlt==0]=None
        rlt.columns.name = 'CellId'
        rlt.index.name = 'Time'
        fig, ax = plt.subplots(1, 1)
        rlt.plot(ax=ax, kind='line', title='Ue_Alive_time',figsize=(10,5))
        
        return

    # -----------------------------------------------------------------------
    # TA 测量与调整分析 —— 代理到 DlSchdUe
    # -----------------------------------------------------------------------

    def show_ta(self, time_bin=1):
        '''TA 测量与调整趋势（tac_sum + rpt_ta_mean）

        Args:
            time_bin: 统计粒度（秒），默认 1s

        Returns:
            DataFrame，列为 tac_sum / rpt_ta_mean，索引为时间
        '''
        if self.dl is None:
            raise RuntimeError('该 UE 没有下行调度 log，无法分析 TA')
        return self.dl.show_ta(time_bin=time_bin)

    def ta_stats(self, time_bin=1):
        '''TA 综合统计（均值/最大/最小/标准差/大步调整次数等）

        Args:
            time_bin: 统计粒度（秒），默认 1s

        Returns:
            DataFrame，多列统计指标，索引为时间
        '''
        if self.dl is None:
            raise RuntimeError('该 UE 没有下行调度 log，无法分析 TA')
        return self.dl.ta_stats(time_bin=time_bin)

    def ta_adjust_events(self):
        '''提取每次 TAC 非零调整的详细事件列表

        Returns:
            DataFrame，列为 LocalTime / tac / rpt_ta
        '''
        if self.dl is None:
            raise RuntimeError('该 UE 没有下行调度 log，无法分析 TA')
        return self.dl.ta_adjust_events()

    # -----------------------------------------------------------------------
    # PUCCH 功控分析 —— 代理到 DlSchdUe
    # -----------------------------------------------------------------------

    def pucch_pc(self, fmt=255, time_bin=1):
        '''PUCCH 功控核心趋势（SINR/RSRP/TPC 均值）

        Args:
            fmt      : PUCCH format（0-4），255 表示全部
            time_bin : 统计粒度（秒），默认 1s

        Returns:
            DataFrame，列为 rpt_sinr_mean/adj_sinr_mean/rsrp_mean/tpc_db_mean 等
        '''
        if self.dl is None:
            raise RuntimeError('该 UE 没有下行调度 log，无法分析 PUCCH 功控')
        return self.dl.pucch_pc(fmt=fmt, time_bin=time_bin)

    def pucch_pc_stats(self, fmt=255, time_bin=1):
        '''PUCCH 功控详细统计（含 TPC 命令分布计数）

        Args:
            fmt      : PUCCH format（0-4），255 表示全部
            time_bin : 统计粒度（秒），默认 1s

        Returns:
            DataFrame，包含 SINR/RSRP 最大/最小/标准差及 TPC 各档次数
        '''
        if self.dl is None:
            raise RuntimeError('该 UE 没有下行调度 log，无法分析 PUCCH 功控')
        return self.dl.pucch_pc_stats(fmt=fmt, time_bin=time_bin)

    def pucch_tpc_distribution(self, fmt=255):
        '''PUCCH TPC 命令整体分布（count + ratio）

        Args:
            fmt: PUCCH format（0-4），255 表示全部

        Returns:
            DataFrame，行为 tpc_db(-1/0/1/3)，列为 count/ratio(%)
        '''
        if self.dl is None:
            raise RuntimeError('该 UE 没有下行调度 log，无法分析 PUCCH 功控')
        return self.dl.pucch_tpc_distribution(fmt=fmt)

    def pucch_sinr_blow(self, fmt, thresh=-5, time_bin=1):
        '''统计 PUCCH 上报 SINR 低于阈值的次数趋势

        Args:
            fmt      : PUCCH format
            thresh   : SINR 阈值（dB），默认 -5dB
            time_bin : 统计粒度（秒），默认 1s

        Returns:
            Series，索引为时间，值为低 SINR 次数
        '''
        if self.dl is None:
            raise RuntimeError('该 UE 没有下行调度 log，无法分析 PUCCH 功控')
        return self.dl.pucch_sinr_blow(fmt=fmt, thresh=thresh, time_bin=time_bin)

    # -----------------------------------------------------------------------
    # DL MIMO 分析 —— 代理到 DlSchdUe
    # -----------------------------------------------------------------------

    def dl_layers_trend(self, time_bin=1, ratio=True):
        '''下行调度层数时间趋势

        Args:
            time_bin : 统计粒度（秒），默认 1s
            ratio    : True=占比，False=绝对次数

        Returns:
            DataFrame，索引为时间，列为 1/2/3/4 层
        '''
        if self.dl is None:
            raise RuntimeError('该 UE 没有下行调度 log')
        return self.dl.dl_layers_trend(time_bin=time_bin, ratio=ratio)

    def dl_mcs_per_layer(self, time_bin=1):
        '''各调度层数下的平均 MCS 趋势

        Args:
            time_bin : 统计粒度（秒），默认 1s

        Returns:
            DataFrame，列为 layer1_mcs / layer2_mcs / ...
        '''
        if self.dl is None:
            raise RuntimeError('该 UE 没有下行调度 log')
        return self.dl.dl_mcs_per_layer(time_bin=time_bin)

    def dl_bler_per_layer(self, time_bin=1):
        '''各调度层数下的 BLER 趋势

        Args:
            time_bin : 统计粒度（秒），默认 1s

        Returns:
            DataFrame，列为 layer1_bler(%) / layer2_bler(%) / ...
        '''
        if self.dl is None:
            raise RuntimeError('该 UE 没有下行调度 log')
        return self.dl.dl_bler_per_layer(time_bin=time_bin)

    def dl_sinr_per_layer(self, time_bin=1):
        '''各调度层数下的内环 SINR 趋势（dB）

        Args:
            time_bin : 统计粒度（秒），默认 1s

        Returns:
            DataFrame，列为 layer1_sinr(dB) / layer2_sinr(dB) / ...
        '''
        if self.dl is None:
            raise RuntimeError('该 UE 没有下行调度 log')
        return self.dl.dl_sinr_per_layer(time_bin=time_bin)

    def dl_mcs_bler_summary(self):
        '''各调度层数下的 MCS 和 BLER 汇总表

        Returns:
            DataFrame，索引为 layers(1~4)，列为 schd_cnt/mcs_mean/mcs_min/bler(%)
        '''
        if self.dl is None:
            raise RuntimeError('该 UE 没有下行调度 log')
        return self.dl.dl_mcs_bler_summary()

    def dl_mimo_assess(self, time_bin=1):
        '''DL MIMO 自适应合理性评估

        综合对比调度层数与上报 RI，输出 under/over/ok 诊断建议。

        Args:
            time_bin : 统计粒度（秒），默认 1s

        Returns:
            DataFrame，含 schd_layers_mean/csi_ri_mean/layer_util_ratio/verdict 等列
        '''
        if self.dl is None:
            raise RuntimeError('该 UE 没有下行调度 log')
        return self.dl.dl_mimo_assess(time_bin=time_bin)

    # -----------------------------------------------------------------------
    # DL CSI 分析 —— 代理到 DlSchdUe（继承自 DlSchd）
    # -----------------------------------------------------------------------

    def csi_stats(self, time_bin=1):
        '''UE 上报 CSI 综合统计（RI 均值 + CQI 均值/范围）

        Args:
            time_bin : 统计粒度（秒），默认 1s

        Returns:
            DataFrame，列为 ri_mean/cqi_mean/cqi_min/cqi_max
        '''
        if self.dl is None:
            raise RuntimeError('该 UE 没有下行调度 log')
        return self.dl.csi_stats(time_bin=time_bin)

    def csi_ri_distribution(self):
        '''UE 上报 RI 整体分布（count + ratio%）

        Returns:
            DataFrame，索引为 ri(1~4)，列为 count/ratio(%)
        '''
        if self.dl is None:
            raise RuntimeError('该 UE 没有下行调度 log')
        return self.dl.csi_ri_distribution()

    def csi_cqi_distribution(self):
        '''UE 上报宽带 CQI 整体分布（count + ratio%）

        Returns:
            DataFrame，索引为 cqi(1~15)，列为 count/ratio(%)
        '''
        if self.dl is None:
            raise RuntimeError('该 UE 没有下行调度 log')
        return self.dl.csi_cqi_distribution()

    # -----------------------------------------------------------------------
    # UL MIMO 分析 —— 代理到 UlSchdUe
    # -----------------------------------------------------------------------

    def ul_layers_trend(self, time_bin=1, ratio=True):
        '''上行调度层数时间趋势

        注：需 GRANT.u8Layers 字段存在（部分版本有效）

        Args:
            time_bin : 统计粒度（秒），默认 1s
            ratio    : True=占比，False=绝对次数

        Returns:
            DataFrame，索引为时间，列为 1/2/3/4 层
        '''
        if self.ul is None:
            raise RuntimeError('该 UE 没有上行调度 log')
        return self.ul.ul_layers_trend(time_bin=time_bin, ratio=ratio)

    def ul_mcs_per_layer(self, time_bin=1):
        '''各上行调度层数下的平均 MCS 趋势

        Args:
            time_bin : 统计粒度（秒），默认 1s

        Returns:
            DataFrame，列为 layer1_mcs / layer2_mcs / ...
        '''
        if self.ul is None:
            raise RuntimeError('该 UE 没有上行调度 log')
        return self.ul.ul_mcs_per_layer(time_bin=time_bin)

    def ul_bler_per_layer(self, time_bin=1):
        '''各上行调度层数下的 BLER 趋势

        Args:
            time_bin : 统计粒度（秒），默认 1s

        Returns:
            DataFrame，列为 layer1_bler(%) / layer2_bler(%) / ...
        '''
        if self.ul is None:
            raise RuntimeError('该 UE 没有上行调度 log')
        return self.ul.ul_bler_per_layer(time_bin=time_bin)

    def ul_sinr_per_layer(self, time_bin=1):
        '''各上行调度层数下的 PUSCH SNR 趋势（dB）

        Args:
            time_bin : 统计粒度（秒），默认 1s

        Returns:
            DataFrame，列为 layer1_snr(dB) / layer2_snr(dB) / ...
        '''
        if self.ul is None:
            raise RuntimeError('该 UE 没有上行调度 log')
        return self.ul.ul_sinr_per_layer(time_bin=time_bin)

    def ul_mcs_bler_summary(self):
        '''各上行调度层数下的 MCS 和 BLER 汇总表

        Returns:
            DataFrame，索引为 layers(1~4)，列为 schd_cnt/mcs_mean/mcs_min/bler(%)
        '''
        if self.ul is None:
            raise RuntimeError('该 UE 没有上行调度 log')
        return self.ul.ul_mcs_bler_summary()

    def ul_srs_sinr_trend(self, time_bin=1):
        '''上行 SRS 单 RB SINR 时间趋势（dB）

        SRS SINR 是 UL MIMO 层数决策的核心依据。

        Args:
            time_bin : 统计粒度（秒），默认 1s

        Returns:
            DataFrame，列为 srs_sinr_mean/min/max(dB)
        '''
        if self.ul is None:
            raise RuntimeError('该 UE 没有上行调度 log')
        return self.ul.ul_srs_sinr_trend(time_bin=time_bin)