# SVInsight

NR (5G) 调度日志分析库，提供小区级和 UE 级的调度性能洞察，支持 EI 二进制日志自动转换。

## 功能概览

| 模块 | 说明 |
|------|------|
| `NrLog` | 日志加载入口，自动识别文件类型，支持时间范围过滤，自动转换 `.ei` 文件 |
| `Cell` | 小区实例，提供 DL/UL 小区级调度分析 |
| `Ue` | UE 实例，提供 DL/UL/SRS 终端级调度分析 |
| `util/ei2csv` | EI 二进制文件转 CSV（基于 `EI_FileProc.dll`） |

---

## 日志文件类型

svinsight 识别以下 3 类 CSV 日志（按文件名前缀匹配）：

| 常量 | 文件名前缀 | 描述 |
|------|-----------|------|
| `NR_FILE_DLSCHD` | `CMAC_dlUeTtiInfo` | 下行 UE TTI 调度日志 |
| `NR_FILE_ULSCHD` | `CMAC_ulUeTtiInfo` | 上行 UE TTI 调度日志 |
| `NR_FILE_PHYSRS` | `PHY_srsTtilnfo` | 物理层 SRS 测量日志 |

文件命名格式：`<前缀>_<YYYYMMDDHHmmss>.csv`

若目录中存在 `.ei` 文件（且无对应 `.csv`），`NrLog` 在加载时会自动调用 `EI_FileProc.dll` 完成转换。

---

## 快速开始

```python
import sys
sys.path.insert(0, r'D:\code\svinsight')
from nrlog import NrLog

# 加载日志目录
nrlog = NrLog(r"D:\logs")

# 查看概要信息
print(nrlog.describle())           # DataFrame: 各文件类型的大小、行数、时间范围
print(sorted(nrlog._cellids))      # 可用的 Cell ID 列表

# 获取小区实例
cell = nrlog.get_cell(1)
print(cell.describle())            # TotUeNum, dlschd_log_lines, ulschd_log_lines

# 获取 UE 实例
ue = nrlog.get_ue(7)
```

### 时间范围过滤

```python
nrlog = NrLog(r"D:\logs", time_interval=["2024/01/01 10:00:00", "2024/01/01 11:00:00"])
```

---

## API 参考

### NrLog

```python
nrlog = NrLog(directory, time_interval=None)

nrlog.directory                      # 日志目录路径
nrlog.describle()                    # DataFrame: 各类型文件信息
nrlog._cellids                       # set: 所有可用 Cell ID
nrlog.get_cell_and_ue_ids()          # DataFrame: CellId + UEGID 映射表
nrlog.get_cell(cellid)               # → Cell 实例
nrlog.get_ue(uegid, cellid=None)     # → Ue 实例（cellid 可选）
nrlog.get_dlschd_logfile(cellid, uegid)
nrlog.get_ulschd_logfile(cellid, uegid)
nrlog.get_physrs_logfile(cellid, uegid)
```

### Cell

```python
cell = nrlog.get_cell(1)

cell.cellid                          # Cell ID
cell.dl                              # DlSchdCell 实例（无 DL 日志时为 None）
cell.ul                              # UlSchdCell 实例（无 UL 日志时为 None）
cell.describle()                     # Series: TotUeNum, dlschd_log_lines, ulschd_log_lines
cell.show_ue_livetime(time_bin=1)    # UE 活跃度甘特图（时间 × UEGID，值=调度次数）
cell.get_ue(uegid)                   # → 该小区内指定 UE 的 Ue 实例
cell._uegids                         # set: 本小区所有 UEGID
```

### DlSchdCell（cell.dl）— 下行小区级

```python
cell.dl.bler_of_slot(time_bin=1)         # DataFrame[time, slot] → BLER%（NACK+DTX）/total×100
cell.dl.mcs_of_slot()                    # Series[slot] → 各 slot 平均 MCS
cell.dl.throuput(time_bin=1)             # Series[time] → TbSize 之和（吞吐量）
cell.dl.schd_ue_cnt(time_bin=1)          # Series[time] → 调度 UE 数
cell.dl.schd_rbnum(time_bin=1)           # Series[time] → 分配 RB 数之和
cell.dl.harq_txcnt()                     # DataFrame[harq_tx_cnt, slot_type] → HARQ 传输次数分布%
cell.dl.harqfail_cnt(time_bin=1)         # Series[time] → HARQ 失败次数
cell.dl.dtx_cnt(time_bin=1)              # Series[time] → DTX 次数
cell.dl.selfmaintain_cnt(time_bin=1)     # Series[time] → 自维护次数
cell.dl.mimo_layers(time_bin=1, ratio=True)  # DataFrame[time, layers(1-4)] → MIMO 层数分布
cell.dl.rpt_csi(ri=1, time_bin=1)        # DataFrame[time] → CSI 上报次数（RI + WideCQI）
cell.dl.amc(layer=1, time_bin=1)         # DataFrame[time] → AMC 统计（InnerSinr/DeltaSinr/SchdMcs）
cell.dl.schdfail_reasons(time_bin=1)     # DataFrame[time, fail_reason] → 调度失败原因计数
cell.dl.describle_dtx()                  # DTX slot UE 组合散点图 + DataFrame
```

### UlSchdCell（cell.ul）— 上行小区级

```python
cell.ul.bler_of_slot(time_bin=1)         # DataFrame[time, slot] → UL BLER%
cell.ul.mcs_of_slot(time_bin=None)       # Series[slot] 或 DataFrame[time,slot] → 平均 MCS
cell.ul.throuput(time_bin=1)             # Series[time] → TB.u16TbSize 之和
cell.ul.schd_uecnt(time_bin=1)           # Series[time] → 调度 UE 数
cell.ul.schd_rbnum(time_bin=1)           # Series[time] → 平均 RB 数
cell.ul.schd_mcs(time_bin=1)             # Series[time] → 平均 MCS
cell.ul.harqfail_cnt(time_bin=1)         # Series[time] → HARQ 失败次数
cell.ul.pathloss(time_bin=1)             # Series[time] → 平均路损（PHR.u16PathLoss）
cell.ul.singlerbsinr(time_bin=1)         # Series[time] → 单 RB SINR 均值
cell.ul.show_amc(time_bin=1)             # DataFrame[time] → min SingleRbSINR, min RptSinr
cell.ul.rpt_minbsr(lchgrp=7, time_bin=1) # Series[time] → 最小 BSR
cell.ul.rpt_maxbsr(lchgrp=7, time_bin=1) # Series[time] → 最大 BSR
cell.ul.show_rpt_bsr(lchgrp=3)          # 绘制 BSR 趋势图
cell.ul.sinr_of_slot(time_bin=1)         # Series[time] → PUSCH SINR 均值
cell.ul.ni_of_slot(time_bin=None)        # Series[slot 或 time] → 最大 NI
cell.ul.schdfail_reasons()               # Series[Fail_Rsn] → 调度失败原因计数（命名索引）
cell.ul.find_selfmaintain()              # DataFrame：自维护事件列表
cell.ul.find_harqfail()                  # DataFrame：HARQ 失败事件列表
```

### Ue

```python
ue = nrlog.get_ue(7)          # 跨小区加载
ue = nrlog.get_ue(7, cellid=1) # 限定小区加载

ue.uegid                       # UE Group ID
ue.dl                          # DlSchdUe 实例（无数据时为 None）
ue.ul                          # UlSchdUe 实例（无数据时为 None）
ue.srs                         # SrsUe 实例（无数据时为 None）
ue.show_livetime()             # 绘制 UE 跨小区活跃时间线（无返回值）
```

### DlSchdUe（ue.dl）— 下行 UE 级

继承 `DlSchdCell` 所有方法，另有 UE 专属方法：

```python
ue.dl.show_ta(time_bin=1)               # DataFrame[time, TAC, RptTa] → TA 变化趋势
ue.dl.bsr()                             # DataFrame → 调度 BSR vs RLC 上报 BSR 对比图
ue.dl.pucch_pc(fmt=255)                 # DataFrame → PUCCH 功控统计（RptAdjSinr/ni/ant0Pwr）
ue.dl.pucch_sinr_blow(fmt, thresh=-5, time_bin=1)  # Series[time] → PUCCH SINR 跌破阈值次数
```

### UlSchdUe（ue.ul）— 上行 UE 级

与 `UlSchdCell` 接口一致。

### SrsUe（ue.srs）— SRS 测量

```python
ue.srs.get_ta()   # DataFrame indexed by [AirTime, Param.Rnti, Param.ccIndex]
                  # cols: [Param.SrsTaValue, Param.SinrAnt[0]]（仅 SinrAnt[0] > 0 的行）
```

---

## 调度失败原因（const.NR_SCHD_FAIL_RSNS）

共 30 种原因，按位掩码表示：

```
cce_input_param, cce_not_enough, cce_position_collision, cce_pwr_limit,
alloc_harqproc_fail, alloc_harqid_fail, alloc_rb_fail, retx_retx_rbnum_different,
gap, dmrs_conflict, rbest_fail, has_schded, drx, pttsps_schded, sps_schded,
retx_coderate_over, sr_request_time_over, sr_schded, err_state, schd_ue_num_over,
pre_cnt_over, ccch_schd_timeover, fbd_schded, msg0_num_over, preamble_id_err,
msg0_alloc_cce_fail, cce_alloc_fail, retx_occupy_rb_fail, pttgap, other
```

---

## UE 配置变更原因（const.CFG_TYPE_NAMES）

字段：`CFG.u32CfgType`（CMAC_ulUeTtiInfo / CMAC_dlUeTtiInfo）

| 值 | 常量 | 含义 |
|----|------|------|
| 0 | `CFG_INITCFG` | UE 初始配置（首次接入） |
| 1 | `CFG_RECFG` | UE 重配置（RRC Reconfiguration） |
| **2** | **`CFG_REESTABLISH`** | **UE 重建立（RRC Reestablishment）← 重建立分析关键** |
| 3 | `CFG_HO` | 切换（Handover） |
| 4 | `CFG_BWP_SWITCH_START` | BWP 切换开始 |
| 5 | `CFG_BWP_SWITCH_END` | BWP 切换结束 |
| **6** | **`CFG_DEL`** | **UE 删除/释放 ← 关联旧 UEGID 的关键** |
| 7 | `CFG_INBOARD_HO` | 站内切换 |
| 8 | `CFG_SR_MAXRET_START` | SR 达到最大重传次数开始 |
| 9 | `CFG_SR_MAXRET_END` | SR 达到最大重传次数结束 |
| 10 | `CFG_INTRA_CELL_HO` | 小区内切换 |
| 11 | `CFG_RESUME` | UE 恢复（RRC Resume，非激活态） |
| 12 | `CFG_GAP` | 测量间隔配置变更 |
| 13 | `CFG_WAVEFORM_START` | 波形切换开始 |
| 14 | `CFG_SCG_ACCESS` | SCG 接入（双连接） |
| 15 | `CFG_SCELL` | SCell 配置变更 |
| 16 | `CFG_SN_SR_MAXRET` | SN 侧 SR 达到最大次数 |
| 17 | `CFG_SN_RESYN` | SN 侧重同步 |
| 18 | `CFG_SN_SCELL_RECFG` | SN 侧 SCell 重配置 |

---

## 调度类型枚举（const.UL/DL_SCHD_TYPE_NAMES）

### 上行调度类型（`SCHD.u8SchdType`，CMAC_ulUeTtiInfo）

| 值 | 常量 | 含义 |
|----|------|------|
| 0 | `ULSCHD_QOS` | 正常 QoS 数据调度 |
| 1 | `ULSCHD_PRESCHD` | 预调度 |
| 2 | `ULSCHD_FIXED_MCS` | 固定 MCS 调度 |
| 3 | `ULSCHD_PG` | 寻呼 |
| 4 | `ULSCHD_SR` | SR 触发的调度 |
| **5** | **`ULSCHD_MSG3`** | **RACH MSG3（重建立过程关键）** |
| **6** | **`ULSCHD_MSG4`** | **RACH MSG4 Contention Resolution** |
| 7 | `ULSCHD_SigLUCI` | 信令 UCI |
| 8 | `ULSCHD_BWP_SWITCH` | BWP 切换 |
| 9 | `ULSCHD_CG` | 免调度授权 |
| 10 | `ULSCHD_CGACT` | CG 激活 |
| 11 | `ULSCHD_CGDEACT` | CG 去激活 |
| 12 | `ULSCHD_INVALID` | 无效值 |

### 下行调度类型（`SCHD.u8SchdType`，CMAC_dlUeTtiInfo）

| 值 | 常量 | 含义 |
|----|------|------|
| 0 | `DLSCHD_QOS` | 正常 QoS 数据调度 |
| **1** | **`DLSCHD_MSG2`** | **RACH MSG2（RAR，重建立过程关键）** |
| **2** | **`DLSCHD_MSG4`** | **RACH MSG4 Contention Resolution** |
| 3 | `DLSCHD_PRESCHD` | 预调度 |
| 4 | `DLSCHD_SPSSCHD` | SPS 半持续调度 |
| 5 | `DLSCHD_SIB1` | SIB1 广播 |
| 6 | `DLSCHD_OSI` | OSI 广播 |
| 7 | `DLSCHD_PAGING` | 寻呼 |
| 8 | `DLSCHD_SPSACT` | SPS 激活 |
| 9 | `DLSCHD_SPSDEACT` | SPS 去激活 |
| 10 | `DLSCHD_INVALID` | 无效值 |

---

## Slot 异常字段（const.SLOT_INT / SLOT_DISC）

### SLOT_INT — Slot 间隔抖动

字段：`SLOT_INT.u16Interval`（单位 us）、`SLOT_INT.u16Cycles`（单位 1/25MHz = 40ns）

| 常量 | 值 | 说明 |
|------|----|------|
| `SLOT_INT_WARN_US` | 600 us | 抖动告警阈值（正常 slot 间隔约 500us） |
| `SLOT_INT_SEVERE_US` | 1000 us | 严重抖动阈值（≥ 2 个 slot 延误） |

影响链：调度线程抖动 → PHY 处理延迟 → HARQ 超时 → BLER↑ → RLCMax / SRMax

### SLOT_DISC — Slot 不连续

字段：`SLOT_DISC.u32PreSysTime`、`SLOT_DISC.u32CurSysTime`、`SLOT_DISC.u32Interval`

| 常量 | 值 | 说明 |
|------|----|------|
| `SLOT_DISC_WARN_GAP` | 2 | 明显不连续阈值（跳过 1 个以上 slot） |
| `SLOT_DISC_SEVERE_GAP` | 10 | 严重不连续阈值（跳过 9+ slot，约 4.5ms） |

影响链：丢 slot → PHY/MAC 时序错乱 → BLER↑ / HARQ 失败 / RLCMax / SRMax / AMC 内环 CSI 失效

---

## WorkBuddy Skills

本项目提供 10 个 WorkBuddy (CodeBuddy) AI 助手 skill，采用**三层合并架构**设计，位于 `skills/` 目录。安装后可让 AI 直接理解并执行 NR 调度分析任务。

### 三层架构说明

为了平衡**可维护性**和**用户体验**，Skills 采用分层设计：

```
┌─────────────────────────────────────────────┐
│ 主平台 Skill（svinsight）                    │
│ ├─ 统一知识库（UEGID、常量、规范）           │
│ └─ 快速导航表                              │
└─────────────────────────────────────────────┘
              ↓
    ┌─────────┼─────────┐
    ↓         ↓         ↓
【问题诊断层】【协议层分析】【统计与工具层】
(4 个)     (2 个)    (4 个)
```

### 主平台 Skill

| Skill | 目录 | 说明 |
|-------|------|------|
| **svinsight** | `skills/svinsight/` | 统一入口；包含快速导航表、层级结构、共享知识库（UEGID规则、常量、时间对齐、RLC 窗口等）；所有其他 skill 的基础依赖 |

### 问题诊断层（故障排查）

| Skill | 目录 | 触发场景 | 相关层 |
|-------|------|---------|-------|
| **sr-max** | `skills/sr-max/` | SRMax 问题 — UE 连续发 SR 但无法获得上行 grant | 协议层、统计层 |
| **dl-rlcmax** | `skills/dl-rlcmax/` | 下行 RLCMax — 下行 RLC 达到最大重传次数，触发 RLF | 协议层 |
| **ue-reestablish** | `skills/ue-reestablish/` | UE 重建立 — RLF 根本原因诊断（4 大根因） | 诊断层、协议层 |
| **ul-scheduling** | `skills/ul-scheduling/` | 上行调度异常 — HARQ 失败、BSR 饥饿、PHR 耗尽 | 诊断层 |

### 协议层分析（深度理解）

| Skill | 目录 | 说明 |
|-------|------|------|
| **pdcp-analysis** | `skills/pdcp-analysis/` | PDCP 层分析：协议栈时延、吞吐量、丢包、TCP 重传、ICMP RTT、端口流量 |
| **rlc-analysis** | `skills/rlc-analysis/` | RLC 层分析：AM 窗口深度、状态报文、重传统计、处理时延 |

### 统计与工具层（汇总观察）

| Skill | 目录 | 说明 |
|-------|------|------|
| **cell-schedule** | `skills/cell-schedule/` | 小区级调度统计：下行/上行 BLER、MCS、吞吐量、MIMO、AMC、HARQ、调度失败 |
| **ue-schedule** | `skills/ue-schedule/` | 单 UE 调度统计：针对某个 UEGID 的性能分析（BLER、MCS、SRS、TA、PUCCH 等） |
| **ei-converter** | `skills/ei-converter/` | EI 日志工具：`.ei` 二进制文件转 CSV 格式，是分析流程的前置工具 |
| **adaptive-analysis** | `skills/adaptive-analysis/` | 自适应分析：探索性分析、问题模式库、知识积累（实验性功能） |

### 典型分析流程

```
EI 文件  →  [ei-converter]  →  CSV 文件
                                   ↓
                          [cell-schedule]  →  定位问题小区
                                   ↓
                           [ue-schedule]  →  定位问题终端
                                   ↓
               [pdcp-analysis / rlc-analysis]  →  协议栈时延分析
                                   ↓
     [ue-reestablish / dl-rlcmax / sr-max / ul-scheduling]  →  专项根因定位
```

### 使用建议

- **遇到故障** → 先用**问题诊断层** skills 快速定位
- **需要细化原因** → 调用**协议层分析** skills 深度理解
- **观察汇总数据** → 用**统计与工具层** skills 获取全局视图
- **不确定选择** → 先加载 `svinsight` skill，查看导航表一眼定位

---

### 安装 Skills 到 WorkBuddy / CodeBuddy

Skills 本质是一个包含 `SKILL.md` 的文件夹，安装方式有两种：

#### 方式一：手动复制（推荐）

将 skill 文件夹直接复制到 WorkBuddy 的 skills 目录：

```
# Windows
%USERPROFILE%\.workbuddy\skills\
```

以安装 `cell-schedule` 为例：

```cmd
xcopy /E /I "D:\code\svinsight\skills\svinsight"      "%USERPROFILE%\.workbuddy\skills\svinsight"
xcopy /E /I "D:\code\svinsight\skills\ei-converter"   "%USERPROFILE%\.workbuddy\skills\ei-converter"
xcopy /E /I "D:\code\svinsight\skills\cell-schedule"  "%USERPROFILE%\.workbuddy\skills\cell-schedule"
xcopy /E /I "D:\code\svinsight\skills\ue-schedule"    "%USERPROFILE%\.workbuddy\skills\ue-schedule"
xcopy /E /I "D:\code\svinsight\skills\pdcp-analysis"  "%USERPROFILE%\.workbuddy\skills\pdcp-analysis"
xcopy /E /I "D:\code\svinsight\skills\rlc-analysis"   "%USERPROFILE%\.workbuddy\skills\rlc-analysis"
xcopy /E /I "D:\code\svinsight\skills\ue-reestablish" "%USERPROFILE%\.workbuddy\skills\ue-reestablish"
xcopy /E /I "D:\code\svinsight\skills\dl-rlcmax"      "%USERPROFILE%\.workbuddy\skills\dl-rlcmax"
xcopy /E /I "D:\code\svinsight\skills\sr-max"         "%USERPROFILE%\.workbuddy\skills\sr-max"
xcopy /E /I "D:\code\svinsight\skills\ul-scheduling"  "%USERPROFILE%\.workbuddy\skills\ul-scheduling"
```

安装完成后**重启 WorkBuddy / CodeBuddy** 即可生效，无需额外配置。

#### 方式二：npx skills CLI

若已安装 Node.js，可使用 skills CLI 从远程安装：

```bash
npx skills add <owner/repo@skill-name> -g -y
```

#### 验证安装

打开 WorkBuddy，在对话框输入：

```
分析 D:/logs 目录下小区1的调度情况
```

若 AI 自动加载 `cell-schedule` skill 并开始执行分析，则安装成功。

---

### 环境变量配置（可选）

skill 脚本优先通过**相对路径**自动定位 svinsight 根目录（`skills/<name>/scripts/` 上三级），
**无需任何配置即可开箱使用**。

仅当 skill 被复制到 svinsight repo **之外**的目录（如 `%USERPROFILE%\.workbuddy\skills\`）时，
才需要通过以下方式之一指定路径：

```cmd
:: 方式一：环境变量（推荐写入系统环境变量）
set SVINSIGHT_ROOT=D:\code\svinsight

:: 方式二：在 skill 目录附近放一个 svinsight.cfg，内容为：
root=D:\code\svinsight
```

### Skills 同步

本项目提供 skills 同步脚本。**每次修改任何 skill 后**，必须执行以下操作将更改同步到仓库：

#### 同步步骤

```bash
# 1. 执行同步脚本（将 ~/.workbuddy/skills/ 全量复制到本地仓库）
cd D:\code\svinsight
python sync_all_skills.py

# 2. 提交到 Gitee
git add -A
git commit -m "refactor(skills): 描述改动内容"
git push origin main
```

#### 相关脚本

| 脚本 | 功能 |
|------|------|
| `sync_all_skills.py` | 全量同步 skills（C:\Users\dell\.workbuddy\skills\ → D:\code\svinsight\skills\） |
| `sync_rules.py` | 同步规则文件（docs/ ↔ .workbuddy/rules/） |

#### 涉及的 Skills（全量列表）

svinsight, cell-schedule, ue-schedule, pdcp-analysis, rlc-analysis, sr-max, ue-reestablish, dl-rlcmax, ul-scheduling, ei-converter, adaptive-analysis, find-skills

---

### 分析实例：定位小区高 BLER 问题

> **场景**：用户发现某时段网络质量下降，需要排查 `D:\logs\20240315` 目录中的 NR 调度日志。

**Step 1 — 告诉 WorkBuddy 你的问题**

```
帮我分析 D:\logs\20240315 目录，看看哪个小区的下行BLER比较高
```

WorkBuddy 自动触发 `cell-schedule` skill，加载日志，输出各小区概览，
例如发现 **Cell 3** 的 DL BLER 在 10:20~10:35 出现明显抬升。

**Step 2 — 深入分析问题小区**

```
详细分析小区3在10:20到10:35的BLER，同时看看MCS和AMC情况
```

WorkBuddy 执行：

```cmd
set SVINSIGHT_ROOT=D:\code\svinsight
py "...\.workbuddy\skills\cell-schedule\scripts\analyze_cell.py" ^
    "D:\logs\20240315" 3 ^
    --time-interval "2024/03/15 10:20:00" "2024/03/15 10:35:00" ^
    --bler --mcs
```

同时在后台运行：

```python
from nrlog import NrLog
nrlog = NrLog(r"D:\logs\20240315",
              time_interval=["2024/03/15 10:20:00", "2024/03/15 10:35:00"])
cell = nrlog.get_cell(3)
amc  = cell.dl.amc(layer=1, time_bin=1)   # InnerSinr 持续偏低 → 信道质量差
mimo = cell.dl.mimo_layers(time_bin=1)    # 单流比例升高 → 信道退化
harq = cell.dl.harqfail_cnt(time_bin=1)  # HARQ 失败次数同步上升
```

WorkBuddy 输出分析结论：
- BLER 抬升时段 InnerSINR 下降 ~5dB
- MIMO 层数从 2→4 层退化到主要 1 层
- 建议检查 UE 侧信道质量，进一步定位具体终端

**Step 3 — 定位问题终端**

```
小区3在那个时段有哪些UE，帮我看看BLER最高的那个UE的详细情况
```

WorkBuddy 切换到 `ue-schedule` skill：

```python
cell = nrlog.get_cell(3)
# 查看 UE 活跃情况
cell.show_ue_livetime(time_bin=1)
# 找到 BLER 最高的 UE，例如 UEGID=42
ue = nrlog.get_ue(42, cellid=3)
ue.dl.bler_of_slot(time_bin=1)      # 确认 BLER 分布
ue.dl.amc(layer=1, time_bin=1)      # AMC 内环 SINR 变化
ue.dl.pucch_pc(fmt=255)             # PUCCH 功控是否异常
ue.srs.get_ta()                     # SRS TA 值是否漂移
```

WorkBuddy 生成诊断报告：UEGID 42 在问题时段 PUCCH SINR 持续低于 -5dB，
建议检查该终端的上行功控配置。

---

## 依赖

```
pandas
numpy
matplotlib
```

EI 转换额外需要：
- `util/EI_FileProc.dll`（64-bit Windows DLL）

---

## 目录结构

```
svinsight/
├── nrlog.py              # 入口类 NrLog：日志加载、分类、Cell/Ue 管理
├── cell.py               # Cell 类：小区级 DL/UL 调度分析
├── ue.py                 # Ue 类：UE 级 DL/UL/SRS 分析
├── dlschd.py             # DlSchdCell / DlSchdUe：下行调度分析
├── ulschd.py             # UlSchdCell / UlSchdUe：上行调度分析
├── srs.py                # SrsUe：SRS 测量分析
├── const.py              # 全局常量（文件类型、调度失败原因、枚举、字段名等）
├── util/
│   ├── ei2csv.py         # EI → CSV 转换核心逻辑（调用 DLL）
│   ├── EI_FileProc.dll   # EI 解析 DLL（Windows 64-bit）
│   ├── error_handling.py # 公共异常处理与工具函数
│   └── md2pdf.py         # Markdown → PDF 转换工具（离线文档导出）
├── skills/               # WorkBuddy 技能包（开发维护主目录）
│   ├── svinsight/        # 平台基础知识 skill（其他 skill 的依赖）
│   ├── cell-schedule/    # 小区调度分析 skill
│   ├── ue-schedule/      # UE 调度分析 skill
│   ├── ei-converter/     # EI 日志转换 skill
│   ├── pdcp-analysis/    # PDCP 层分析 skill
│   ├── rlc-analysis/     # RLC 层分析 skill
│   ├── ue-reestablish/   # UE 重建立分析 skill
│   ├── dl-rlcmax/        # 下行 RLCMax 分析 skill
│   ├── sr-max/           # SR 达到最大次数分析 skill
│   └── ul-scheduling/    # 上行调度问题分析 skill
├── docs/                 # 项目文档
│   ├── code_fix_summary.md    # 代码修复记录
│   ├── nr_log_background.md   # NR 日志背景知识（AI 分析上下文）
│   └── pdf/                 # 各文档的 PDF 导出版本
├── sync_rules.py         # 规则文件同步脚本
└── .workbuddy/
    └── rules/
        └── nr_log_background.md  # WorkBuddy 自动加载的规则（与 docs/ 同步）
```

**说明**：
- `skills/`：WorkBuddy 技能包的 Git 仓库备份（版本控制）
- `docs/nr_log_background.md`：NR 日志背景知识文档，方便查阅
- `.workbuddy/rules/nr_log_background.md`：WorkBuddy 自动加载的规则，为 AI 助手提供上下文
- `sync_rules.py`：保持 `docs/` 和 `.workbuddy/rules/` 同步的脚本
