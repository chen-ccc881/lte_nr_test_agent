"""QXDM/QUTS 适配器：UE 侧 log 跟踪（真实/离线双模式）。

真实实现复用现有框架 QutsService（路径 A，QUTS Thrift + QXDM COM）。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from env_config import EnvConfig
from models.schema import LogArtifact
from tools.base import ToolAdapter
from tools.framework_bridge import FrameworkBridge, FrameworkUnavailable


class QXDMAdapter(ToolAdapter):
    name = "qxdm"
    description = "高通 QXDM/QUTS UE 侧 log 跟踪：启动/停止并产出 .hdf"
    actions = {
        "start_ue_log": "启动 UE 侧 QXDM log 跟踪",
        "stop_ue_log": "停止并保存 .hdf 日志",
        "collect_log": "返回最近一次 UE log 产物",
    }

    def __init__(self, config: EnvConfig, bridge: FrameworkBridge | None = None):
        self.cfg = config
        self.bridge = bridge or FrameworkBridge(config)
        self._started = False
        self._last_hdf: str | None = None
        self._offline_reason: str | None = None

    def _mode(self) -> str:
        if self.cfg.get("agent", "force_offline", default=False):
            return "offline"
        if self.bridge.probe_service("BasicService.ue.qutsService"):
            return "live"
        self._offline_reason = "QUTS SDK(Common/DiagService/QutsClient) 未安装"
        return "offline"

    def connect(self, config: Dict[str, Any]) -> bool:
        return True

    def health_check(self) -> bool:
        return self._mode() == "live"

    def execute(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        mode = self._mode()
        try:
            if action == "start_ue_log":
                return self._start(params, mode)
            if action == "stop_ue_log":
                return self._stop(params, mode)
            if action == "collect_log":
                return self._collect(params, mode)
            return {"ok": False, "data": None, "error": f"未知动作: {action}"}
        except Exception as exc:
            return {"ok": False, "data": None, "error": f"{type(exc).__name__}: {exc}"}

    def _start(self, p: Dict[str, Any], mode: str) -> Dict[str, Any]:
        if mode == "live":
            Svc = self.bridge.service("BasicService.ue.qutsService", "QutsService")
            ok = Svc().startUeLogTrace()
        else:
            ok = True
        self._started = bool(ok)
        art = LogArtifact(tool="qxdm", interface="UE", started_at=datetime.now().isoformat(),
                          meta={"mode": mode})
        return {"ok": self._started, "data": art.model_dump(),
                "error": None if self._started else self._offline_reason}

    def _stop(self, p: Dict[str, Any], mode: str) -> Dict[str, Any]:
        if not self._started:
            return {"ok": False, "data": None, "error": "UE log 未启动"}
        out_dir = self.cfg.artifact_path("qxdm_log_dir")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        if mode == "live":
            Svc = self.bridge.service("BasicService.ue.qutsService", "QutsService")
            hdf = Svc().stopUeLogTrace()
            self._last_hdf = str(hdf)
        else:
            hdf = out_dir / f"ue_{ts}.hdf"
            hdf.write_text("[offline] simulated QXDM HDF log\n", encoding="utf-8")
            self._last_hdf = str(hdf)
        art = LogArtifact(tool="qxdm", interface="UE", path=self._last_hdf,
                          files=[self._last_hdf], ended_at=datetime.now().isoformat(),
                          size_bytes=Path(self._last_hdf).stat().st_size,
                          meta={"mode": mode})
        return {"ok": True, "data": art.model_dump(), "error": None}

    def _collect(self, p: Dict[str, Any], mode: str) -> Dict[str, Any]:
        base = self.cfg.artifact_path("qxdm_log_dir")
        files = [str(f) for f in base.rglob("*.hdf")] if base.exists() else []
        size = sum(Path(f).stat().st_size for f in files)
        art = LogArtifact(tool="qxdm", interface="UE", path=str(base), files=files,
                          size_bytes=size, meta={"mode": mode, "file_count": len(files)})
        return {"ok": True, "data": art.model_dump(), "error": None}
