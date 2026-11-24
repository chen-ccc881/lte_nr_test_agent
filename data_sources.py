"""DataParser 多数据源解析工具（纯函数，零 LangChain / 外部框架依赖）。

覆盖 DataParser 专家需要的实时数据源解析：
- detect_data_sources：探测本次任务可见的数据源（日志/配置/经验文档）；
- parse_weblmt_config：WebLMT 基站配置导出（csv/xlsx/json/txt）结构化；
- parse_pcap：Wireshark pcap/pcapng 基础统计（纯 struct 解析，无 scapy 依赖）；
- parse_device_log / parse_xtrace_log / parse_sv_log：设备日志 / Xtrace / SV LOG；
- parse_experience_doc：本地 Markdown/文本/Word/Excel 经验文档检索。

所有路径都先解析到 BASE_DIR 下，避免任意文件读取。解析失败返回可读说明而非抛异常。
依赖 pandas/openpyxl 的部分均做可选降级。
"""

from __future__ import annotations

import json
import re
import struct
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

LOG_EXTENSIONS = (".txt", ".log", ".isf", ".isfx", ".bin", ".hdf", ".hdf5", ".h5", ".dlf", ".xst", ".bei", ".xst.bak")
CONFIG_EXTENSIONS = (".csv", ".xlsx", ".xls", ".json", ".txt", ".ini", ".xml")
PCAP_EXTENSIONS = (".pcap", ".pcapng", ".cap")
DOC_EXTENSIONS = (".md", ".markdown", ".txt", ".docx", ".doc", ".xlsx", ".xls", ".pdf")


def is_tti_csv(path) -> bool:
    """判断是否为 LTE 空口 TTI 级 CSV（CMAC/PDCP/RLC 的 dl/ulUeTtiInfo）。"""
    low = str(path).lower()
    return ("uetttiinfo" in low or ("uetti" in low and "lte" in low)) and low.endswith(".csv")


def _resolve(path, must_exist: bool = True) -> Path:
    raw = Path(str(path)).expanduser()
    resolved = raw if raw.is_absolute() else BASE_DIR / raw
    return resolved


def _glob_files(path: Path, extensions: tuple[str, ...]) -> list[Path]:
    if path.is_file():
        return [path] if path.suffix.lower() in extensions else []
    if not path.is_dir():
        return []
    found: list[Path] = []
    for ext in extensions:
        found.extend(sorted(path.rglob(f"*{ext}")))
        found.extend(sorted(path.rglob(f"*{ext.upper()}")))
    seen: set[Path] = set()
    unique: list[Path] = []
    for item in found:
        if item.resolve() not in seen:
            seen.add(item.resolve())
            unique.append(item)
    return unique[:100]


def detect_data_sources(query: str = "", *, log_dir="", config_dir="", doc_dir="") -> dict:
    """探测本次任务可见数据源，返回布尔标记与命中路径（最多各 20 条）。

    Orchestrator 据此判断该调度哪些专家、是否需要向工程师索要路径。
    """
    query_text = str(query or "")
    path_hints = re.findall(r"[\w./\\-]+\.(?:txt|log|isf|isfx|hdf|hdf5|h5|dlf|xst|bei|csv|xlsx|xls|json|pcap|pcapng|cap|md|markdown|docx|doc|pdf)", query_text, flags=re.I)
    candidates = [Path(hint) for hint in path_hints]

    def collect(directory: str, exts: tuple[str, ...]) -> list[Path]:
        items: list[Path] = []
        if directory:
            items.extend(_glob_files(_resolve(directory, must_exist=False), exts))
        for candidate in candidates:
            if candidate.suffix.lower() in exts:
                resolved = _resolve(candidate, must_exist=False)
                if resolved.exists():
                    items.append(resolved)
        # 按解析后路径去重（目录扫描与路径线索可能重复命中同一文件）
        seen: set[Path] = set()
        unique: list[Path] = []
        for item in items:
            key = item.resolve()
            if key not in seen:
                seen.add(key)
                unique.append(item)
        return unique[:20]

    logs = collect(log_dir or "artifacts/qxdm", LOG_EXTENSIONS + PCAP_EXTENSIONS)
    # TTI CSV 也属于日志：从 log_dir 与路径线索中收集 csv 候选
    csv_logs = collect(log_dir or "artifacts/qxdm", (".csv",))
    csv_logs = [p for p in csv_logs if is_tti_csv(p)]
    configs = collect(config_dir or "docs", CONFIG_EXTENSIONS)
    docs = collect(doc_dir or "docs", DOC_EXTENSIONS)

    # 默认目录兜底探测（存在才纳入）
    for default_dir, exts, bucket in (
        ("artifacts/qxdm", LOG_EXTENSIONS + PCAP_EXTENSIONS, logs),
        ("artifacts/uploads/temp", LOG_EXTENSIONS + PCAP_EXTENSIONS, logs),  # 前端临时上传日志
        ("docs", CONFIG_EXTENSIONS, configs),
        ("docs", DOC_EXTENSIONS, docs),
        ("artifacts/experience", DOC_EXTENSIONS, docs),  # 前端长期经验文档
    ):
        if len(bucket) < 20:
            bucket.extend(_glob_files(_resolve(default_dir, must_exist=False), exts)[:20 - len(bucket)])

    # 前端临时上传目录中的 TTI CSV：不依赖 LLM 透传完整路径，服务端主动扫描。
    # 这是上传 CMAC/PDCP/RLC TTI CSV 能被 DataParser 发现的关键（LLM 常把 query 简化，丢失路径线索）。
    for upload_dir in ("artifacts/uploads/temp", "artifacts/qxdm"):
        if len(logs) >= 20:
            break
        for p in _glob_files(_resolve(upload_dir, must_exist=False), (".csv",)):
            if is_tti_csv(p) and p not in logs:
                logs.append(p)
                if len(logs) >= 20:
                    break

    # TTI CSV 是空口日志而非配置/经验文档：合并日志目录候选 + 从 configs 分离
    tti = [p for p in configs if is_tti_csv(p)] + csv_logs
    if tti:
        for p in tti:
            if p not in logs:
                logs.append(p)
        configs = [p for p in configs if not is_tti_csv(p)]

    # 全局去重（docs 可能在 docs/ 与 experience/ 重复命中）
    def dedup(items):
        seen = set(); out = []
        for item in items:
            key = item.resolve()
            if key not in seen:
                seen.add(key); out.append(item)
        return out[:20]
    logs, configs, docs = dedup(logs), dedup(configs), dedup(docs)

    return {
        "has_log": bool(logs),
        "has_config": bool(configs),
        "has_experience_doc": bool(docs),
        "log_files": [str(p) for p in logs[:20]],
        "config_files": [str(p) for p in configs[:20]],
        "experience_files": [str(p) for p in docs[:20]],
        "query_mentions_log": any(k in query_text.lower() for k in ("日志", "log", "qxdm", "xtrace", "sv", "pcap")),
        "query_mentions_config": any(k in query_text.lower() for k in ("配置", "weblmt", "参数表", "config")),
    }


def parse_weblmt_config(path: str, keyword: str = "", max_rows: int = 200) -> str:
    """解析 WebLMT 配置导出文件（csv/xlsx/xls/json/txt），按关键字过滤。"""
    file_path = _resolve(path)
    if not file_path.exists():
        return f"WebLMT配置文件不存在：{file_path}"
    suffix = file_path.suffix.lower()

    def keep(row: str) -> bool:
        return not keyword or keyword.lower() in row.lower()

    try:
        if suffix == ".json":
            data = json.loads(file_path.read_text(encoding="utf-8", errors="ignore"))
            text = json.dumps(data, ensure_ascii=False, indent=2)
            lines = [line for line in text.splitlines() if keep(line)][:max_rows]
            return f"【WebLMT配置-JSON】{file_path.name}\n" + "\n".join(lines)

        if suffix in (".csv", ".txt"):
            lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            return f"【WebLMT配置-{suffix[1:].upper()}】{file_path.name}\n" + "\n".join(
                line for line in lines if keep(line)
            )[:max_rows * 200]

        if suffix in (".xlsx", ".xls"):
            try:
                import pandas as pd
            except ImportError:
                return "解析WebLMT Excel需要 pandas/openpyxl，请在 .venv 中安装后重试。"
            book = pd.read_excel(file_path, sheet_name=None, dtype=str).values()
            rows: list[str] = []
            for frame in book:
                for values in frame.fillna("").astype(str).values.tolist():
                    line = " | ".join(values)
                    if keep(line):
                        rows.append(line)
                    if len(rows) >= max_rows:
                        break
                if len(rows) >= max_rows:
                    break
            return f"【WebLMT配置-Excel】{file_path.name}\n" + "\n".join(rows)
    except Exception as exc:
        return f"WebLMT配置解析失败：{type(exc).__name__}: {exc}"
    return f"暂不支持的WebLMT配置格式：{suffix}"


def parse_pcap(path: str, max_packets: int = 5000) -> str:
    """Wireshark pcap/pcapng 空口信令深度解码（委托 protocols，纯 Python 无外部工具）。"""
    try:
        import protocols
        return protocols.decode_pcap(path, max_packets)
    except Exception as exc:
        return f"pcap深度解码失败：{type(exc).__name__}: {exc}"


def _scan_text_log(path: Path, keyword: str, max_lines: int) -> str:
    if not keyword:
        keyword = "error|warn|fail|rrc|mac|phy|prach|harq|rsrp|sinr"
    pattern = re.compile(keyword, re.I)
    hits: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if pattern.search(line):
            hits.append(line.rstrip())
            if len(hits) >= max_lines:
                break
    return "\n".join(hits)


def parse_device_log(path: str, keyword: str = "", max_lines: int = 100) -> str:
    """解析基站 device log：支持单文件、目录、zip（内部文本）。"""
    file_path = _resolve(path)
    if not file_path.exists():
        return f"device log路径不存在：{file_path}"
    try:
        if file_path.suffix.lower() == ".zip":
            import zipfile
            parts: list[str] = []
            with zipfile.ZipFile(file_path) as archive:
                for name in archive.namelist()[:50]:
                    if not name.lower().endswith((".log", ".txt")):
                        continue
                    text = archive.read(name).decode("utf-8", errors="ignore")
                    lines = [ln for ln in text.splitlines() if not keyword or keyword.lower() in ln.lower()]
                    if lines:
                        parts.append(f"--- {name} ---\n" + "\n".join(lines[:max_lines]))
            return "\n\n".join(parts) or "device log zip 中未按关键字命中内容。"
        files = _glob_files(file_path, (".log", ".txt"))
        parts = [f"--- {f.name} ---\n" + _scan_text_log(f, keyword, max_lines) for f in files[:20]]
        return "\n\n".join(parts) or "device log 中未按关键字命中内容。"
    except Exception as exc:
        return f"device log解析失败：{type(exc).__name__}: {exc}"


def parse_xtrace_log(path: str, max_lines: int = 100, mode: str = "auto") -> str:
    """Xtrace .xst 深度解码（委托 protocols，纯 Python；文本则直接读取）。

    mode 与 max_lines 均透传到底层解码器；解码器内部按字符上限硬封顶，
    避免上万条记录回流撑爆调用方（ReAct agent）的 LLM 上下文。
    """
    file_path = _resolve(path)
    if not file_path.exists():
        return f"Xtrace日志不存在：{file_path}"
    if file_path.suffix.lower() == ".xst":
        try:
            import protocols
            return protocols.decode_xtrace(
                str(file_path), mode=mode, max_records=max_lines
            )
        except Exception as exc:
            return f"Xtrace深度解码失败：{type(exc).__name__}: {exc}"
    try:
        lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()[:max_lines]
        return f"【Xtrace文本】{file_path.name}\n" + "\n".join(lines)
    except Exception as exc:
        return f"Xtrace日志读取失败：{type(exc).__name__}: {exc}"


def parse_sv_log(path: str, max_lines: int = 100) -> str:
    """SV/EI 调度日志深度分析（委托 protocols，纯 pandas）。"""
    file_path = _resolve(path)
    if not file_path.exists():
        return f"SV LOG不存在：{file_path}"
    try:
        import protocols
        return protocols.analyze_sv(str(file_path))
    except Exception as exc:
        return f"SV/EI分析失败：{type(exc).__name__}: {exc}"


def parse_experience_doc(path: str, keyword: str = "", max_rows: int = 80) -> str:
    """检索本地经验文档（Markdown/文本/Word/Excel/PDF）命中片段。"""
    file_path = _resolve(path)
    if not file_path.exists():
        return f"经验文档不存在：{file_path}"
    if file_path.is_dir():
        files = _glob_files(file_path, DOC_EXTENSIONS)[:20]
        blocks = [parse_experience_doc(str(f), keyword, max_rows) for f in files]
        return "\n\n".join(blocks) or "经验库目录中未找到可解析文档。"

    suffix = file_path.suffix.lower()
    try:
        if suffix in (".md", ".markdown", ".txt"):
            lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            hits = [ln for ln in lines if not keyword or keyword.lower() in ln.lower()]
            return f"【经验文档-{suffix[1:].upper()}】{file_path.name}\n" + "\n".join(hits[:max_rows])

        if suffix == ".docx":
            from docx import Document
            document = Document(str(file_path))
            rows = [p.text for p in document.paragraphs]
            for table in document.tables:
                for row in table.rows:
                    rows.append(" | ".join(cell.text for cell in row.cells))
            hits = [r for r in rows if r.strip() and (not keyword or keyword.lower() in r.lower())]
            return f"【经验文档-DOCX】{file_path.name}\n" + "\n".join(hits[:max_rows])

        if suffix in (".xlsx", ".xls"):
            import pandas as pd
            frames = pd.read_excel(file_path, sheet_name=None, dtype=str).values()
            rows: list[str] = []
            for frame in frames:
                for values in frame.fillna("").astype(str).values.tolist():
                    line = " | ".join(values)
                    if not keyword or keyword.lower() in line.lower():
                        rows.append(line)
                    if len(rows) >= max_rows:
                        break
                if len(rows) >= max_rows:
                    break
            return f"【经验文档-Excel】{file_path.name}\n" + "\n".join(rows)
    except ImportError:
        return f"解析 {suffix} 经验文档缺少可选依赖（python-docx/pandas），请安装后重试。"
    except Exception as exc:
        return f"经验文档解析失败：{type(exc).__name__}: {exc}"
    return f"暂不支持的经验文档格式：{suffix}"
