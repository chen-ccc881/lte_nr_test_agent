"""Phase2 适配器离线验证：不依赖真实框架/环境。"""
from __future__ import annotations

import pytest

from env_config import EnvConfig
from models.schema import ActionType, LogArtifact
from tools.factory import build_registry, health_report


@pytest.fixture
def cfg(tmp_path):
    return EnvConfig.__new__(EnvConfig)


def _config(tmp_path):
    import yaml
    p = tmp_path / "env.yaml"
    p.write_text(yaml.safe_dump({
        "agent": {"force_offline": True},
        "python": {"framework_root": "", "interpreter": "python"},
        "paths": {
            "trace_log_dir": str(tmp_path / "artifacts/xtrace"),
            "sv_log_dir": str(tmp_path / "artifacts/sv"),
            "qxdm_log_dir": str(tmp_path / "artifacts/qxdm"),
            "devicelog_dir": str(tmp_path / "artifacts/devicelog"),
        },
    }, allow_unicode=True), encoding="utf-8")
    return EnvConfig(p)


def test_registry_has_all_adapters(tmp_path):
    reg = build_registry(_config(tmp_path))
    tools = reg.list_tools()
    for name in ["log_tool", "xtrace", "sv", "qxdm", "devicelog", "weblmt"]:
        assert name in tools
    print("适配器注册齐全:", tools)


def test_xtrace_flow_offline(tmp_path):
    reg = build_registry(_config(tmp_path))
    x = reg.get("xtrace")
    assert x.execute("start_trace", {"interface": "UU"})["ok"]
    assert x.execute("stop_trace", {})["ok"]
    r = x.execute("collect_log", {})
    assert r["ok"]
    art = LogArtifact(**r["data"])
    assert art.tool == "xtrace" and art.size_bytes > 0
    print("Xtrace 流程 OK:", art.files)


def test_sv_flow_offline(tmp_path):
    reg = build_registry(_config(tmp_path))
    s = reg.get("sv")
    assert s.execute("start_sv", {})["ok"]
    assert s.execute("stop_sv", {})["ok"]
    r = s.execute("collect_log", {})
    art = LogArtifact(**r["data"])
    assert art.tool == "sv" and art.size_bytes > 0
    print("SV 流程 OK:", art.files)


def test_qxdm_flow_offline(tmp_path):
    reg = build_registry(_config(tmp_path))
    q = reg.get("qxdm")
    assert q.execute("start_ue_log", {})["ok"]
    assert q.execute("stop_ue_log", {})["ok"]
    r = q.execute("collect_log", {})
    art = LogArtifact(**r["data"])
    assert art.tool == "qxdm" and art.size_bytes > 0
    print("QXDM 流程 OK:", art.files)


def test_devicelog_offline(tmp_path):
    reg = build_registry(_config(tmp_path))
    d = reg.get("devicelog")
    r = d.execute("export", {})
    assert r["ok"]
    art = LogArtifact(**r["data"])
    assert art.tool == "devicelog" and art.size_bytes > 0
    print("DeviceLog 流程 OK:", art.path)


def test_weblmt_offline(tmp_path):
    reg = build_registry(_config(tmp_path))
    w = reg.get("weblmt")
    assert w.execute("login", {})["ok"]
    r = w.execute("config", {"table_name": "LTE_NR_CELL", "data": []})
    assert r["ok"]
    assert LogArtifact(**r["data"]).tool == "weblmt"
    print("WebLMT 流程 OK")


def test_health_report(tmp_path):
    rep = health_report(_config(tmp_path))
    assert "tools" in rep and "framework_imports" in rep
    print("健康检查:", rep["tools"])
