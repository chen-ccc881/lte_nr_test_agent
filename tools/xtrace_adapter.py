"""Xtrace 适配器：静默启动/停止 + .xst 采集。

真实模式：调用 MainToolAppService（路径 A）。
离线模式：框架不可用时生成模拟产物，保证链路可验证。
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from env_config import EnvConfig
from models.schema import LogArtifact
from tools.base import ToolAdapter
from tools.framework_bridge import FrameworkBridge, FrameworkUnavailable


class XtraceAdapter(ToolAdapter):
    name = "xtrace"
    description = "Xtrace 基站侧信令跟踪（静默）：启动/停止跟踪，归档并采集 .xst 日志"
    actions = {
        "start_trace": "配置并静默启动 TraceTool_auto.exe 开始信令跟踪",
        "stop_trace": "停止跟踪、终止进程并搬运 .xst 到归档目录",
        "collect_log": "返回最近一次跟踪的 .xst 日志产物",
    }

    def __init__(self, config: EnvConfig, bridge: FrameworkBridge | None = None):
        self.cfg = config
        self.bridge = bridge or FrameworkBridge(config)
        self._proc = None
        self._tool_path = ""
        self._log_dir: Path | None = None
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
            if action == "start_trace":
                return self._start(params, mode)
            if action == "stop_trace":
                return self._stop(params, mode)
            if action == "collect_log":
                return self._collect(params, mode)
            return {"ok": False, "data": None, "error": f"未知动作: {action}"}
        except Exception as exc:
            return {"ok": False, "data": None, "error": f"{type(exc).__name__}: {exc}"}

    # ---------- 具体动作 ----------
    def _start(self, p: Dict[str, Any], mode: str) -> Dict[str, Any]:
        gnb_ip = str(p.get("gnb_ip") or self.cfg.get("gnb", "debug_ip"))
        local_ip = str(p.get("local_ip") or self.cfg.get("maintenance_tool", "local_pc_ip"))
        tool_path = str(p.get("tool_path") or self._tool_path)
        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        save_dir = self.cfg.artifact_path("trace_log_dir", now)

        if mode == "live":
            Svc = self.bridge.service("BasicService.maintenanceTool.mainToolAppService", "MainToolAppService")
            self._proc = Svc().startSignalTraceLog(
                gnb_ip, local_ip, tool_path, str(save_dir),
                self.cfg.get("maintenance_tool", "tool_mode", default="Autotest"),
            )
            ok = self._proc is not None
        else:
            save_dir.mkdir(parents=True, exist_ok=True)
            ok = True  # 离线模式：仅创建归档目录

        self._log_dir = save_dir
        art = LogArtifact(tool="xtrace", interface=str(p.get("interface", "UU")),
                          path=str(save_dir), started_at=datetime.now().isoformat(),
                          meta={"mode": mode, "gnb_ip": gnb_ip, "local_ip": local_ip})
        return {"ok": ok, "data": art.model_dump(), "error": None if ok else self._offline_reason}

    def _stop(self, p: Dict[str, Any], mode: str) -> Dict[str, Any]:
        save_dir = self._log_dir or self.cfg.artifact_path("trace_log_dir")
        if mode == "live":
            Svc = self.bridge.service("BasicService.maintenanceTool.mainToolAppService", "MainToolAppService")
            ok = Svc().closeSignalTraceLog(
                self._proc, str(self._tool_path), str(save_dir),
                self.cfg.get("maintenance_tool", "tool_mode", default="Autotest"),
            )
        else:
            # 离线模式：生成一个模拟 .xst 文件
            save_dir.mkdir(parents=True, exist_ok=True)
            xst = save_dir / f"trace_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xst"
            xst.write_text("[offline] simulated Xtrace signaling log\n", encoding="utf-8")
            ok = True

        art = LogArtifact(tool="xtrace", interface=str(p.get("interface", "UU")),
                          path=str(save_dir), ended_at=datetime.now().isoformat(),
                          meta={"mode": mode})
        self._artifacts.append(art)
        return {"ok": ok, "data": art.model_dump(), "error": None}

    def _collect(self, p: Dict[str, Any], mode: str) -> Dict[str, Any]:
        save_dir = self._log_dir or self.cfg.artifact_path("trace_log_dir")
        files = [str(f) for f in save_dir.rglob("*.xst")] if save_dir.exists() else []
        size = sum(Path(f).stat().st_size for f in files)
        art = LogArtifact(tool="xtrace", interface=str(p.get("interface", "UU")),
                          path=str(save_dir), files=files, size_bytes=size,
                          meta={"mode": mode, "file_count": len(files)})
        return {"ok": True, "data": art.model_dump(), "error": None}
