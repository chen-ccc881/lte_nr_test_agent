"""环境配置中心：所有 IP / 账号 / 工具路径均来自配置，不写死在代码里。

配置文件查找顺序：
  1) 构造时显式传入 config_path
  2) 环境变量 AGENT_ENV_CONFIG
  3) 项目根目录 env_config.yaml
  4) 若都不存在，使用 DEFAULT_CONFIG（占位值，需按实际环境修改）

密码支持从环境变量读取：值写成 "env:变量名"，加载时自动解析。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict

import yaml

DEFAULT_CONFIG: Dict[str, Any] = {
    "agent": {
        # true=强制离线模拟（不调用真实工具），用于开发/测试；false=按框架可用性自动判断
        "force_offline": False,
    },
    "python": {
        # 运行现有 bjckAutotest 框架的 Python 解释器（需含 pywin32/pywinauto/selenium/paramiko/thrift）
        "interpreter": "python",
        "framework_root": "",  # bjckAutotest 目录绝对路径
    },
    "weblmt": {
        "ip": "172.16.44.139",
        "port": "8090",
        "username": "admin",
        "password": "env:WEBLMT_PASSWORD",
        "driver_path": "",  # chromedriver.exe 路径
        "browser_type": "Chrome",
    },
    "gnb": {
        "gnb_type": "B5140DUAL",
        "debug_ip": "172.16.44.139",
        "ssh_ip": "172.16.44.139",
        "ssh_user": "root",
        "ssh_password": "env:GNB_SSH_PASSWORD",
    },
    "maintenance_tool": {
        "local_pc_ip": "172.16.44.133",
        "tool_path": "",  # 维护工具解压根目录（含 DU_Xtrace / DU_SV）
        "tool_mode": "Autotest",  # Autotest=静默
        "is_save_qxdm": False,
    },
    "paths": {
        "trace_log_dir": "artifacts/xtrace",
        "sv_log_dir": "artifacts/sv",
        "qxdm_log_dir": "artifacts/qxdm",
        "devicelog_dir": "artifacts/devicelog",
        "parsed_dir": "artifacts/parsed",
    },
    "trace": {
        "trace_me_port": "6060",
        "sig_trace_port": "6080",
        "move_xst": True,
    },
}


def _resolve_secret(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("env:"):
        return os.environ.get(value[4:], "")
    return value


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class EnvConfig:
    def __init__(self, config_path: str | Path | None = None):
        self.path = self._locate(config_path)
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.data = _deep_merge(DEFAULT_CONFIG, raw or {})
        self._resolve_all(self.data)

    @staticmethod
    def _locate(config_path: str | Path | None) -> Path:
        if config_path:
            return Path(config_path)
        env = os.environ.get("AGENT_ENV_CONFIG")
        if env:
            return Path(env)
        return Path(__file__).parent / "env_config.yaml"

    @classmethod
    def _resolve_all(cls, d: Dict[str, Any]) -> None:
        for k, v in d.items():
            if isinstance(v, dict):
                cls._resolve_all(v)
            else:
                d[k] = _resolve_secret(v)

    def get(self, *keys: str, default=None):
        cur: Any = self.data
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    def artifact_path(self, key: str, filename: str = "") -> Path:
        base = Path(self.get("paths", key, default="artifacts"))
        base.mkdir(parents=True, exist_ok=True)
        return base / filename if filename else base


def create_template(path: str | Path | None = None) -> str:
    p = Path(path) if path else Path(__file__).parent / "env_config.yaml"
    if not p.exists():
        p.write_text(
            yaml.safe_dump(DEFAULT_CONFIG, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    return str(p)


if __name__ == "__main__":
    print("配置模板:", create_template())
    cfg = EnvConfig()
    print("weblmt ip:", cfg.get("weblmt", "ip"))
    print("framework root:", cfg.get("python", "framework_root") or "(未配置)")
