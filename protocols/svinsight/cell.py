# coding=utf-8
import numpy as np
import pandas as pd
import const
from dlschd import DlSchdCell
from ue import Ue
from ulschd import UlSchdCell
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


class Cell(object):
    '''小区实例'''

    def __init__(self, cellid, log):
        '''初始化小区实例
            根据输入的信息，完成如下事情：
                a) 尝试推断上下行配比,有可能需要用户输入

            Args:
                cellid: 小区Id
        '''
        self._cellid = cellid
        self._log = log

        dl_log = log.get_dlschd_logfile(cellid=cellid)
        if dl_log is not None:
            self._dl = DlSchdCell(dl_log, self)

        ul_log = log.get_ulschd_logfile(cellid=cellid)
        if ul_log is not None:
            self._ul = UlSchdCell(ul_log, self)

        # PDCP / RLC 分析实例（按 cellid 过滤，日志目录取自 log）
        self._pdcp = PdcpAnalysis(log.directory, cellid=cellid)
        self._rlc  = RlcAnalysis(log.directory, cellid=cellid)

        self._ues = {}
        self._uegids = self._get_uegids()

    @property
    def cellid(self):
        return self._cellid

    @property
    def log(self):
        return self._log

    @property
    def dl(self):
        return getattr(self, '_dl', None)

    @property
    def ul(self):
        return getattr(self, '_ul', None)

    @property
    def ni(self):
        return getattr(self, '_ni', None)

    @property
    def pdcp(self):
        return self._pdcp

    @property
    def rlc(self):
        return self._rlc

    def _get_uegids(self):
        '''获取本小区下的所有UEGID（调度日志 + PDCP/RLC 日志合并去重）'''
        dluegid = self._dl.log.uegids if hasattr(self, '_dl') else set()
        uluegid = self._ul.log.uegids if hasattr(self, '_ul') else set()
        # 从 PDCP/RLC 日志补充 UEGID
        pdcp_uegids = set()
        rlc_uegids  = set()
        try:
            pdcp_ul_df = self._pdcp.ul._data()
            if not pdcp_ul_df.empty and 'UEGID' in pdcp_ul_df.columns:
                pdcp_uegids = set(pdcp_ul_df['UEGID'].dropna().astype(int).unique())
        except Exception:
            pass
        try:
            rlc_dl_df = self._rlc.dl._data()
            if not rlc_dl_df.empty and 'UEGID' in rlc_dl_df.columns:
                rlc_uegids = set(rlc_dl_df['UEGID'].dropna().astype(int).unique())
        except Exception:
            pass
        return dluegid | uluegid | pdcp_uegids | rlc_uegids

    def get_ue(self, uegid=None):
        '''获取小区实例
            Args：
                uegid：如果想查看Log中的所有uegid，那么不用赋值
            Returns:
                如果uegid不为None，返回对应的UE实例，否则返回Log中的所有uegid
        '''
        if uegid is None:
            return '所有UEGID：{uegids}. 请使用get_ue(uegid)获取对应的UE实例'.format(uegids=self._uegids)

        if uegid in self._uegids:
            dllog = self.log.get_dlschd_logfile(self.cellid, uegid)
            ullog = self.log.get_ulschd_logfile(self.cellid, uegid)
            srslog = self.log.get_physrs_logfile(uegid=uegid)
            self._ues[uegid] = Ue(ullog, dllog, srslog, self, uegid)
            return self._ues[uegid]
        else:
            return '非法uegid值，此ue不存在'

    def show_ue_livetime(self, time_bin=1):
        """Display UE lifetime (scheduling activity timeline).

        Args:
            time_bin: Time bin size in seconds (default: 1)

        Returns:
            pivot DataFrame (time x UEGID, values = schedule count), or None
        """
        try:
            if not isinstance(time_bin, (int, float)) or time_bin <= 0:
                raise ValueError("time_bin must be a positive number")

            cols = ['LocalTime', 'UEGID']

            logs = []
            if hasattr(self, '_dl') and self._dl is not None:
                logs.append(self.dl.log)
            if hasattr(self, '_ul') and self._ul is not None:
                logs.append(self.ul.log)

            if not logs:
                print("No log data available")
                return None

            data_list = []
            for log in logs:
                try:
                    data = log.get_data_of_cols(cols, format_time=True)
                    if data is not None and not data.empty:
                        data_list.append(data[cols])
                except Exception as e:
                    print(f"Error reading log data: {e}")

            if not data_list:
                print("No valid data found")
                return None

            df = pd.concat(data_list, ignore_index=True)
            df = df.dropna(subset=['LocalTime', 'UEGID'])
            df['LocalTime'] = pd.to_datetime(df['LocalTime'], errors='coerce')
            df = df.dropna(subset=['LocalTime'])

            # Use groupby+pivot instead of resample().apply().unstack()
            # to avoid the zero-size array bug in older pandas versions
            rule = f'{time_bin}s'
            df['_tb'] = df['LocalTime'].dt.floor(rule)
            pivot = (
                df.groupby(['_tb', 'UEGID'])
                .size()
                .reset_index(name='_cnt')
                .pivot(index='_tb', columns='UEGID', values='_cnt')
                .fillna(0)
            )
            pivot.index = pd.DatetimeIndex(pivot.index)
            pivot.index.name = 'LocalTime'
            pivot.columns.name = 'UEGID'

            # Plot
            import matplotlib.pyplot as _plt
            import matplotlib.colors as _mc
            import numpy as _np

            uegids = pivot.columns.tolist()
            times = pivot.index
            n_ue = len(uegids)
            fig_h = max(4, n_ue * 0.35 + 2)
            fig, ax = _plt.subplots(figsize=(13, fig_h))

            data_mat = pivot[uegids].values.T  # (n_ue, n_time)
            vmax = data_mat.max() if data_mat.max() > 0 else 1
            norm = _mc.Normalize(vmin=0, vmax=vmax)
            cmap = _plt.cm.Blues

            # fix: 若 times 为空则提前返回，避免 times[0] IndexError
            if not len(times):
                return pivot
            t0 = times[0]
            t_sec = _np.array([(t - t0).total_seconds() for t in times])
            bw = time_bin * 0.85

            for ue_i, uegid in enumerate(uegids):
                row = data_mat[ue_i]
                mask = row > 0
                for t_idx in _np.where(mask)[0]:
                    ax.barh(ue_i, bw, left=t_sec[t_idx], height=0.7,
                            color=cmap(norm(row[t_idx])), align='center')

            # X-axis: real time labels
            total_sec = float(t_sec[-1]) if len(t_sec) else 0
            n_ticks = min(8, max(4, int(total_sec // 5) + 1))
            tick_pos = _np.linspace(0, total_sec, n_ticks)
            tick_lbl = [(t0 + pd.Timedelta(seconds=float(p))).strftime('%H:%M:%S')
                        for p in tick_pos]
            ax.set_xticks(tick_pos)
            ax.set_xticklabels(tick_lbl, rotation=25, ha='right', fontsize=8)

            ax.set_yticks(range(n_ue))
            ax.set_yticklabels([str(u) for u in uegids], fontsize=8)
            ax.set_xlabel('Time')
            ax.set_ylabel('UEGID')
            ax.set_title(f'Cell {self._cellid} - UE Lifetime  ({t0.strftime("%Y/%m/%d")})',
                         fontsize=11)
            ax.set_xlim(-bw, total_sec + bw)
            ax.grid(axis='x', alpha=0.25, linestyle='--')

            sm = _plt.cm.ScalarMappable(cmap=cmap, norm=norm)
            sm.set_array([])
            cbar = fig.colorbar(sm, ax=ax, fraction=0.02, pad=0.02)
            cbar.set_label('Schedules/bin', fontsize=8)

            _plt.tight_layout()
            return pivot

        except Exception as e:
            print(f"Error displaying UE lifetime: {e}")
            import traceback
            traceback.print_exc()
            return None

    def describe(self):
        """Cell summary statistics.

        Returns:
            Series with TotUeNum, dlschd_log_lines, ulschd_log_lines
        """
        try:
            rlt = pd.Series(name='Cell Info', dtype=object)
            rlt['TotUeNum'] = len(self._uegids)

            if hasattr(self, '_dl') and self._dl is not None and hasattr(self._dl, 'log'):
                rlt['dlschd_log_lines'] = self._dl.log.lines
            else:
                rlt['dlschd_log_lines'] = 0

            if hasattr(self, '_ul') and self._ul is not None and hasattr(self._ul, 'log'):
                rlt['ulschd_log_lines'] = self._ul.log.lines
            else:
                rlt['ulschd_log_lines'] = 0

            return rlt
        except Exception as e:
            print(f"Error getting cell description: {e}")
            return pd.Series(name='Cell Info', dtype=object)

    def describle(self):
        '''兼容旧接口，请使用 describe()'''
        return self.describe()
