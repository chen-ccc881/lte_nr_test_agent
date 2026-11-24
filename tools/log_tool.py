"""专用测试 log 记录工具适配器（示例实现，可替换为真实SDK）。"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict

from tools.base import ToolAdapter


class LogToolAdapter(ToolAdapter):
    name = "log_tool"
    description = "专用测试日志记录工具：连接日志服务、开始/停止记录、按接口导出日志"
    actions = {
        "connect_log": "连接并开始按指定接口记录日志",
        "collect_log": "导出指定时长的日志到本地文件",
        "stop": "停止记录",
    }

    def __init__(self, out_dir: str = "logs"):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._connected = False
        self._interfaces: list[str] = []

    def connect(self, config: Dict[str, Any]) -> bool:
        self._interfaces = str(config.get("interface", "Uu")).split(",")
        # 真实场景：此处调用工具SDK/CLI建立连接
        self._connected = True
        return True

    def execute(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._connected and action != "connect_log":
            return {"ok": False, "data": None, "error": "log工具未连接"}

        if action == "connect_log":
            ok = self.connect(params)
            return {"ok": ok, "data": {"interfaces": self._interfaces}, "error": None}

        if action == "collect_log":
            duration = int(params.get("duration_sec", 3))
            time.sleep(min(duration, 3))  # 骨架上限，实际按 duration 采集
            fname = f"log_{int(time.time() * 1000)}.txt"
            path = self.out_dir / fname
            path.write_text(
                f"# 模拟日志\n接口: {self._interfaces}\n时长: {duration}s\n"
                f"[INFO] RRC Setup Complete\n[INFO] HO Command sent\n",
                encoding="utf-8",
            )
            return {"ok": True, "data": {"path": str(path), "lines": 2}, "error": None}

        if action == "stop":
            self._connected = False
            return {"ok": True, "data": None, "error": None}

        return {"ok": False, "data": None, "error": f"未知动作: {action}"}

    def health_check(self) -> bool:
        return True
