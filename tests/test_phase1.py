"""Phase1 离线自测：不依赖真实LLM和工具。"""
from __future__ import annotations

import pandas as pd

from llm.gateway import LLMGateway
from orchestrator.engine import Orchestrator
from parser.case_parser import CaseParser
from tools.log_tool import LogToolAdapter
from tools.registry import ToolRegistry


def _make_md_case(tmp_path):
    p = tmp_path / "sample_case.md"
    p.write_text(
        "# 用例 NR_HO_007: NR站内切换验证\n"
        "- 连接Uu和Xn口日志工具\n"
        "- 触发UE切换\n"
        "- 等待1秒\n"
        "- 导出日志\n",
        encoding="utf-8",
    )
    return str(p)


def _make_real_excel(tmp_path):
    """生成真实模板格式的 Excel（每用例一行，步骤在单元格内换行）。"""
    p = tmp_path / "real_case.xlsx"
    pd.DataFrame([{
        "用例编号": "基站支持VLAN_配置和功能验证_01_002",
        "测试项目": "VLAN配置",
        "测试子项": "功能验证",
        "测试用例标题": "非级联组网下基站关闭VLAN功能",
        "测试目的": "验证VLAN配置",
        "预置条件": "1.基站上电并正常加载版本和数据配置文件\n2.基站级联组网开关关闭\n",
        "测试步骤": "1.在基站上配置单VLAN，VLANID=3000\n"
                    "2.观察接口状态\n"
                    "3.等待1秒\n"
                    "4.导出日志",
        "预期结果": "1.VLAN可配置成功\n2.VLAN信息已删除",
        "优先级": "高",
        "实测结果": "通过",
        "测试结果说明": "配置成功",
        "测试人员": "张宏强",
        "测试日期": "2019-11-29",
        "备注": "",
    }]).to_excel(p, index=False)
    return str(p)


def test_end_to_end(tmp_path):
    llm = LLMGateway({"provider": "mock"})
    parser = CaseParser(llm)
    cases = parser.parse_file(_make_md_case(tmp_path))
    assert len(cases) == 1
    assert len(cases[0].steps) == 4

    registry = ToolRegistry()
    registry.register(LogToolAdapter(out_dir=str(tmp_path / "logs")))
    run = Orchestrator(registry).run(cases[0])
    assert run.status == "success", run
    # Phase1 仅映射 log 工具：connect_log/wait/collect_log 为 ok，trigger 为 skipped
    statuses = {r.seq: r.status for r in run.step_results}
    assert statuses == {1: "ok", 2: "skipped", 3: "ok", 4: "ok"}, statuses
    print("Phase1 端到端自测通过:", run.summary())


def test_parse_real_excel_template(tmp_path):
    """真实模板：每用例一行，单元格内换行拆步骤，额外列进 metadata。"""
    cases = CaseParser(LLMGateway({"provider": "mock"})).parse_file(
        _make_real_excel(tmp_path))
    assert len(cases) == 1
    c = cases[0]
    assert c.case_id == "基站支持VLAN_配置和功能验证_01_002"
    assert c.case_name == "非级联组网下基站关闭VLAN功能"
    assert len(c.preconditions) == 2
    assert c.preconditions[0].startswith("基站上电")
    assert len(c.steps) == 4
    assert c.steps[0].raw_text.startswith("在基站上配置单VLAN")
    assert c.steps[0].action.value == "config"
    assert c.steps[1].action.value == "analyze"
    assert c.steps[2].action.value == "wait"
    assert c.steps[3].action.value == "collect_log"
    assert c.metadata["优先级"] == "高"
    assert c.metadata["测试人员"] == "张宏强"
    assert "VLAN可配置成功" in c.expected_result
    print("真实模板解析自测通过:", c.case_id, "steps=", len(c.steps))


def test_registry_and_schema():
    from models.schema import ToolCall

    registry = ToolRegistry()
    registry.register(LogToolAdapter(out_dir="logs"))
    assert "log_tool" in registry.list_tools()
    assert len(registry.get_function_schemas()) == 1

    tc = ToolCall(tool="log_tool", action="collect_log", params={"duration_sec": 1})
    assert tc.action == "collect_log"


def test_unsupported_format(tmp_path):
    import pytest

    p = tmp_path / "dummy.txt"
    p.write_text("x", encoding="utf-8")
    llm = LLMGateway({"provider": "mock"})
    with pytest.raises(ValueError):
        CaseParser(llm).parse_file(str(p))
