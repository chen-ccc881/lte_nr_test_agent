"""日志分析统一门面：一键解析多源日志并给出问题定位结论。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from env_config import EnvConfig
from log_analyzer.devicelog_parser import DeviceLogParser
from log_analyzer.diagnosis_agent import DiagnosisAgent
from log_analyzer.sv_parser import SVParser
from log_analyzer.xtrace_parser import XtraceParser
from llm.gateway import LLMGateway
from models.schema import AnalysisResult, DiagnosisResult
from tools.framework_bridge import FrameworkBridge


class LogAnalyzer:
    def __init__(self, config: EnvConfig, llm: LLMGateway | None = None):
        self.cfg = config
        self.bridge = FrameworkBridge(config)
        self.sv = SVParser(config, self.bridge)
        self.xtrace = XtraceParser(config, self.bridge)
        self.devicelog = DeviceLogParser(config)
        self.agent = DiagnosisAgent(config, llm)

    def analyze(self, *, sv_dir: str | None = None, xst: str | None = None,
                devicelog: str | None = None, sv_mode: str = "auto",
                xst_mode: str = "auto", extract_xst: bool = True) -> DiagnosisResult:
        results: List[AnalysisResult] = []
        if sv_dir:
            results.append(self.sv.parse(sv_dir, mode=sv_mode))
        if xst:
            results.append(self.xtrace.parse(xst, mode=xst_mode, extract=extract_xst))
        if devicelog:
            results.append(self.devicelog.parse(devicelog))
        diag = self.agent.diagnose(results)
        diag.meta["sources"] = {
            "sv_dir": sv_dir, "xst": xst, "devicelog": devicelog,
            "results": [{"tool": r.tool, "ok": r.ok, "error": r.error,
                         "events": len(r.events)} for r in results],
        }
        return diag

    def to_report(self, diag: DiagnosisResult) -> str:
        """生成可读的文本报告。"""
        lines = ["=" * 60, "日志分析与问题定位报告", "=" * 60]
        lines.append(f"结论: {'发现异常' if diag.has_issue else '未发现异常'}")
        lines.append(f"摘要: {diag.summary}")
        if diag.module_hits:
            lines.append(f"疑似问题模块: {', '.join(diag.module_hits)}")
        if diag.root_causes:
            lines.append("\n--- 根因假设 ---")
            for i, rc in enumerate(diag.root_causes, 1):
                lines.append(f"{i}. [{rc['category']}] 置信度={rc['confidence']}")
                lines.append(f"   根因: {rc['root_cause']}")
                for ev in rc.get("evidence", [])[:2]:
                    lines.append(f"   证据: {ev}")
        if diag.suggestions:
            lines.append("\n--- 解决建议 ---")
            for i, s in enumerate(diag.suggestions, 1):
                lines.append(f"{i}. {s}")
        if diag.meta.get("llm_summary"):
            lines.append("\n--- LLM 归纳 ---")
            lines.append(str(diag.meta["llm_summary"]))
        return "\n".join(lines)
