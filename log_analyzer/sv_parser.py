"""SV/EI 调度流量日志解析器（复用现有框架 svinsightEILteValidator）。

产出的框架报告含 has_anomaly / events / summary 等，本模块把它归一为
统一的 ParsedEvent 列表，供问题定位 Agent 消费。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from env_config import EnvConfig
from models.schema import AnalysisResult, ParsedEvent
from tools.framework_bridge import FrameworkBridge, FrameworkUnavailable

SV_LTE_ANALYZER = "BasicModel.maintenanceTool.svinsightEILteValidator"
SV_NR_ANALYZER = "BasicModel.maintenanceTool.svinsightEIValidator"
SV_LTE_FUNC = "analyze_svinsight_ei_lte"
SV_NR_FUNC = "analyze_svinsight_ei"


class SVParser:
    def __init__(self, config: EnvConfig, bridge: FrameworkBridge | None = None):
        self.cfg = config
        self.bridge = bridge or FrameworkBridge(config)

    def parse(self, ei_dir: str, mode: str = "auto", thresholds: Dict[str, Any] | None = None) -> AnalysisResult:
        p = Path(ei_dir)
        if not p.exists():
            return AnalysisResult(tool="sv", ok=False, error=f"目录不存在: {ei_dir}")

        chosen = self._choose_mode(p, mode)
        try:
            raw = self._call_analyzer(chosen, str(p), thresholds)
        except FrameworkUnavailable as exc:
            return AnalysisResult(tool="sv", ok=False, error=str(exc))
        except Exception as exc:
            return AnalysisResult(tool="sv", ok=False, error=f"{type(exc).__name__}: {exc}")

        events = self._to_events(raw, chosen)
        return AnalysisResult(
            tool="sv", ok=True, events=events, raw=raw,
            meta={"mode": chosen, "dir": str(p), "event_count": len(events)},
        )

    def _choose_mode(self, p: Path, mode: str) -> str:
        if mode in ("LTE", "NR"):
            return mode
        # 根据文件名后缀 _Lte 自动判断
        files = [f.name for f in p.rglob("*.csv")]
        if any("_Lte" in f for f in files):
            return "LTE"
        return "NR"

    def _call_analyzer(self, mode: str, ei_dir: str, thresholds: Dict[str, Any] | None) -> Dict[str, Any]:
        if mode == "LTE":
            func = self.bridge.service(SV_LTE_ANALYZER, SV_LTE_FUNC)
        else:
            func = self.bridge.service(SV_NR_ANALYZER, SV_NR_FUNC)
        if thresholds:
            return func(ei_dir, thresholds=thresholds)
        return func(ei_dir)

    def _to_events(self, raw: Dict[str, Any], mode: str) -> List[ParsedEvent]:
        events: List[ParsedEvent] = []
        for ev in raw.get("events", []) or []:
            text = str(ev)
            category, code = self._classify(text)
            events.append(ParsedEvent(
                source="sv", severity="error", category=category,
                code=code, message=text,
                evidence={"mode": mode, "raw_event": text},
            ))
        return events

    @staticmethod
    def _classify(text: str):
        if "调度失败" in text or "schd" in text.lower():
            return "调度异常", "SCHD_FAIL"
        if "bler" in text.lower() or "误块" in text:
            return "链路质量", "BLER_HIGH"
        if "harq" in text.lower():
            return "HARQ异常", "HARQ_FAIL"
        if "丢包" in text or "pdcp" in text.lower():
            return "丢包", "PDCP_LOSS"
        if "mcs" in text.lower() or "阶数" in text:
            return "调制异常", "MCS_LOW"
        if "重传" in text or "tcp" in text.lower():
            return "传输异常", "TCP_RETRANS"
        return "其他异常", "SV_ANOMALY"
