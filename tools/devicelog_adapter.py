"""Device Log 适配器：通过 WebLMT HTTP API 导出 device log（真实/离线双模式）。

真实实现复用现有框架 LmtGnbService.lmtExportDeviceLog（路径 A）。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from env_config import EnvConfig
from models.schema import LogArtifact
from tools.base import ToolAdapter
from tools.framework_bridge import FrameworkBridge, FrameworkUnavailable


class DeviceLogAdapter(ToolAdapter):
    name = "devicelog"
    description = "通过 WebLMT API 导出基站 device log 压缩包"
    actions = {
        "login": "WebLMT 登录取 Cookie",
        "export": "导出 device log 到本地并产出 LogArtifact",
    }

    def __init__(self, config: EnvConfig, bridge: FrameworkBridge | None = None):
        self.cfg = config
        self.bridge = bridge or FrameworkBridge(config)
        self._lmt = None
        self._offline_reason: str | None = None

    def _mode(self) -> str:
        if self.cfg.get("agent", "force_offline", default=False):
            return "offline"
        if self.bridge.probe_service("BasicService.weblmt.lmtGnbService"):
            return "live"
        self._offline_reason = "框架模块不可用或未配置 framework_root"
        return "offline"

    def connect(self, config: Dict[str, Any]) -> bool:
        return True

    def health_check(self) -> bool:
        return self._mode() == "live"

    def execute(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        mode = self._mode()
        try:
            if action == "login":
                return self._login(params, mode)
            if action == "export":
                return self._export(params, mode)
            return {"ok": False, "data": None, "error": f"未知动作: {action}"}
        except Exception as exc:
            return {"ok": False, "data": None, "error": f"{type(exc).__name__}: {exc}"}

    def _login(self, p: Dict[str, Any], mode: str) -> Dict[str, Any]:
        if mode == "live":
            # 真实登录由 weblmt_adapter 完成，此处仅占位复用其 cookie
            LoginSvc = self.bridge.service("BasicService.weblmt.lmtGnbService", "LmtGnbService")
            ok = LoginSvc is not None
        else:
            ok = True
        return {"ok": ok, "data": {"mode": mode}, "error": None}

    def _export(self, p: Dict[str, Any], mode: str) -> Dict[str, Any]:
        out_dir = self.cfg.artifact_path("devicelog_dir")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_name = f"DeviceLog_{ts}.zip"
        target = out_dir / file_name

        if mode == "live":
            if self._lmt is None:
                return {"ok": False, "data": None,
                        "error": "请先通过 weblmt.login 获取 lmtObj 并传入 params.lmtObj"}
            Svc = self.bridge.service("BasicService.weblmt.lmtGnbService", "LmtGnbService")
            result = Svc().lmtExportDeviceLog(
                self._lmt, str(out_dir), file_name,
                self.cfg.get("gnb", "gnb_type", default="B5140DUAL"))
            ok = bool(result)
        else:
            target.write_bytes(b"[offline] simulated device log zip")
            ok = True

        art = LogArtifact(tool="devicelog", interface="OMC", path=str(target),
                          files=[str(target)] if target.exists() else [],
                          size_bytes=target.stat().st_size if target.exists() else 0,
                          started_at=datetime.now().isoformat(),
                          meta={"mode": mode})
        return {"ok": ok, "data": art.model_dump(), "error": None if ok else "export failed"}
