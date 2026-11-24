"""问题定位 Agent：汇总多源解析事件 → 关联 → 根因推理 → 解决方案建议。

推理分两层：
  1) 规则层（默认，离线可用）：基于异常组合映射到疑似模块与根因假设
  2) LLM 层（可选）：把结构化事件交给大模型做归纳，需 provider != mock
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List

from env_config import EnvConfig
from llm.gateway import LLMGateway
from models.schema import AnalysisResult, DiagnosisResult, ParsedEvent

# 异常类别 → 疑似问题模块 + 根因假设 + 建议（规则知识库，可持续补充）
RULE_KB: Dict[str, Dict[str, Any]] = {
    "调度异常": {
        "modules": ["MAC层调度器", "资源分配模块"],
        "cause": "调度失败比例超阈值，可能因 CCE 资源不足/RB 分配冲突/HARQ 进程耗尽",
        "advice": [
            "检查 SCHD_FAIL_RSN.u32UeSchdFailRsn 具体失败码（cce_not_enough / alloc_harqproc_fail 等）",
            "核对小区 CCE/RB 资源配置与激活 UE 数是否匹配",
            "检查 PDCCH 聚合等级与 CCE 位置碰撞配置",
        ],
    },
    "调度失败": {
        "modules": ["MAC层调度器"],
        "cause": "调度失败比例超阈值",
        "advice": ["检查调度失败原因字段", "核对无线资源与负载"],
    },
    "HARQ异常": {
        "modules": ["HARQ模块", "物理层"],
        "cause": "HARQ 失败率偏高，可能与空口质量、MCS 过高或定时关系异常有关",
        "advice": ["检查 CQI/SINR 与 MCS 是否匹配", "核查 HARQ 定时与 ACK/NACK 检测", "降低初始 MCS 观察"],
    },
    "链路质量": {
        "modules": ["物理层", "信道估计"],
        "cause": "BLER 偏高，空口链路质量差",
        "advice": ["检查 RSRP/SINR/CQI", "核查干扰与邻区配置", "检查 AMC 内环调整"],
    },
    "异常信令": {
        "modules": ["RRC层", "移动性管理"],
        "cause": "出现 RLF/重建/切换失败等异常信令，流程未正常完成",
        "advice": ["按时间轴还原异常信令前后流程", "检查切换门限与邻区配置", "核对 RRC 重建原因值"],
    },
    "流程缺失": {
        "modules": ["RRC层", "接入流程"],
        "cause": "接入/切换流程缺少预期信号，流程中断",
        "advice": ["定位流程断点对应的协议栈实体", "检查对应接口的信令跟踪是否完整"],
    },
    "丢包": {
        "modules": ["PDCP层"],
        "cause": "PDCP COUNT 不连续，存在丢包",
        "advice": ["检查 PDCP 重排序与乱序门限", "核查空口 BLER", "检查切换数据转发"],
    },
    "硬件/BSP": {
        "modules": ["BSP/硬件驱动"],
        "cause": "设备底层告警（如 I2C 设备缺失、EEPROM、复位原因）",
        "advice": ["核对硬件版本与设备树配置", "检查 cpld/i2c 相关设备节点"],
    },
    "配置/数据库": {
        "modules": ["配置管理", "数据库"],
        "cause": "配置变更或数据库操作异常",
        "advice": ["核对 weblmt 下发的配置参数合法性", "检查 DBS 表结构与字段约束"],
    },
    "运维管理": {
        "modules": ["OAM/网管"],
        "cause": "运维管理面异常",
        "advice": ["检查 OAM 进程状态与 TR69 配置"],
    },
    "进程通信异常": {
        "modules": ["进程间通信模块(os_comm/NRTP)"],
        "cause": "进程间通信发送失败，可能因对端进程异常或 SPID 配置错误",
        "advice": ["检查 SrcSPID/Pri 目标进程是否存活", "核查进程通信路由配置", "检查通信缓冲区是否耗尽"],
    },
    "测试用例执行异常": {
        "modules": ["测试执行框架(CTLP/UEC)"],
        "cause": "测试脚本调用底层函数返回错误码，多为测试工具侧问题而非基站本身",
        "advice": ["按 Return code 查 CTLP 错误码表", "检查测试脚本参数与 UE 状态", "此类日志建议与信令/调度日志交叉验证后再定位"],
    },
    "断言/崩溃": {
        "modules": ["对应进程源码"],
        "cause": "进程触发 assert/panic，属于严重缺陷",
        "advice": ["提取 assert 所在文件与行号", "结合 core dump 与上下文日志分析", "优先上报研发"],
    },
    "断言/进程错误": {
        "modules": ["对应进程(XOS/系统打印)"],
        "cause": "进程输出 ERROR invalid / XOS 异常打印，可能为内部状态异常或无效参数调用",
        "advice": ["提取 XOS_VPrintf 所在源码位置", "统计该类错误频率与时间点", "与信令/调度日志时间对齐确认影响"],
    },
    "业务服务": {
        "modules": ["业务服务层"],
        "cause": "业务服务逻辑异常",
        "advice": ["按函数名定位源码分支", "结合上下文日志确认触发条件"],
    },
    "设备告警": {
        "modules": ["设备系统"],
        "cause": "设备侧告警",
        "advice": ["结合原始日志定位模块"],
    },
}


class DiagnosisAgent:
    def __init__(self, config: EnvConfig, llm: LLMGateway | None = None):
        self.cfg = config
        self.llm = llm or LLMGateway({"provider": "mock"})

    def diagnose(self, results: List[AnalysisResult]) -> DiagnosisResult:
        # 汇总所有事件
        events: List[ParsedEvent] = []
        for r in results:
            events.extend(r.events)
        if not events:
            return DiagnosisResult(has_issue=False, summary="未发现异常", meta={"event_count": 0})

        # 按类别聚合
        cat_count = Counter(e.category for e in events)
        error_events = [e for e in events if e.severity == "error"]

        # 根因推理（规则层）
        root_causes: List[Dict[str, Any]] = []
        module_hits: List[str] = []
        suggestions: List[str] = []
        for category, count in cat_count.most_common():
            rule = self._match_rule(category)
            if not rule:
                continue
            conf = min(0.5 + 0.1 * count, 0.95)
            root_causes.append({
                "category": category, "count": count,
                "root_cause": rule["cause"],
                "confidence": round(conf, 2),
                "evidence": [e.message for e in events if e.category == category][:3],
            })
            for m in rule["modules"]:
                if m not in module_hits:
                    module_hits.append(m)
            for a in rule["advice"]:
                if a not in suggestions:
                    suggestions.append(a)

        summary = (
            f"共发现 {len(events)} 个事件（其中 error {len(error_events)}），"
            f"涉及 {len(cat_count)} 类异常："
            + "、".join(f"{c}×{n}" for c, n in cat_count.most_common(5))
        )

        # LLM 层（可选增强，仅非 mock 时）
        llm_summary = None
        if self.llm.provider != "mock":
            llm_summary = self._llm_reason(events, root_causes)

        return DiagnosisResult(
            has_issue=True, summary=summary, events=events,
            root_causes=root_causes, suggestions=suggestions[:10],
            module_hits=module_hits[:8],
            meta={"event_count": len(events), "error_count": len(error_events),
                  "categories": dict(cat_count), "llm_summary": llm_summary},
        )

    def _match_rule(self, category: str) -> Dict[str, Any] | None:
        if category in RULE_KB:
            return RULE_KB[category]
        for key, val in RULE_KB.items():
            if key in category or category in key:
                return val
        return None

    def _llm_reason(self, events: List[ParsedEvent], root_causes: List[Dict[str, Any]]) -> str:
        payload = {
            "events": [e.model_dump() for e in events[:30]],
            "rule_root_causes": root_causes,
        }
        import json
        try:
            res = self.llm.chat([
                {"role": "system", "content": "你是LTE/NR基站协议栈专家，基于事件归纳根因，给出简短结论。"},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ])
            return str(res.get("content", ""))[:1000]
        except Exception:
            return ""
