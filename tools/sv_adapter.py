"""SV 适配器：静默启动/停止 + .bei 采集（真实/离线双模式）。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from env_config import EnvConfig
from models.schema import LogArtifact
from tools.base import ToolAdapter
from tools.framework_bridge import FrameworkBridge, FrameworkUnavailable


class SVAdapter(ToolAdapter):
    name = "sv"
    description = "SV 基站侧调度/流量日志（静默）：启动/停止，采集 .bei 日志"
    actions = {
        "start_sv": "配置 EnbCfg.xml 并静默启动 SV_auto.exe",
        "stop_sv": "停止 SV 并归档日志",
        "collect_log": "返回最近一次 SV 日志产物",
    }

    def __init__(self, config: EnvConfig, bridge: FrameworkBridge | None = None):
        self.cfg = config
        self.bridge = bridge or FrameworkBridge(config)
        self._proc = None
        self._tool_path = ""
        self._artifacts: list[LogArtifact] = []
        self._offline_reason: str | None = None

    def _mode(self) -> str:
        if self.cfg.get("agent", "force_offline", default=False):
            return "offline"
        if self.bridge.probe_service("BasicService.maintenanceTool.mainToolAppService"):
            return "live"
        self._offline_reason = "框架模块不可用或未配置 framework_root"
        return "offline"

    def connect(self, config: Dict[str, Any]) -> bool:
        self._tool_path = str(config.get("tool_path") or self.cfg.get("maintenance_tool", "tool_path") or "")
        return bool(self._tool_path) or self._mode() == "offline"

    def health_check(self) -> bool:
        return self._mode() == "live"

    def execute(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        mode = self._mode()
        try:
            if action == "start_sv":
                return self._start(params, mode)
            if action == "stop_sv":
                return self._stop(params, mode)
            if action == "collect_log":
                return self._collect(params, mode)
            return {"ok": False, "data": None, "error": f"未知动作: {action}"}
        except Exception as exc:
            return {"ok": False, "data": None, "error": f"{type(exc).__name__}: {exc}"}

    def _start(self, p: Dict[str, Any], mode: str) -> Dict[str, Any]:
        gnb_ip = str(p.get("gnb_ip") or self.cfg.get("gnb", "debug_ip"))
        local_ip = str(p.get("local_ip") or self.cfg.get("maintenance_tool", "local_pc_ip"))
        tool_path = str(p.get("tool_path") or self._tool_path)
        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        save_dir = self.cfg.artifact_path("sv_log_dir", now)
        self._last_dir = save_dir

        if mode == "live":
            Svc = self.bridge.service("BasicService.maintenanceTool.mainToolAppService", "MainToolAppService")
            self._proc = Svc().startSvLog(
                gnb_ip, local_ip, tool_path,
                self.cfg.get("maintenance_tool", "tool_mode", default="Autotest"),
            )
            ok = self._proc is not None
        else:
            save_dir.mkdir(parents=True, exist_ok=True)
            ok = True

        art = LogArtifact(tool="sv", interface="OMC", path=str(save_dir),
                          started_at=datetime.now().isoformat(),
                          meta={"mode": mode, "gnb_ip": gnb_ip, "detail_items": p.get("detail_items", [])})
        return {"ok": ok, "data": art.model_dump(), "error": None if ok else self._offline_reason}

    def _stop(self, p: Dict[str, Any], mode: str) -> Dict[str, Any]:
        save_dir = getattr(self, "_last_dir", None) or self.cfg.artifact_path("sv_log_dir")
        if mode == "live":
            Svc = self.bridge.service("BasicService.maintenanceTool.mainToolAppService", "MainToolAppService")
            ok = Svc().closeSVTool(
                self._proc, self.cfg.get("maintenance_tool", "tool_mode", default="Autotest"))
        else:
            save_dir.mkdir(parents=True, exist_ok=True)
            bei = save_dir / f"sv_{datetime.now().strftime('%Y%m%d_%H%M%S')}.bei"
            bei.write_text("[offline] simulated SV EI log\n", encoding="utf-8")
            ok = True
        art = LogArtifact(tool="sv", path=str(save_dir), ended_at=datetime.now().isoformat(), meta={"mode": mode})
        self._artifacts.append(art)
        return {"ok": ok, "data": art.model_dump(), "error": None}

    def _collect(self, p: Dict[str, Any], mode: str) -> Dict[str, Any]:
        base = self.cfg.artifact_path("sv_log_dir")
        files = [str(f) for f in base.rglob("*.bei")] if base.exists() else []
        size = sum(Path(f).stat().st_size for f in files)
        art = LogArtifact(tool="sv", path=str(base), files=files, size_bytes=size,
                          meta={"mode": mode, "file_count": len(files)})
        return {"ok": True, "data": art.model_dump(), "error": None}
