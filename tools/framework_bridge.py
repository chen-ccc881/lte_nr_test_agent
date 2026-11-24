"""现有 bjckAutotest 框架的桥接加载器。

设计要点：
- 路径 A：只调用 BasicService 层（不碰 UserKeywords/allure，避免全局状态与额外依赖）。
- 解释器可配置：框架依赖 pywin32/pywinauto/selenium/paramiko/thrift，可能与本项目 .venv 不同。
  优先在当前进程 import；失败时用配置中的 interpreter 启动子进程执行框架调用（subprocess 模式）。
- 依赖自检：import 失败给出明确提示，不静默吞错。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

from env_config import EnvConfig


class FrameworkUnavailable(RuntimeError):
    pass


class FrameworkBridge:
    def __init__(self, config: EnvConfig):
        self.cfg = config
        self.root = Path(self.cfg.get("python", "framework_root", default="") or "")
        self.interpreter = self.cfg.get("python", "interpreter", default="python")
        self._loaded: Dict[str, Any] = {}

    # ---------- 依赖自检 ----------
    def check(self) -> Dict[str, bool]:
        """检查关键 Service 类能否 import（当前进程）。"""
        mods = {
            "maintool": "BasicService.maintenanceTool.mainToolAppService",
            "weblmt": "BasicService.weblmt.lmtGnbService",
            "quts": "BasicService.ue.qutsService",
        }
        result: Dict[str, bool] = {}
        for name, mod in mods.items():
            result[name] = self.probe_service(mod)
        return result

    def probe_service(self, module: str) -> bool:
        """用配置的解释器（子进程）探测模块能否 import，不受当前 .venv 依赖影响。"""
        if not self.root or not self.root.exists():
            return False
        import os
        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.root) + os.pathsep + env.get("PYTHONPATH", "")
        script = (
            "import sys\n"
            "m = sys.stdin.read().strip()\n"
            "try:\n"
            "    __import__(m, fromlist=['X'])\n"
            "    print('1')\n"
            "except Exception:\n"
            "    print('0')\n"
        )
        try:
            proc = subprocess.run(
                [self.interpreter, "-c", script],
                input=module, text=True, capture_output=True, env=env, timeout=30,
            )
            return proc.stdout.strip().endswith("1")
        except Exception:
            return False

    # ---------- 在当前进程加载 Service 类 ----------
    def service(self, module: str, cls: str):
        key = f"{module}.{cls}"
        if key in self._loaded:
            return self._loaded[key]
        if not self.root or not self.root.exists():
            raise FrameworkUnavailable(
                "未配置有效的 python.framework_root，请在 env_config.yaml 指向 bjckAutotest 目录")
        if str(self.root) not in sys.path:
            sys.path.insert(0, str(self.root))
        try:
            mod = __import__(module, fromlist=[cls])
            obj = getattr(mod, cls)
            self._loaded[key] = obj
            return obj
        except Exception as exc:
            raise FrameworkUnavailable(
                f"无法加载 {module}.{cls}: {type(exc).__name__}: {exc}。"
                f"请确认 python.interpreter 指向含全部依赖的解释器，或改用 subprocess 模式。") from exc

    # ---------- subprocess 模式：用另一个解释器执行框架调用 ----------
    def run_in_framework(self, script: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """在配置的解释器中执行一段脚本，stdin 传 JSON，stdout 读 JSON 结果。

        script 需自行 print 一个 JSON 对象作为返回值。这样可跨解释器复用框架依赖。
        """
        if not self.root or not self.root.exists():
            raise FrameworkUnavailable("未配置有效的 python.framework_root")
        env = None
        import os
        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.root) + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [self.interpreter, "-c", script],
            input=json.dumps(payload, ensure_ascii=False),
            text=True, capture_output=True, env=env, cwd=str(self.root),
        )
        if proc.returncode != 0:
            raise FrameworkUnavailable(f"框架子进程失败: {proc.stderr[-1000:]}")
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise FrameworkUnavailable(f"框架子进程未返回JSON: {proc.stdout[-1000:]}")

    @property
    def maintenance_cfg(self) -> Dict[str, Any]:
        return {
            "local_ip": self.cfg.get("maintenance_tool", "local_pc_ip"),
            "tool_path": self.cfg.get("maintenance_tool", "tool_path"),
            "tool_mode": self.cfg.get("maintenance_tool", "tool_mode", default="Autotest"),
            "gnb_ip": self.cfg.get("gnb", "debug_ip"),
        }
