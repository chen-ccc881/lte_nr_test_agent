# coding=utf-8
import numpy as np
import pandas as pd
import datetime
import os
import const
import threading
from ue import Ue
from cell import Cell
from util.ei2csv import ei2csv

class NrLog(object):
    ''' NR调度模块Log分析接口类

        主要提供如下3类功能：
        a) 信息呈现
        b）问题发现
        c）问题定位
        要求所有文件命名符合EI命名格式：子系统_时间.csv
    '''

    def __init__(self, directory, time_interval=None):
        '''初始化Log实例,把所有Log按照类型分类

           Args:
               directory: Log所在目录
               time_interval: 时间范围[start, end],格式为yyyy/mm/dd/ hh:mm:ss
        '''
        if time_interval:
            assert(len(time_interval)==2)
            assert(pd.to_datetime(time_interval[0]) <= pd.to_datetime(time_interval[1]))
            
        self._directory = directory
        self._logfiles={}
        self._time_interval = pd.to_datetime(time_interval) if time_interval else None
        self._cells = {}
        self._cellids = set()
        self._ues = {}
        self._cell_and_ue_ids = pd.DataFrame()

        files = pd.Series(os.listdir(self._directory))
        csv_files = set(f for f in os.listdir(self._directory) if f.endswith(r'.csv'))
        eifiles = files[files.apply(lambda x: x.endswith(r'.ei') and os.path.splitext(x)[0]+r'.csv' not in csv_files)]   
        if eifiles.size:
            if time_interval:
                # fix: reset_index 保持 eifiles、start、end 索引一致，避免布尔掩码对齐错误
                eifiles = eifiles.sort_values().reset_index(drop=True)
                start = eifiles.apply(lambda x: pd.to_datetime(os.path.splitext(x)[0].rsplit('_')[-1]))
                end = pd.concat([start[1:], start.iloc[[-1]]]).reset_index(drop=True)
                eifiles = eifiles[(start <= self._time_interval[1]) & (end >= self._time_interval[0])]
            ei2csv(directory,list(eifiles)) 

        for filetype in const.NR_FILE_TYPES:
            filenames = self._filenames_of_type(filetype)
            if filenames:
                logfile = NrFile(filetype, directory, filenames, time_interval=self._time_interval)
                if logfile.lines == 0:
                    continue
                self._logfiles[filetype] = logfile
                self._cellids = set.union(self._cellids, logfile.cellids)
                self._cell_and_ue_ids = pd.concat([logfile._cell_and_ue_ids, self._cell_and_ue_ids]).drop_duplicates()

        # LTE 制式文件已被 NR 管道隔离（列名不同），提示调用方改走 LTE 分析器
        lte = self.get_lte_filenames()
        if lte:
            print('检测到 %d 个 LTE 制式文件，已与 NR 管道隔离（列名不兼容）。' % len(lte))
            print('请使用 svinsightEILteValidator.analyze_svinsight_ei_lte() 分析，'
                  '或调用 nrlog.get_lte_filenames() 获取文件清单。')

    @property
    def directory(self):
        return self._directory

    def _filter_by_time(self, csvfiles):
        '''按 time_interval 过滤文件名列表（保留跨区间的相邻文件）

            Args：
                csvfiles：已排序的文件名列表
            Returns:
                文件名列表
        '''
        if self._time_interval is None:
            return list(csvfiles)

        names_of_filetype = []
        for i, name in enumerate(csvfiles):
            time = pd.to_datetime(os.path.splitext(name)[0].rsplit('_')[-1])
            if time > self._time_interval[1]:
                continue
            if time < self._time_interval[0] and i + 1 < len(csvfiles):
                nextname = csvfiles[i + 1]
                time = pd.to_datetime(os.path.splitext(nextname)[0].rsplit('_')[-1])
                if time < self._time_interval[0]:
                    continue
            names_of_filetype.append(name)
        return names_of_filetype

    def _filenames_of_type(self, filetype, exclude_lte=True):
        '''获取指定文件类型的所有文件名

            Args：
                filetype：文件类型
                exclude_lte：是否排除 LTE 制式文件，默认 True
            Returns:
                文件名列表
        '''
        csvfiles = pd.Series(np.sort(os.listdir(self._directory)))
        csvfiles = csvfiles[csvfiles.apply(lambda x: x.endswith(r'.csv'))]
        csvfiles = csvfiles[csvfiles.apply(lambda x: -1 != x.find(filetype))]

        # fix: LTE 详情文件（<前缀>_Lte_<时间>.csv）会被子串匹配误当成 NR 文件加载。
        # 但 dlschd/ulschd 按 NR 列名（CRCI.u8IsHarqFail、TB.u8Mcs0 等）读取，
        # 而 LTE 列名是另一套（ACK.u8Ack / ACK.b8IsHarqFail / TB.u8Mcs0），
        # 混入后会 "有数据但指标全空"。默认隔离，改由 get_lte_filenames() 分流
        # 给 LTE 专用分析器（svinsightEILteValidator）。
        if exclude_lte:
            csvfiles = csvfiles[csvfiles.apply(lambda x: not const.is_lte_filename(x))]

        return self._filter_by_time(list(csvfiles))

    def get_lte_filenames(self, filetype=None):
        '''获取 LTE 制式详情 CSV 文件名列表

            LTE 与 NR 列名不同，不能用本类的 NR 分析管道处理，需分流给
            LTE 专用分析器（BasicModel/maintenanceTool/svinsightEILteValidator.py）。

            Args：
                filetype：可选，限定文件类型前缀（如 'CMAC_dlUeTtiInfo'）
            Returns:
                文件名列表
        '''
        csvfiles = pd.Series(np.sort(os.listdir(self._directory)))
        csvfiles = csvfiles[csvfiles.apply(lambda x: x.endswith(r'.csv'))]
        csvfiles = csvfiles[csvfiles.apply(const.is_lte_filename)]
        if filetype:
            csvfiles = csvfiles[csvfiles.apply(lambda x: -1 != x.find(filetype))]
        return self._filter_by_time(list(csvfiles))

    @property
    def lte_files(self):
        '''当前目录下所有 LTE 制式详情文件名'''
        return self.get_lte_filenames()

    @property
    def has_lte(self):
        '''当前目录是否包含 LTE 制式文件（调用方应改走 LTE 分析器）'''
        return bool(self.get_lte_filenames())

    def describe(self):
        '''当前目录下相关Log文件总体描述，每类Log文件合并为一个文件

           输出文件名，大小，行数，时间范围，airtime范围等，每个Log文件一列
        '''
        df = pd.DataFrame()
        for filetype, logfile in self._logfiles.items():
            df.at[filetype, 'size'] = logfile.size
            df.at[filetype, 'num_of_files'] = len(logfile.files)
            df.at[filetype, 'num_of_lines'] = logfile.lines
            df.at[filetype, 'starttime'] = logfile.times[0]
            df.at[filetype, 'endtime'] = logfile.times[1]
        df.index.name = 'filename'
        return df

    def describle(self):
        '''兼容旧接口，请使用 describe()'''
        return self.describe()

    def get_cell(self, cellid):
        '''获取小区实例
            Args：
                cellid：小区id
            Returns:
                对应的小区实例
        '''
        
        if cellid in self._cellids:
            if cellid not in self._cells.keys():
                self._cells[cellid] = Cell(cellid, self)
            return self._cells[cellid]
        else:
            raise ValueError(f'非法 CellId 值 {cellid!r}，此小区不存在')
        
    def get_ue(self, uegid, cellid=None):
        '''获取UE实例
            Args：
                uegid：uegid
                cellid: 可选，小区id
            Returns:
                对应的UE实例
        '''
        
        if cellid:
            if (cellid == self._cell_and_ue_ids['CellId']).any():
                # fix: 使用 get_cell() 而非每次重建 Cell，保留缓存
                return self.get_cell(cellid).get_ue(uegid)
            else:
                raise ValueError(f'非法 cellid={cellid!r} 或 uegid={uegid!r}，此小区或 UE 不存在')
        else:
            if (uegid == self._cell_and_ue_ids['UEGID']).any():
                dllog = self.get_dlschd_logfile(uegid=uegid)
                ullog = self.get_ulschd_logfile(uegid=uegid)
                srslog = self.get_physrs_logfile(uegid=uegid)
                self._ues[uegid] = Ue(ullog, dllog, srslog, None, uegid)
                return self._ues[uegid]
            else:
                raise ValueError(f'非法 uegid={uegid!r}，此 UE 不存在')
        
    def get_cell_and_ue_ids(self):
        '''获取小区实例
            Args：
                None
            Returns:
                Log中的所有CellId,UeId
        '''

        return self._cell_and_ue_ids


    def _get_schd_logfile(self, filetype, cellid=None, uegid=None):
        '''获取Log文件实例'''

        if filetype not in self._logfiles:
            return None
        
        id_filter = {}     
        if cellid is not None:
            id_filter.update({'CellId': [cellid]})     
        if uegid is not None:
            id_filter.update({'UEGID': [uegid]})
         
        return NrFile(filetype, self._directory, self._filenames_of_type(filetype), id_filter=id_filter, time_interval=self._time_interval)

    def get_dlschd_logfile(self, cellid=None, uegid=None):
        '''获取Log文件实例'''
        
        filetype = const.NR_FILE_DLSCHD
        if filetype not in self._logfiles:
            return None         
        id_filter = {}     
        if cellid is not None:
            id_filter.update({'CellId': [cellid]})     
        if uegid is not None:
            id_filter.update({'UEGID': [uegid]})
         
        return NrFile(filetype, self._directory, self._filenames_of_type(filetype), id_filter=id_filter, time_interval=self._time_interval)

    def get_ulschd_logfile(self, cellid=None, uegid=None):
        '''获取Log文件实例'''
        
        filetype = const.NR_FILE_ULSCHD
        if filetype not in self._logfiles:
            return None          
        id_filter = {}     
        if cellid is not None:
            id_filter.update({'CellId': [cellid]})     
        if uegid is not None:
            id_filter.update({'UEGID': [uegid]})
         
        return NrFile(filetype, self._directory, self._filenames_of_type(filetype), id_filter=id_filter, time_interval=self._time_interval)

    def get_physrs_logfile(self, cellid=None, uegid=None):
        '''获取Log文件实例'''
        
        filetype = const.NR_FILE_PHYSRS
        if filetype not in self._logfiles:
            return None          
        id_filter = {}     
        if cellid is not None:
            id_filter.update({'CellId': [cellid]})     
        if uegid is not None:
            id_filter.update({'UEGID': [uegid]})
         
        return NrFile(filetype, self._directory, self._filenames_of_type(filetype), id_filter=id_filter, time_interval=self._time_interval)
        
class NrFile(object):
    '''Log文件接口类'''

    def __init__(self, filetype, directory, files, id_filter=None, time_interval=None):
        '''初始化Log实例,把所有Log按照类型分类

           Args:
               file: 文件名
               filetype: log类型
        '''
        self._files = files
        self._type = filetype
        self._directory = directory
        self._id_filter = id_filter
        self._time_filter = None
        self._size = sum([os.path.getsize(os.path.join(directory, file)) for file in files])
        self._times = [-1, -1]
        self._lines = 0
        self._cellids = set()
        self._uegids = set()
        self._cell_and_ue_ids = pd.DataFrame()
        self._time_interval = time_interval

        cols = ['LocalTime', 'CellId', 'UEGID']
        data = self.get_data_of_cols(cols,format_time=True)
        if len(data.index):
            self._lines = len(data.index)
            self._times[0] = data.iat[0, 0]
            self._times[1] = data.iat[-1, 0]
            self._cellids = set.union(self._cellids, set(data[cols[1]]))
            self._uegids = set.union(self._uegids, set(data[cols[2]]))
            data = data.set_index(cols[0])
            self._cell_and_ue_ids = pd.concat([data[cols[1:]].drop_duplicates(), self._cell_and_ue_ids]).drop_duplicates()

    @property
    def cellids(self):
        '''获取所有小区ID'''
        return self._cellids

    @property
    def uegids(self):
        '''获取所有UEGID'''
        return self._uegids
    
    @property
    def cell_and_ue_ids(self):
        '''获取所有UEGID,CellGid'''
        return self._cell_and_ue_ids

    @property
    def type(self):
        return self._type

    @property
    def files(self):
        return self._files

    @property
    def size(self):
        return self._size

    @property
    def id_filter(self):
        return self._id_filter

    @property
    def lines(self):
        '''获取文件总行数'''
        return self._lines

    @property
    def times(self):
        '''PC时间范围'''
        return tuple(self._times)


    def _format_time(self, data, datestr):
        col = 'LocalTime'
        def __retime(timestr):
            y, m ,d = datestr[:4], datestr[4:6], datestr[6:8]
            h, mi, s, ms = timestr.replace(' ', '').split(":")
            return datetime.datetime(int(y), int(m), int(d), int(h), int(mi), int(s), int(ms)*1000)
        data[col] = data[col].apply(__retime)
        return data

    def gen_of_cols(self, cols=None, val_filter=None, format_time=False):
        '''获取指定列的生成器
            Args：
                cols: 列名列表，如果为None，表示获取全部列
                col_val_filter: 过滤条件，字典格式{'colname': [val1,]}
            Yields:
                生成器格式
        '''

        if format_time:
            assert('LocalTime' in cols)

        filters = {}
        if val_filter:
            filters.update(val_filter)
        if self._id_filter:
            filters.update(self._id_filter)

        filter_cols = None
        if cols is not None:
            filter_cols = list(set.union(set(filters), set(cols)))

        for file in self._files:
            filename = os.path.join(self._directory, file)
            data = pd.read_csv(filename, na_values='-', usecols=filter_cols, encoding='latin-1')
            if format_time:
                datestr = os.path.splitext(file)[0].rsplit('_')[-1]
                self._format_time(data, datestr)

            if 'LocalTime' in cols and self._time_interval is not None:
                data = data[(self._time_interval[0] <= data['LocalTime']) & (data['LocalTime'] <= self._time_interval[1])]

            if not filters:
                yield data
                continue

            mask = data[list(filters.keys())].isin(filters).all(1)
            if cols is not None:
                yield data[mask][cols]
            else:
                yield data[mask]
            
    def get_data_of_cols(self, cols, val_filter=None, format_time=False):
        '''获取指定cols的数据
            Args：
                cols: 列名列表
                col_val_filter: 过滤条件，字典格式{'colname': [val1,]}
            Returns:
                数据，DataFrame格式
        '''
        
        if format_time:
            assert('LocalTime' in cols)
        
        if self._time_interval is not None:
            format_time = True

        filters = {}
        if val_filter:
            filters.update(val_filter)
        if self._id_filter:
            filters.update(self._id_filter)
        
        filter_cols = None
        if cols is not None:
            filter_cols = list(set.union(set(filters), set(cols)))
            if format_time and 'LocalTime' not in cols:
                if isinstance(filter_cols, list):
                    filter_cols.append('LocalTime')
                else:
                    # fix: 括号位置修正，确保 filter_cols 内容被保留
                    filter_cols = list(set((list(filter_cols) if filter_cols else []) + ['LocalTime']))
           
        thread_data = {}
        _lock = threading.Lock()
        def __readcsv(threadid, filename, file_name, na_values, usecols):
            # fix: 使用参数 file_name 而非外层循环变量 name，避免多线程闭包竞态条件
            tdata = pd.read_csv(filename, na_values=na_values, usecols=usecols, encoding='latin-1')
            if format_time:
                datestr = os.path.splitext(file_name)[0].rsplit('_')[-1]
                self._format_time(tdata, datestr)

            if 'LocalTime' in cols and self._time_interval is not None:
                tdata = tdata[(self._time_interval[0] <= tdata['LocalTime']) & (tdata['LocalTime'] <= self._time_interval[1])]

            # fix: 加锁保护并发写入，避免非 CPython 实现或未来版本的竞态风险
            with _lock:
                thread_data[threadid] = tdata
        
        threads = {}
        sorted_files = np.sort(self._files)
        for threadid, name in enumerate(sorted_files):
            filename = os.path.join(self._directory, name)
            thread = threading.Thread(target=__readcsv, args=(threadid, filename, name),
                                      kwargs={'na_values': '-', 'usecols': filter_cols})
            threads.update({threadid: thread})
            thread.start()
        
        # fix: 收集所有 DataFrame 后一次性 concat，避免 O(n²) 性能问题
        parts = []
        for threadid, name in enumerate(sorted_files):
            threads[threadid].join()
            data = thread_data[threadid]
            if not filters:
                parts.append(data)
                continue

            mask = data[list(filters.keys())].isin(filters).all(1)
            if cols is not None:
                parts.append(data[mask][cols])
            else:
                parts.append(data[mask])

        rlt = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        return rlt

    def mean_of_cols(self, cols, time_bin=1, filters=None):
        '''按照时间粒度计算指定列的平均值

            Args:
                cols:待汇总的列名
                time_bin：时间粒度（s)
                filters：滤波条件，字典格式{'列名0'：值， '列名1'：值...}
        '''

        time_col = 'LocalTime'
        if time_col not in cols:
            cols.append(time_col) 
        rlt = self.get_data_of_cols(cols, val_filter=filters, format_time=True)
        rlt = rlt[cols].set_index(rlt[time_col])
        rlt = rlt.resample(str(time_bin)+'S').apply('mean')  
        return rlt

    def sum_of_cols(self, cols, time_bin=1, filters=None):
        '''按照时间粒度计算指定列的总和

            Args:
                cols:待汇总的列名
                time_bin：时间粒度（s)
                filters：滤波条件，字典格式{'列名0'：值， '列名1'：值...}
        '''
        time_col = 'LocalTime'
        if time_col not in cols:
            cols.append(time_col) 
        rlt = self.get_data_of_cols(cols, val_filter=filters, format_time=True)
        rlt = rlt[cols].set_index(time_col)
        rlt = rlt.resample(str(time_bin)+'S').apply('sum') 
        return rlt
        
    def min_of_cols(self, cols, time_bin=1, filters=None):
        '''按照时间粒度计算指定列的最小值

            Args:
                cols:待汇总的列名
                time_bin：时间粒度（s)
                filters：滤波条件，字典格式{'列名0'：值， '列名1'：值...}
                time_col: 聚合的时间列名，默认'AirTime'
        '''
        time_col = 'LocalTime'
        if time_col not in cols:
            cols.append(time_col) 
        rlt = self.get_data_of_cols(cols, val_filter=filters, format_time=True)
        rlt = rlt[cols].set_index(time_col)
        rlt = rlt.resample(str(time_bin)+'S').apply('min') 
        return rlt
        
    def max_of_cols(self, cols, time_bin=1, filters=None):
        '''按照时间粒度计算指定列的最大值

            Args:
                cols:待汇总的列名
                time_bin：时间粒度（s)
                filters：滤波条件，字典格式{'列名0'：值， '列名1'：值...}
                time_col: 聚合的时间列名，默认'AirTime'
        '''
        time_col = 'LocalTime'
        if time_col not in cols:
            cols.append(time_col) 
        rlt = self.get_data_of_cols(cols, val_filter=filters, format_time=True)
        rlt = rlt[cols].set_index(time_col)
        rlt = rlt.resample(str(time_bin)+'S').apply('max') 
        return rlt

    def cnt_of_cols(self, cols, time_bin, filters=None):
        '''按照时间粒度计算指定列的次数

            Args:
                time_bin：时间粒度（s), 0表示不区分时间粒度
                filters：滤波条件，字典格式{'列名0'：值， '列名1'：值...}
        '''
        time_col = 'LocalTime'
        if time_col not in cols:
            cols.append(time_col) 
        rlt = self.get_data_of_cols(cols, val_filter=filters, format_time=True)
        rlt = rlt[cols].set_index(time_col)
        rlt = rlt.resample(str(time_bin)+'S').apply('count') 
        return rlt

    def value_count_of_col(self, col, time_bin=1, ratio=False, filters=None):
        '''按照时间粒度计算指定列的直方图数据

            ratio: 是否计算比例, 默认Ratio为True
            time_bin：时间粒度（s)
            filters：滤波条件，字典格式{'列名0'：值， '列名1'：值...}
        '''
        time_col = 'LocalTime'
        col_name = col[0]
        if time_col not in col:
            col.append(time_col) 
        rlt = self.get_data_of_cols(col, val_filter=filters, format_time=True)
        rlt = rlt.set_index(time_col)
        rlt = rlt[col_name]
        # fix: resample+apply(value_counts) 结果在数据量少时 index 可能不是 MultiIndex，
        # 导致 unstack() 抛出 "index must be a MultiIndex" 错误。
        # 改用 groupby+value_counts+unstack 组合，显式构造宽表，更稳健。
        tb_floor = rlt.index.floor(str(time_bin) + 'S')
        tmp = pd.DataFrame({'_tb': tb_floor, '_val': rlt.values})
        counted = tmp.groupby(['_tb', '_val']).size().unstack(fill_value=0)
        counted.index.name = time_col
        rlt = counted
        if ratio:
            rlt = rlt.apply(lambda x: x/x.sum(), axis=1)
        rlt.columns.name = col_name
        return rlt

    def hist_of_col(self, col, bins=10, density=False, filters=None):
        '''计算指定列的直方图数据

            density: 是否计算概率密度, 默认 False（原参数 normed 已在 matplotlib 3.x 废弃）
            filters：滤波条件，字典格式{'列名0'：值， '列名1'：值...}
        '''
        # fix: 统一接受 str 或 list，避免 set(str) 把列名字符拆散
        if isinstance(col, str):
            col = [col]
        rlt = self.get_data_of_cols(col, val_filter=filters)
        return rlt.hist(bins=bins, density=density)

if __name__ == '__main__' :
    #svlog = NrLog(r"D:\sv\20210324", time_interval=['2021/03/23/ 21:37:00', '2021/03/23/ 21:38:00'])
    svlog = NrLog(r"D:\问题分析\DU3.2\艾灵\redcap下行harqfail")
    #svlog1 = NrLog(r"D:\问题分析\上行200Mbps\srs-640slots")
    #svlog1 = NrLog(r"D:\问题分析\特殊时隙bler\1011", time_interval=['2022/10/11/ 9:58:00', '2022/10/11/ 10:00:00'])
    #cell1 = svlog.get_cell(1)
    #cell._dl.describle_dtx()
    #ue = svlog.get_ue(7)
    #ue15 = svlog.get_ue(15)
    #dl = ue.dl
    #dl.amc()
    ##dl.schdfail_reasons()
    