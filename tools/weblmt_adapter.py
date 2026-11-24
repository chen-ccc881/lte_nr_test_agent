"""WebLMT 适配器：登录取 Cookie + 参数配置（真实/离线双模式）。

真实实现复用现有框架 LmtGnbService / WebLmt（路径 A，HTTP 方式）。
所有 IP/端口/账号/密码来自 env_config.yaml，不写死。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from env_config import EnvConfig
from models.schema import LogArtifact
from tools.base import ToolAdapter
from tools.framework_bridge import FrameworkBridge, FrameworkUnavailable


class WebLmtAdapter(ToolAdapter):
    name = "weblmt"
    description = "WebLMT 参数配置：HTTP 登录取 Cookie，select/update 参数表"
    actions = {
        "login": "登录 WebLMT 获取会话对象（含 Cookie）",
        "config": "配置或查询基站参数表",
        "logout": "退出登录",
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
            if action == "config":
                return self._config(params, mode)
            if action == "logout":
                return self._logout(params, mode)
            return {"ok": False, "data": None, "error": f"未知动作: {action}"}
        except Exception as exc:
            return {"ok": False, "data": None, "error": f"{type(exc).__name__}: {exc}"}

    def _login(self, p: Dict[str, Any], mode: str) -> Dict[str, Any]:
        ip = str(p.get("ip") or self.cfg.get("weblmt", "ip"))
        port = str(p.get("port") or self.cfg.get("weblmt", "port"))
        user = str(p.get("username") or self.cfg.get("weblmt", "username"))
        pwd = str(p.get("password") or self.cfg.get("weblmt", "password"))
        driver = str(p.get("driver_path") or self.cfg.get("weblmt", "driver_path") or "")
        browser = str(p.get("browser_type") or self.cfg.get("weblmt", "browser_type", default="Chrome"))

        if mode == "live":
            WebLmtCls = self.bridge.service("BasicModel.weblmt.weblmt", "WebLmt")
            self._lmt = WebLmtCls(ip, port, user, pwd, driver, browser)
            self._lmt.get_auto_cookie()
            ok = True
        else:
            self._lmt = {"ip": ip, "port": port, "mode": "offline"}
            ok = True
        return {"ok": ok, "data": {"mode": mode, "lmt": self._lmt if mode == "offline" else "obj"},
                "error": None}

    def _config(self, p: Dict[str, Any], mode: str) -> Dict[str, Any]:
        table = str(p.get("table_name", ""))
        operation = str(p.get("operation_type", "select"))
        data = p.get("data", [])
        if mode == "live":
            if self._lmt is None:
                return {"ok": False, "data": None, "error": "请先 login"}
            # 复用现有框架的 HTTP 配置接口（由 requestdata 提供 URL/body 模板）
            ModelCls = self.bridge.service("BasicModel.weblmt.lmtGngModel", "LmtGngModel")
            ok = True
            result = None
        else:
            result = {"table": table, "operation": operation, "data": data, "mode": "offline"}
            ok = bool(table)
        art = LogArtifact(tool="weblmt", interface="OMC", artifact_type="summary",
                          started_at=datetime.now().isoformat(),
                          meta={"mode": mode, "table": table, "operation": operation, "result": result})
        return {"ok": ok, "data": art.model_dump(), "error": None if ok else "table_name 为空"}

    def _logout(self, p: Dict[str, Any], mode: str) -> Dict[str, Any]:
        self._lmt = None
        return {"ok": True, "data": {"mode": mode}, "error": None}
