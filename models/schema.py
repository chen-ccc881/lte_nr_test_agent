"""Phase1 数据模型。"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ActionType(str, Enum):
    CONNECT_LOG = "connect_log"
    CONFIG = "config"
    TRIGGER = "trigger"
    WAIT = "wait"
    COLLECT_LOG = "collect_log"
    ANALYZE = "analyze"
    # Phase2 工具动作
    START_TRACE = "start_trace"        # Xtrace 静默启动
    STOP_TRACE = "stop_trace"          # Xtrace 停止并归档 .xst
    START_SV = "start_sv"              # SV 静默启动
    STOP_SV = "stop_sv"                # SV 停止并归档 .bei
    START_UE_LOG = "start_ue_log"      # QXDM/QUTS 启动
    STOP_UE_LOG = "stop_ue_log"        # QXDM/QUTS 停止
    EXPORT_DEVICE_LOG = "export_device_log"  # WebLMT 导出 device log
    LMT_LOGIN = "lmt_login"            # WebLMT 登录取 Cookie
    LMT_CONFIG = "lmt_config"          # WebLMT 参数配置
    EXEC_CMD = "exec_cmd"              # SSH 执行命令
    UNKNOWN = "unknown"


class LogArtifact(BaseModel):
    """所有工具日志产物的统一模型（Phase2 核心数据契约）。"""
    tool: str                          # xtrace | sv | qxdm | devicelog | weblmt
    artifact_type: str = "raw"         # raw | parsed | summary
    interface: str = ""                # Uu | XN | S1 | NG | UE | OMC | 空
    path: str = ""                     # 本地落盘路径
    files: List[str] = Field(default_factory=list)
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    size_bytes: int = 0
    meta: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None

    @classmethod
    def empty(cls, tool: str, error: str) -> "LogArtifact":
        return cls(tool=tool, error=error)


class TestStep(BaseModel):
    seq: int
    action: ActionType = ActionType.UNKNOWN
    target: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)
    expect: Optional[str] = None
    raw_text: Optional[str] = None
    needs_confirm: bool = False


class TestCase(BaseModel):
    case_id: str
    case_name: str
    preconditions: List[str] = Field(default_factory=list)
    steps: List[TestStep] = Field(default_factory=list)
    expected_result: Optional[str] = None
    source_file: Optional[str] = None
    # 真实用例模板的额外列：测试项目/子项/测试目的/优先级/实测结果/人员/日期/备注
    metadata: Dict[str, Any] = Field(default_factory=dict)


class StepResult(BaseModel):
    seq: int
    action: ActionType
    status: str  # ok | error | skipped
    output: Any = None
    error: Optional[str] = None
    elapsed_sec: float = 0.0


class RunResult(BaseModel):
    case_id: str
    status: str
    step_results: List[StepResult] = Field(default_factory=list)
    started_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    finished_at: Optional[str] = None

    def summary(self) -> dict:
        ok = sum(1 for s in self.step_results if s.status == "ok")
        err = sum(1 for s in self.step_results if s.status == "error")
        skip = sum(1 for s in self.step_results if s.status == "skipped")
        return {
            "case_id": self.case_id,
            "status": self.status,
            "steps": len(self.step_results),
            "ok": ok,
            "error": err,
            "skipped": skip,
        }


class ToolCall(BaseModel):
    """LLM 决定调用某个工具的结构化输出。"""
    tool: str
    action: str
    params: Dict[str, Any] = Field(default_factory=dict)
    thought: Optional[str] = None


class ParsedEvent(BaseModel):
    """统一的解析事件：所有工具/分析器的异常与关键事件归一到此模型。"""
    source: str                        # xtrace | sv | devicelog
    severity: str = "info"             # info | warning | error
    category: str = ""                 # 信令异常 | 调度异常 | 丢包 | 设备告警 ...
    code: str = ""                     # 异常信令名 / 指标名 / 错误码
    message: str = ""                  # 可读描述
    timestamp: Optional[str] = None    # 事件时间
    cell_id: Optional[str] = None
    ue_gid: Optional[str] = None
    evidence: Dict[str, Any] = Field(default_factory=dict)  # 原始证据(数值/字段)


class AnalysisResult(BaseModel):
    """单个日志源的解析结果。"""
    tool: str
    ok: bool = True
    error: Optional[str] = None
    events: List[ParsedEvent] = Field(default_factory=list)
    raw: Dict[str, Any] = Field(default_factory=dict)  # 框架原始报告
    meta: Dict[str, Any] = Field(default_factory=dict)


class DiagnosisResult(BaseModel):
    """问题定位 Agent 的综合结论。"""
    has_issue: bool = False
    summary: str = ""
    events: List[ParsedEvent] = Field(default_factory=list)
    root_causes: List[Dict[str, Any]] = Field(default_factory=list)  # 根因假设+置信度+证据
    suggestions: List[str] = Field(default_factory=list)             # 解决方案建议
    module_hits: List[str] = Field(default_factory=list)             # 疑似问题模块
    meta: Dict[str, Any] = Field(default_factory=dict)
