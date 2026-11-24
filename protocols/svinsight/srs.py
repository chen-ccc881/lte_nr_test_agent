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

class Srs():
    '''SRS分析类'''

    def __init__(self, log, cell, uegid=None):
        self._type = const.NR_FILE_PHYSRS
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

    def get_ta(self):
        '''SRS TA统计

            Returns：
                趋势图：x轴为时间粒度，y轴为各传输方案比例
        '''
        cols = ['AirTime','Param.ccIndex', 'Param.Rnti', 'Param.SrsTaValue','Param.SinrAnt[0]']
        data = self._log.get_data_of_cols(cols).dropna(how='any')  
        return data[data[cols[4]] > 0].set_index(['AirTime','Param.Rnti','Param.ccIndex'], drop=True)
        #data[data['Param.ccIndex']==2]['Param.SrsTaValue'].plot(figsize=(15,10))
    
class SrsCell(Srs):
    '''下行调度分析类'''

    def __init__(self, log, cell):
        super(SrsCell, self).__init__(log, cell)


class SrsUe(Srs):
    '''下行调度UE分析类'''

    def __init__(self, log, cell, uegid):
        super(SrsUe, self).__init__(log, cell, uegid)
        self._uegid = uegid

