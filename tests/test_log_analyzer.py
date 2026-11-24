"""日志解析与问题定位模块验证（含真实参考日志）。"""
from __future__ import annotations

import pytest

from env_config import EnvConfig
from log_analyzer.analyzer import LogAnalyzer
from log_analyzer.devicelog_parser import DeviceLogParser
from log_analyzer.diagnosis_agent import DiagnosisAgent
from log_analyzer.sv_parser import SVParser
from models.schema import AnalysisResult, ParsedEvent

REF = r"C:\Users\Administrator\Downloads\Tools_B5140_ltev2_T1997_OptSchd_0826_T44\tool\参考log"


def _cfg(tmp_path=None):
    return EnvConfig()


def test_sv_parser_real_csv():
    """用真实 SV LTE CSV 验证解析出调度异常事件。"""
    sv_dir = REF + r"\EI_Report\172.16.38.201\EI_Detail\20261029141606480"
    result = SVParser(_cfg()).parse(sv_dir, mode="LTE")
    assert result.ok, result.error
    assert result.meta["mode"] == "LTE"
    assert any(e.code == "SCHD_FAIL" for e in result.events)
    print("SV 解析事件数:", len(result.events))


def test_devicelog_parser_real_zip():
    """用真实 device log zip 验证错误行提取与分类。"""
    z = REF + r"\DeviceLog_package (5).zip"
    result = DeviceLogParser(_cfg()).parse(z, max_events=100)
    assert result.ok, result.error
    assert len(result.events) > 0
    cats = {e.category for e in result.events}
    # 只要事件被归入具体类别（非空）即可；具体类别分布随日志内容变化
    assert all(c and c != "设备异常" for c in cats), cats
    print("DeviceLog 事件类别:", cats)


def test_diagnosis_agent_aggregate():
    """验证问题定位 Agent 的规则聚合与建议生成。"""
    events = [
        ParsedEvent(source="sv", severity="error", category="调度异常", code="SCHD_FAIL", message="调度失败比例=1.0"),
        ParsedEvent(source="sv", severity="error", category="调度异常", code="SCHD_FAIL", message="UE调度失败"),
        ParsedEvent(source="xtrace", severity="error", category="异常信令", code="RLF", message="RLF detected"),
    ]
    results = [AnalysisResult(tool="mixed", ok=True, events=events)]
    diag = DiagnosisAgent(_cfg()).diagnose(results)
    assert diag.has_issue
    assert any(rc["category"] == "调度异常" for rc in diag.root_causes)
    assert any("MAC层调度器" in m for m in diag.module_hits)
    assert len(diag.suggestions) > 0
    print("诊断摘要:", diag.summary)


def test_analyzer_end_to_end_real():
    """端到端：真实 SV + DeviceLog → 综合诊断报告。"""
    sv_dir = REF + r"\EI_Report\172.16.38.201\EI_Detail\20261029141606480"
    dev = REF + r"\DeviceLog_package (5).zip"
    az = LogAnalyzer(_cfg())
    diag = az.analyze(sv_dir=sv_dir, devicelog=dev)
    report = az.to_report(diag)
    assert diag.has_issue
    assert "调度异常" in report
    assert "MAC层调度器" in report
    print("端到端诊断报告生成成功，事件数:", len(diag.events))


def test_xtrace_missing_file():
    """xst 不存在时给出明确错误。"""
    from log_analyzer.xtrace_parser import XtraceParser
    r = XtraceParser(_cfg()).parse("not_exist.xst")
    assert not r.ok
    assert "不存在" in r.error
