"""工具装配工厂：根据环境配置创建并注册全部适配器。"""
from __future__ import annotations

from env_config import EnvConfig
from tools.framework_bridge import FrameworkBridge
from tools.log_tool import LogToolAdapter
from tools.registry import ToolRegistry
from tools.xtrace_adapter import XtraceAdapter
from tools.sv_adapter import SVAdapter
from tools.qxdm_adapter import QXDMAdapter
from tools.devicelog_adapter import DeviceLogAdapter
from tools.weblmt_adapter import WebLmtAdapter


def build_registry(config: EnvConfig) -> ToolRegistry:
    bridge = FrameworkBridge(config)
    reg = ToolRegistry()
    reg.register(LogToolAdapter(out_dir="logs"))
    reg.register(XtraceAdapter(config, bridge))
    reg.register(SVAdapter(config, bridge))
    reg.register(QXDMAdapter(config, bridge))
    reg.register(DeviceLogAdapter(config, bridge))
    reg.register(WebLmtAdapter(config, bridge))
    return reg


def health_report(config: EnvConfig) -> dict:
    bridge = FrameworkBridge(config)
    reg = build_registry(config)
    modes = {}
    for name in reg.list_tools():
        tool = reg.get(name)
        try:
            modes[name] = "live" if tool.health_check() else "offline"
        except Exception as exc:
            modes[name] = f"error: {exc}"
    return {
        "framework_root": str(bridge.root) if bridge.root else "",
        "framework_imports": bridge.check(),
        "tools": modes,
    }
