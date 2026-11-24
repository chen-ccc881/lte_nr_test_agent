# coding=utf-8

# NR调度失败原因
NR_SCHD_FAIL_RSNS = ('cce_input_param', 'cce_not_enough', 'cce_position_collision', 'cce_pwr_limit',
                      'alloc_harqproc_fail', 'alloc_harqid_fail', 'alloc_rb_fail', 'retx_retx_rbnum_different',
                      'gap', 'dmrs_conflict', 'rbest_fail', 'has_schded', 'drx', 'pttsps_schded', 'sps_schded',
                      'retx_coderate_over', 'sr_request_time_over', 'sr_schded', 'err_state', 'schd_ue_num_over',
                      'pre_cnt_over', 'ccch_schd_timeover', 'fbd_schded', 'msg0_num_over', 'preamble_id_err',
                      'msg0_alloc_cce_fail', 'cce_alloc_fail', 'retx_occupy_rb_fail', 'pttgap', 'other')

# NR文件类型
NR_FILE_TYPES = ('CMAC_ulUeTtiInfo', 'CMAC_dlUeTtiInfo', 'PHY_srsTtilnfo')
NR_FILE_ULSCHD = NR_FILE_TYPES[0]
NR_FILE_DLSCHD = NR_FILE_TYPES[1]
NR_FILE_PHYSRS = NR_FILE_TYPES[2]

# ---------------------------------------------------------------------------
# LTE 制式文件类型
# ---------------------------------------------------------------------------
# T2232 导出的 LTE 详情文件命名为 <NR前缀>_Lte_<时间>.csv，例如：
#   CMAC_dlUeTtiInfo_Lte_20260317104100.csv
#   RLC_ulUeTtiInfo_Lte_20260317104100.csv
# 判定规则与 util/fileproc.py 的 is_lte_file（"_Lte" in filename）保持一致。
#
# 注意：LTE 与 NR 的 CSV 列名不同，不可混用同一套分析逻辑，例如：
#   NR  下行 HARQ 失败 : CRCI.u8IsHarqFail
#   LTE 下行 HARQ 失败 : ACK.u8Ack / ACK.b8IsHarqFail，MCS 为 TB.u8Mcs0
# 因此 NrLog 必须把 LTE 文件从 NR 管道中隔离（见 nrlog._filenames_of_type），
# 交由 LTE 专用分析器（svinsightEILteValidator）处理。
# ---------------------------------------------------------------------------
LTE_SUFFIX = 'Lte'
LTE_FILE_TYPES = tuple(t + '_' + LTE_SUFFIX for t in NR_FILE_TYPES)
LTE_FILE_ULSCHD = LTE_FILE_TYPES[0]
LTE_FILE_DLSCHD = LTE_FILE_TYPES[1]
LTE_FILE_PHYSRS = LTE_FILE_TYPES[2]

# LTE 日志前缀（含 NR 管道未覆盖的 PDCP / RLC）
LTE_LOG_PREFIXES = ('CMAC_ulUeTtiInfo', 'CMAC_dlUeTtiInfo', 'PHY_srsTtilnfo',
                    'PDCP_ulUeTtiInfo', 'PDCP_dlUeTtiInfo',
                    'RLC_ulUeTtiInfo', 'RLC_dlUeTtiInfo')


def is_lte_filename(filename):
    '''判断文件名是否为 LTE 制式 EI 文件（<前缀>_Lte_<时间>.csv）。

    按 '_' 分段后精确匹配 'Lte'，避免 'Lte' 恰好出现在前缀子串中造成误判。

    Args:
        filename: 文件名或路径
    Returns:
        bool
    '''
    import os
    stem = os.path.splitext(os.path.basename(filename))[0]
    return LTE_SUFFIX in stem.split('_')

# AirTime/DemTime 字段说明 (2026-03-19 更新，支持半时隙调度)
# -------------------------------------------------------
# 新格式 (32位整数):
#   bit0     : 半时隙标志 (0=前半时隙, 1=后半时隙)
#   bit1~7   : 时隙号 (0-127)
#   bit8~17  : 帧号 (0-1023)
#   bit18~27 : 超帧号 (0-1023)
#   bit28+   : 暂未使用
# 
# 相关字段名:
#   DL_DEMTIME_COL      = 'SCHD.u32DemTime'      # 下行调度解调时间戳
#   DL_ACK_DEMTIME_COL  = 'ACK.u32DemTime'       # 下行 ACK 解调时间戳
#   UL_DEMTIME_COL      = 'CRCI.u32DemTime'      # 上行 CRC 解调时间戳
#   UL_AIRTIME_COL      = 'SCHD.u32AirTime'      # 上行调度空中时间 (如存在)
#
# 解析函数见 airtime_demtime.py 模块:
#   - parse_airtime(value)     : 解析 AirTime/DemTime 各分量
#   - format_airtime(value)    : 格式化为字符串 "SF:F:Sl:H"
#   - airtime_to_absolute_slot(value, slots_per_frame) : 转换为绝对时隙号
# -------------------------------------------------------

# ACK 相关常量
ACK_NACK = 0
ACK_ACK = 1
ACK_DTX = 2
ACK_VALUES = [ACK_NACK, ACK_ACK, ACK_DTX]

# MIMO 层数相关常量
MIMO_LAYERS = [1, 2, 3, 4]

# ---------- DL MIMO / AMC 字段名（CMAC_dlUeTtiInfo）----------
DL_LAYERS_COL        = 'SCHD.u8Layers'        # 下行调度层数
DL_MCS_COL           = 'SCHD.u8Mcs'           # 下行调度 MCS
DL_TBSIZE_COL        = 'SCHD.u32TbSize'        # 下行 TB 大小（字节）
DL_RBNUM_COL         = 'SCHD.u16RbNum'         # 下行调度 RB 数
DL_TXCNT_COL         = 'SCHD.u8TxCnt'          # 下行 HARQ 累计传输次数
DL_DEMTIME_COL       = 'SCHD.u32DemTime'        # 下行解调时间戳
DL_HARQID_COL        = 'SCHD.u8HarqId'         # 下行 HARQ 进程 ID
DL_ACK_INFO_COL      = 'ACK.u8AckInfo'         # 下行 ACK 信息（0=NACK,1=ACK,2=DTX）
DL_ACK_HARQFAIL_COL  = 'ACK.u8IsHarqFail'      # 下行 HARQ 失败标志
DL_ACK_DEMTIME_COL   = 'ACK.u32DemTime'        # 下行 ACK 解调时间戳
DL_ACK_HARQID_COL    = 'ACK.u8HarqId'          # 下行 ACK HARQ 进程 ID
DL_AMC_INNER_SINR_COL = 'AMC.s16InnerSinr'     # 下行内环 SINR（0.1dB）
DL_AMC_DELTA_SINR_COL = 'AMC.s16DeltaSinr'     # 下行 Delta SINR（0.1dB）
DL_SINR_SCALE         = 10.0                    # SINR 原始值 → dB 换算比例

# ---------- DL CSI 字段名（CMAC_dlUeTtiInfo）----------
CSI_RPT_RI_COL       = 'CSI.u8RptRI'           # UE 上报 RI（Rank Indicator）
CSI_RPT_WIDE_CQI_COL = 'CSI.u8RptWideCqi'      # UE 上报宽带 CQI
CSI_RPT_PMI_COL      = 'CSI.u8RptPmi'          # UE 上报 PMI（预编码矩阵索引）
CSI_RPT_SUBCQI_COL   = 'CSI.u8RptSubCqi'       # UE 上报子带 CQI（部分场景有效）
# CQI→MCS/吞吐量的理论参考（3GPP TS 38.214 Table 5.2.2.1-3）
# CQI 0 = 无信号，CQI 1~15 对应调制阶数+码率
CSI_CQI_VALID_RANGE  = (1, 15)                 # 有效 CQI 范围

# ---------- UL MIMO 字段名（CMAC_ulUeTtiInfo）----------
UL_LAYERS_COL        = 'GRANT.u8Layers'        # 上行调度层数（部分版本有此字段）
UL_MCS_COL           = 'GRANT.u8Mcs'           # 上行调度 MCS
UL_TBSIZE_COL        = 'GRANT.u32TbSize'        # 上行 TB 大小（字节）
UL_RBNUM_COL         = 'GRANT.u16RbNum'         # 上行调度 RB 数
UL_HARQID_COL        = 'GRANT.u8HarqId'         # 上行 HARQ 进程 ID
UL_ACK_INFO_COL      = 'CRCI.u8AckInfo'         # 上行 ACK 信息（0=NACK,1=ACK,2=DTX）
UL_ACK_HARQFAIL_COL  = 'CRCI.u8IsHarqFail'      # 上行 HARQ 失败标志
UL_DEMTIME_COL       = 'CRCI.u32DemTime'         # 上行 CRC 解调时间戳
UL_PUSCH_SNR_COL     = 'CRCI.s16PuschSnr'        # 上行 PUSCH 宽带 SNR（0.1dB）
UL_PUSCH_NI_COL      = 'CRCI.s16PuschNI'         # 上行噪声干扰 NI
UL_SRS_SINR_COL      = 'SRS.s16SingleRbSINRRank1'  # 上行 SRS 单 RB SINR（Rank1）

# ---------- MIMO 自适应合理性判断阈值 ----------
# 若实际调度层数长期低于 UE 上报的 RI，说明 MIMO 自适应偏保守
MIMO_LAYER_UNDER_THRESH = 0.5   # 调度层数/RI < 此比例视为层数利用不足
MIMO_BLER_HIGH_THRESH   = 10.0  # BLER > 此值（%）时，当前层数下的 MCS 偏激进
MIMO_SINR_PER_LAYER_MIN = 3.0   # dB，每增加一层所需的最低额外 SINR 增益参考值

# TAC 相关常量
TAC_INVALID_VALUE = 255
TAC_OFFSET = 31

# TA (Timing Advance) 相关常量
# TAC 命令范围：0~63，31 表示无调整；TAC 实际调整量 = TAC - TAC_OFFSET
TA_RPT_SCALE = 4          # rptTA 上报值按 1/4 Tc 为单位，换算为 Ts 需除以 4
TA_ADJUST_NONE = 0        # TAC 转换后为 0 表示未调整
TA_LARGE_THRESH = 8       # 绝对值超过此阈值的 TAC 命令视为较大调整（可配置）
TA_RPT_TA_COL  = 'TA.as16RptTa'          # DL UEtti 中上报 TA 的字段名
TA_TAC_COL     = 'SCHD.u8Tac'            # DL UEtti 中 TAC 命令字段名

# PUCCH 功控相关常量
# TPC 命令索引（3GPP TS 38.213 Table 7.2.1-1）
PUCCH_TPC_TABLE = {0: -1, 1: 0, 2: 1, 3: 3}   # TPC index -> dB 调整量
PUCCH_FMT_ALL   = 255     # 查询所有 format 时的通配符
# PUCCH_PC 字段名（DL UEtti 中）
PUCCH_PC_FORMAT_COL    = 'PUCCH_PC.format'      # PUCCH format（0/1/2/3/4）
PUCCH_PC_TPC_COL       = 'PUCCH_PC.tpc'         # 下发的 TPC 命令索引
PUCCH_PC_RPT_SINR_COL  = 'PUCCH_PC.rptSinr'     # 上报 SINR（原始值，单位 0.1dB）
PUCCH_PC_ADJ_SINR_COL  = 'PUCCH_PC.rptAdjSinr'  # 调整后 SINR（单位 0.1dB）
PUCCH_PC_FILTER_SINR_COL = 'PUCCH_PC.filterSinr' # 滤波后 SINR（单位 0.1dB）
PUCCH_PC_RSRP_COL      = 'PUCCH_PC.rsrp'        # PUCCH RSRP（单位 0.1dBm）
PUCCH_PC_NI_COL        = 'PUCCH_PC.ni'          # 干扰噪声 NI
PUCCH_PC_ANT0_PWR_COL  = 'PUCCH_PC.ant0Pwr'     # 天线 0 功率
PUCCH_PC_ANT1_PWR_COL  = 'PUCCH_PC.ant1Pwr'     # 天线 1 功率
PUCCH_SINR_SCALE       = 10.0   # SINR 原始值换算为 dB 需除以 10
PUCCH_RSRP_SCALE       = 10.0   # RSRP 原始值换算为 dBm 需除以 10
PUCCH_SINR_LOW_THRESH  = -50    # 原始值：-50 → -5dB，低 SINR 告警阈值（可配置）

# 时隙类型
SLOT_TYPE_NORMAL = 0
SLOT_TYPE_SPECIAL = 7

# LCH Group 默认值
DEFAULT_LCH_GRP = 7

# 特殊/广播信道 UEGID 起始值
# UEGID >= SPECIAL_UEGID_MIN 属于 SSB/SIB1/MSG4 等公共信道/广播消息，不属于普通 UE
# 当前值为 7201（已在 pdcp.py / rlc.py 中直接引用，后续可统一改为从此处导入）
SPECIAL_UEGID_MIN = 7201

# ---------- UE 配置变更原因（E_L2UeCfgRsn） ----------
# 来源：CMAC_ulUeTtiInfo / CMAC_dlUeTtiInfo 中的 CFG.u32CfgType 字段
# 每条 UEtti 日志记录的配置变更事件类型
CFG_TYPE_COL = 'CFG.u32CfgType'         # CMAC UEtti 日志中的配置变更原因字段名

CFG_INITCFG           = 0   # UE 初始配置（初始接入，第一次建立）
CFG_RECFG             = 1   # UE 重配置（RRC Reconfiguration）
CFG_REESTABLISH       = 2   # UE 重建立（RRC Reestablishment）← 分析重建立的关键标志
CFG_HO                = 3   # 切换（Handover，目标侧建立）
CFG_BWP_SWITCH_START  = 4   # BWP 切换开始
CFG_BWP_SWITCH_END    = 5   # BWP 切换结束
CFG_DEL               = 6   # UE 删除/释放
CFG_INBOARD_HO        = 7   # 站内切换
CFG_SR_MAXRET_START   = 8   # SR 达到最大重传次数开始
CFG_SR_MAXRET_END     = 9   # SR 达到最大重传次数结束
CFG_INTRA_CELL_HO     = 10  # 小区内切换
CFG_RESUME            = 11  # UE 恢复（RRC Resume，非激活态恢复）
CFG_GAP               = 12  # 测量间隔（Measurement Gap）配置变更
CFG_WAVEFORM_START    = 13  # 波形切换开始
CFG_SCG_ACCESS        = 14  # SCG 接入（双连接场景）
CFG_SCELL             = 15  # SCell 配置变更
CFG_SN_SR_MAXRET      = 16  # SN 侧 SR 达到最大次数
CFG_SN_RESYN          = 17  # SN 侧重同步
CFG_SN_SCELL_RECFG    = 18  # SN 侧 SCell 重配置

# 枚举值 → 名称映射（用于打印/可视化）
CFG_TYPE_NAMES = {
    CFG_INITCFG:          'INITCFG',
    CFG_RECFG:            'RECFG',
    CFG_REESTABLISH:      'REESTABLISH',
    CFG_HO:               'HO',
    CFG_BWP_SWITCH_START: 'BWP_START',
    CFG_BWP_SWITCH_END:   'BWP_END',
    CFG_DEL:              'DEL',
    CFG_INBOARD_HO:       'INBOARD_HO',
    CFG_SR_MAXRET_START:  'SR_MAXRET_START',
    CFG_SR_MAXRET_END:    'SR_MAXRET_END',
    CFG_INTRA_CELL_HO:    'INTRA_CELL_HO',
    CFG_RESUME:           'RESUME',
    CFG_GAP:              'GAP',
    CFG_WAVEFORM_START:   'WAVEFORM_START',
    CFG_SCG_ACCESS:       'SCG_ACCESS',
    CFG_SCELL:            'SCELL',
    CFG_SN_SR_MAXRET:     'SN_SR_MAXRET',
    CFG_SN_RESYN:         'SN_RESYN',
    CFG_SN_SCELL_RECFG:   'SN_SCELL_RECFG',
}

# ---------- 上行调度类型（E_ULSCHD_TYPE） ----------
# 来源：CMAC_ulUeTtiInfo 中的 SCHD.u8SchdType 字段
UL_SCHD_TYPE_COL = 'SCHD.u8SchdType'     # 上行 UEtti 日志中的调度类型字段名

ULSCHD_QOS          = 0    # 正常 QoS 数据调度（业务数据，最常见）
ULSCHD_PRESCHD      = 1    # 预调度（Preschedule）
ULSCHD_FIXED_MCS    = 2    # 固定 MCS 调度
ULSCHD_PG           = 3    # 寻呼（Paging）
ULSCHD_SR           = 4    # SR 触发的调度（UE 发 SR 后分配 grant）
ULSCHD_MSG3         = 5    # 随机接入 MSG3（RACH 过程）← 重建立 Top2 关键
ULSCHD_MSG4         = 6    # 随机接入 MSG4（Contention Resolution）← 重建立 Top2 关键
ULSCHD_SigLUCI      = 7    # 信令 UCI
ULSCHD_BWP_SWITCH   = 8    # BWP 切换
ULSCHD_CG           = 9    # 免调度授权（Configured Grant）
ULSCHD_CGACT        = 10   # CG 激活
ULSCHD_CGDEACT      = 11   # CG 去激活
ULSCHD_INVALID      = 12   # 无效值

# 枚举值 → 名称映射
UL_SCHD_TYPE_NAMES = {
    ULSCHD_QOS:        'QOS',
    ULSCHD_PRESCHD:    'PRESCHD',
    ULSCHD_FIXED_MCS:  'FIXED_MCS',
    ULSCHD_PG:         'PG',
    ULSCHD_SR:         'SR',
    ULSCHD_MSG3:       'MSG3',
    ULSCHD_MSG4:       'MSG4',
    ULSCHD_SigLUCI:    'SigLUCI',
    ULSCHD_BWP_SWITCH: 'BWP_SWITCH',
    ULSCHD_CG:         'CG',
    ULSCHD_CGACT:      'CGACT',
    ULSCHD_CGDEACT:    'CGDEACT',
    ULSCHD_INVALID:    'INVALID',
}

# ---------- 下行调度类型（E_DLSCHD_TYPE） ----------
# 来源：CMAC_dlUeTtiInfo 中的 SCHD.u8SchdType 字段
DL_SCHD_TYPE_COL = 'SCHD.u8SchdType'     # 下行 UEtti 日志中的调度类型字段名（与上行同名）

DLSCHD_QOS      = 0    # 正常 QoS 数据调度（业务数据，最常见）
DLSCHD_MSG2     = 1    # 随机接入 MSG2（RAR，Random Access Response）← 重建立 Top2 关键
DLSCHD_MSG4     = 2    # 随机接入 MSG4（Contention Resolution）← 重建立 Top2 关键
DLSCHD_PRESCHD  = 3    # 预调度（Preschedule）
DLSCHD_SPSSCHD  = 4    # SPS 半持续调度（Semi-Persistent Scheduling）
DLSCHD_SIB1     = 5    # SIB1 广播消息调度
DLSCHD_OSI      = 6    # OSI（Other System Information）广播调度
DLSCHD_PAGING   = 7    # 寻呼（Paging）
DLSCHD_SPSACT   = 8    # SPS 激活
DLSCHD_SPSDEACT = 9    # SPS 去激活
DLSCHD_INVALID  = 10   # 无效值

# 枚举值 → 名称映射
DL_SCHD_TYPE_NAMES = {
    DLSCHD_QOS:     'QOS',
    DLSCHD_MSG2:    'MSG2',
    DLSCHD_MSG4:    'MSG4',
    DLSCHD_PRESCHD: 'PRESCHD',
    DLSCHD_SPSSCHD: 'SPSSCHD',
    DLSCHD_SIB1:    'SIB1',
    DLSCHD_OSI:     'OSI',
    DLSCHD_PAGING:  'PAGING',
    DLSCHD_SPSACT:  'SPSACT',
    DLSCHD_SPSDEACT:'SPSDEACT',
    DLSCHD_INVALID: 'INVALID',
}

# ---------- Slot 间隔抖动（SLOT_INT） ----------
# 来源：CMAC_ulUeTtiInfo / CMAC_dlUeTtiInfo 中的 SLOT_INT 字段组
# 含义：连续两个 slot 的时间间隔异常（调度线程抖动/CPU 抢占）
# 影响：间隔过大 → PHY 处理延迟 → HARQ 超时 → BLER 升高 → RLCMax / SRMax
SLOT_INT_INTERVAL_COL = 'SLOT_INT.u16Interval'  # 两个连续 slot 间隔，单位 us
SLOT_INT_CYCLES_COL   = 'SLOT_INT.u16Cycles'    # 连续两次读取 slot 的间隔，单位 1/25MHz（= 40ns）

# 告警阈值（SCS=30kHz 时正常 slot 间隔约 500us）
SLOT_INT_WARN_US      = 600    # us，间隔超过此值视为抖动（正常值的 1.2x）
SLOT_INT_SEVERE_US    = 1000   # us，间隔超过此值视为严重抖动（≥ 2 个 slot 延误）
SLOT_INT_CYCLES_SCALE = 25e6   # Hz，Cycles 单位换算系数（÷ 25MHz = 秒）

# ---------- Slot 不连续（SLOT_DISC） ----------
# 来源：CMAC_ulUeTtiInfo / CMAC_dlUeTtiInfo 中的 SLOT_DISC 字段组
# 含义：相邻两次调度之间发生了 slot 跳跃（丢失了一个或多个 slot 的调度机会）
# 影响：丢 slot → PHY/MAC 层时序错乱 → BLER 升高 / HARQ 失败 / RLCMax / SRMax / AMC 内环 CSI 失效
SLOT_DISC_PRE_COL      = 'SLOT_DISC.u32PreSysTime'  # 前一个系统空口时间（slot 序号或时间戳）
SLOT_DISC_CUR_COL      = 'SLOT_DISC.u32CurSysTime'  # 后一个系统空口时间
SLOT_DISC_INTERVAL_COL = 'SLOT_DISC.u32Interval'    # 两个不连续 slot 的间隔（单位同系统时间）

# 告警阈值（SCS=30kHz 时正常相邻 slot 差值 = 1；差值 > 1 即为不连续）
SLOT_DISC_WARN_GAP    = 2     # 间隔 > 此值视为明显不连续（跳过 1 个以上 slot）
SLOT_DISC_SEVERE_GAP  = 10   # 间隔 > 此值视为严重不连续（跳过 9 个以上 slot，约 4.5ms）
