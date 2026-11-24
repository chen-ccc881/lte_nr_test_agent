"""Device Log 解析器：扫描基站 device log(zip 或目录)中的错误/告警行。

设备日志格式：
  [lineno=1] 2026-04-27 11:26:44 0 [WARN] SERVICE_LOG: crm_cm.c(2538) Func: message
级别：ERROR / WARN / INFO；错误特征：err: / fail / assert / panic / exception
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any, Dict, List

from env_config import EnvConfig
from models.schema import AnalysisResult, ParsedEvent

LINE_RE = re.compile(
    r"\[lineno=(\d+)\]\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+\d+\s+"
    r"\[(\w+)\]\s*([^:]+):\s*(.*)"
)
ERR_KEYWORDS = re.compile(r"\berr(or)?\b|fail|assert|panic|exception|abnormal|timeout", re.I)
# 误报抑制：成功类消息里常含 success，即使带 setup 等词也不算错误
SUPPRESS = re.compile(r"success|succeed|normal|ok\b", re.I)


class DeviceLogParser:
    def __init__(self, config: EnvConfig):
        self.cfg = config

    def parse(self, path: str, max_events: int = 200, severity_filter: str = "error") -> AnalysisResult:
        p = Path(path)
        if not p.exists():
            return AnalysisResult(tool="devicelog", ok=False, error=f"路径不存在: {path}")

        sources: List[tuple[str, str]] = []  # (文件名, 文本)
        try:
            if p.suffix.lower() == ".zip":
                with zipfile.ZipFile(p) as z:
                    for name in z.namelist():
                        if name.lower().endswith((".log", ".txt")) or ".log." in name.lower():
                            sources.append((name, z.read(name).decode("utf-8", "ignore")))
            elif p.is_dir():
                for f in p.rglob("*.log"):
                    sources.append((str(f), f.read_text(encoding="utf-8", errors="ignore")))
            else:
                sources.append((str(p), p.read_text(encoding="utf-8", errors="ignore")))
        except Exception as exc:
            return AnalysisResult(tool="devicelog", ok=False, error=f"读取失败: {exc}")

        events: List[ParsedEvent] = []
        for fname, text in sources:
            for line in text.splitlines():
                ev = self._parse_line(line, fname)
                if ev and self._keep(ev, severity_filter):
                    events.append(ev)
                    if len(events) >= max_events:
                        break
            if len(events) >= max_events:
                break

        return AnalysisResult(
            tool="devicelog", ok=True, events=events,
            meta={"path": str(p), "files_scanned": len(sources), "event_count": len(events)},
        )

    def _parse_line(self, line: str, fname: str) -> ParsedEvent | None:
        m = LINE_RE.match(line.strip())
        if not m:
            # 非标准行：仅当含 ERROR 关键字时记录
            if re.search(r"\[ERROR\]|\bpanic\b|\bassert\b|\bFailed\b", line, re.I):
                category = self._classify("", line)
                return ParsedEvent(
                    source="devicelog", severity="error", category=category,
                    code="DEVICE_ERROR", message=line.strip()[:300],
                    evidence={"file": fname},
                )
            return None
        lineno, ts, level, module, content = m.groups()
        level_u = level.upper()
        is_err = level_u == "ERROR" or (ERR_KEYWORDS.search(content) and not SUPPRESS.search(content))
        if not is_err:
            return None
        severity = "error" if level_u == "ERROR" else "warning"
        category = self._classify(module, content)
        return ParsedEvent(
            source="devicelog", severity=severity, category=category,
            code=module.strip(), message=content.strip()[:300],
            timestamp=ts, evidence={"file": fname, "lineno": lineno, "module": module.strip()},
        )

    @staticmethod
    def _keep(ev: ParsedEvent, severity_filter: str) -> bool:
        if severity_filter == "error":
            return ev.severity == "error"
        if severity_filter == "warning":
            return ev.severity in ("error", "warning")
        return True

    @staticmethod
    def _classify(module: str, content: str) -> str:
        mod = module.strip().upper()
        text = (module + " " + content).upper()
        # 精确模块名优先（避免内容里路径子串误伤）
        if any(k in mod for k in ["CONFIG_LOG", "DBS"]):
            return "配置/数据库"
        if any(k in mod for k in ["OAM_LOG", "TR69"]):
            return "运维管理"
        if any(k in mod for k in ["SERVICE_LOG", "CRM"]):
            return "业务服务"
        if any(k in mod for k in ["BSP_LOG", "I2C", "EEPROM", "CPLD"]):
            return "硬件/BSP"
        # 内容关键字兜底
        if "RLF" in text or "RADIO LINK" in text:
            return "链路异常"
        if "NRTP" in text or "OS_COMM" in text or "COMM SEND" in text:
            return "进程通信异常"
        if "CTLP" in text or "UEC" in text or "FAILED TO CALL" in text:
            return "测试用例执行异常"
        if "ASSERT" in text or "PANIC" in text or "XOS_VPRINTF" in text or "ERROR INVALID" in text:
            return "断言/进程错误"
        if "SQL" in text or "CONFIG" in text:
            return "配置/数据库"
        return "设备告警"
