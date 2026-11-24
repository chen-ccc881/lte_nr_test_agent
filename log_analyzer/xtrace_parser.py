"""Xtrace .xst 信令日志解析器（复用现有框架）。

流程：
  1) xtraceLogAnalyze() 把 .xst 解码为每条信令一个 JSON（依赖 ASN.1 DLL + toolPath）
  2) xtraceFlowValidator 校验接入流程顺序 + 异常信令黑名单
  3) 归一为统一 ParsedEvent
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from env_config import EnvConfig
from models.schema import AnalysisResult, ParsedEvent
from tools.framework_bridge import FrameworkBridge, FrameworkUnavailable

TRACE_MODEL = "BasicModel.maintenanceTool.traceLogExtractModel"
TRACE_FUNC = "xtraceLogAnalyze"
FLOW_MODEL = "BasicModel.maintenanceTool.xtraceFlowValidator"
FLOW_FUNC = "validate_xtrace_flow_and_anomaly"


class XtraceParser:
    def __init__(self, config: EnvConfig, bridge: FrameworkBridge | None = None):
        self.cfg = config
        self.bridge = bridge or FrameworkBridge(config)

    def parse(self, xst_path: str, mode: str = "auto", extract: bool = True) -> AnalysisResult:
        p = Path(xst_path)
        if not p.exists():
            return AnalysisResult(tool="xtrace", ok=False, error=f"文件不存在: {xst_path}")

        chosen = self._choose_mode(p, mode)
        extract_result: Dict[str, Any] = {}
        json_dir = ""

        if extract:
            try:
                func = self.bridge.service(TRACE_MODEL, TRACE_FUNC)
                tool_path = str(self.cfg.get("maintenance_tool", "tool_path", default="") or "")
                extract_result = func(str(p), chosen, toolPath=tool_path) or {}
                json_dir = str(extract_result.get("output_dir", ""))
            except Exception as exc:
                return AnalysisResult(
                    tool="xtrace", ok=False,
                    error=f"xst 解析失败({type(exc).__name__}): {exc}；.xst 为二进制，须由框架 ASN.1 DLL 解码",
                    raw={"extract": {"error": str(exc)}},
                )

        events: List[ParsedEvent] = []
        flow_raw: Dict[str, Any] = {}
        if json_dir and Path(json_dir).exists():
            try:
                flow_func = self.bridge.service(FLOW_MODEL, FLOW_FUNC)
                flow_raw = flow_func(json_dir) or {}
                events = self._flow_to_events(flow_raw)
            except Exception as exc:
                events.append(ParsedEvent(
                    source="xtrace", severity="warning", category="流程校验失败",
                    code="FLOW_CHECK_ERROR", message=f"{type(exc).__name__}: {exc}",
                ))

        return AnalysisResult(
            tool="xtrace", ok=True, events=events,
            raw={"extract": extract_result, "flow": flow_raw},
            meta={"mode": chosen, "xst": str(p), "json_dir": json_dir,
                  "event_count": len(events)},
        )

    def _choose_mode(self, p: Path, mode: str) -> str:
        if mode in ("4G", "5G"):
            return "4G" if mode == "4G" else "5G"
        # 文件名/路径含 lte/4g 判为 4G，否则 5G
        low = str(p).lower()
        return "4G" if ("lte" in low or "4g" in low) else "5G"

    def _flow_to_events(self, flow: Dict[str, Any]) -> List[ParsedEvent]:
        events: List[ParsedEvent] = []
        anomaly = flow.get("anomaly", {}) or {}
        # 异常信令命中
        for sig in anomaly.get("hits", []) or anomaly.get("matched", []) or []:
            events.append(ParsedEvent(
                source="xtrace", severity="error", category="异常信令",
                code=str(sig), message=f"命中异常信令: {sig}",
                evidence={"signal": sig},
            ))
        # 流程缺失/顺序问题
        flow_info = flow.get("flow", {}) or {}
        for missing in flow_info.get("missing", []) or []:
            events.append(ParsedEvent(
                source="xtrace", severity="warning", category="流程缺失",
                code="FLOW_MISSING", message=f"流程缺少预期信号: {missing}",
                evidence={"missing": missing},
            ))
        # 兼容纯文本 message
        msg = flow.get("message")
        if msg and not events:
            events.append(ParsedEvent(
                source="xtrace",
                severity="error" if not flow.get("pass", True) else "info",
                category="流程校验", code="FLOW_RESULT", message=str(msg),
                evidence={"flow": flow_info},
            ))
        return events
