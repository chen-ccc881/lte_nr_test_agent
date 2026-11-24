"""上传文件生命周期管理。

规则（按用户需求）：
- 上传的 log（QXDM/pcap/Xtrace/SV/device log 等）视为临时数据：
  落到 artifacts/uploads/temp/<session>/，任务结束后整目录删除（用完即删）。
- 上传的经验文档（Markdown/Word/Excel/PDF/txt）视为长期数据：
  落到 artifacts/experience/ 持久保留，可被 KnowledgeRetriever / 经验库检索复用。
- 按扩展名 + 文件头 magic 自动识别格式，返回给后端解析器匹配的路径。

所有路径限定在 BASE_DIR 内，避免目录穿越。
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

logger = logging.getLogger("lte_nr_agent.upload")

BASE_DIR = Path(__file__).resolve().parent
TEMP_ROOT = BASE_DIR / "artifacts" / "uploads" / "temp"
EXPERIENCE_ROOT = BASE_DIR / "artifacts" / "experience"

# 格式识别：扩展名 -> 规范类别
EXT_CATEGORY = {
    ".isf": "qxdm", ".isfx": "qxdm", ".dlf": "qxdm", ".hdf": "qxdm",
    ".hdf5": "qxdm", ".h5": "qxdm", ".bin": "qxdm",
    ".pcap": "pcap", ".pcapng": "pcap", ".cap": "pcap",
    ".xst": "xtrace", ".bei": "sv_log",
    ".csv": "weblmt_or_experience", ".log": "device_log", ".txt": "text_or_experience",
    ".md": "experience", ".markdown": "experience",
    ".docx": "experience", ".doc": "experience", ".xlsx": "experience", ".xls": "experience",
    ".pdf": "experience",
}
EXPERIENCE_EXTS = {".md", ".markdown", ".docx", ".doc", ".xlsx", ".xls", ".pdf"}
LOG_EXTS = {".isf", ".isfx", ".dlf", ".hdf", ".hdf5", ".h5", ".bin",
            ".pcap", ".pcapng", ".cap", ".xst", ".bei", ".log", ".csv", ".txt"}

MAGIC_RULES = [
    (b"\xd4\xc3\xb2\xa1", "pcap"), (b"\xa1\xb2\xc3\xd4", "pcap"),
    (b"\x4d\x3c\xb2\xa1", "pcap"), (b"\xa1\xb2\x3c\x4d", "pcap"),
    (b"\x0a\x0d\x0d\x0a", "pcap"),
    (b"\xde\xde\xde\xde", "xtrace"),
    (b"\xaf\xaf\xaf\xaf", "sv_log"),
    (b"PK\x03\x04", "zip_or_office"),
    (b"%PDF", "experience"),
]


def detect_format(path: Path) -> str:
    """扩展名优先 + 文件头 magic 佐证判定格式类别。

    强特征扩展名（qxdm 系列 / 经验文档）优先，避免 magic 与扩展名冲突时误判；
    pcap/xtrace/sv 等二进制再由 magic 二次确认。
    """
    suffix = path.suffix.lower()
    ext_category = EXT_CATEGORY.get(suffix, "unknown")
    # QXDM 专属扩展名、经验文档、以及文本类：扩展名优先
    if ext_category in ("qxdm", "experience", "weblmt_or_experience", "text_or_experience"):
        return ext_category
    try:
        head = path.open("rb").read(8)
    except Exception:
        head = b""
    for magic, category in MAGIC_RULES:
        if head.startswith(magic):
            if category == "zip_or_office":
                break  # PK 头用扩展名区分 docx/xlsx
            return category
    return ext_category if ext_category != "unknown" else "unknown"


def is_experience_doc(path: Path) -> bool:
    return path.suffix.lower() in EXPERIENCE_EXTS


class UploadManager:
    def __init__(self, session_id: str):
        safe = "".join(ch for ch in str(session_id) if ch.isalnum() or ch in "-_")[:64] or "default"
        self.session_id = safe
        self.temp_dir = TEMP_ROOT / safe
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        EXPERIENCE_ROOT.mkdir(parents=True, exist_ok=True)

    def _safe_name(self, name: str) -> str:
        base = Path(str(name or "upload.bin")).name
        safe = "".join(ch for ch in base if ch.isalnum() or ch in "._-")
        return safe or "upload.bin"

    def handle_uploads(self, files, keep_experience: bool = True) -> dict:
        """落地上传文件，返回 {logs:[path], experience:[path], details:[...]}。

        files: Gradio gr.File 返回的临时文件路径列表（str 或 list）。
        keep_experience=True 时经验文档复制到长期库；否则也按临时处理。
        """
        result = {"logs": [], "experience": [], "details": []}
        if not files:
            return result
        if isinstance(files, (str, Path)):
            files = [files]

        for item in files:
            src = Path(str(item))
            if not src.exists():
                continue
            name = self._safe_name(src.name)
            category = detect_format(src)
            is_exp = is_experience_doc(src) and category != "pcap"
            if is_exp and keep_experience:
                # 长期经验库：同名覆盖，保留最新
                dst = EXPERIENCE_ROOT / name
            else:
                # 临时日志：按 session 隔离
                dst = self.temp_dir / f"{int(time.time() * 1000)}_{name}"
            try:
                shutil.copy2(src, dst)
            except Exception as exc:
                logger.warning("上传文件落地失败：%s -> %s: %s", src, dst, exc)
                continue
            rel = str(dst.relative_to(BASE_DIR))
            if is_exp and keep_experience:
                result["experience"].append(rel)
            else:
                result["logs"].append(rel)
            result["details"].append({
                "name": name, "category": category, "path": rel,
                "persist": bool(is_exp and keep_experience),
            })
        return result

    def build_query_suffix(self, handled: dict) -> str:
        """把上传文件路径拼成用户问题后缀，供 Orchestrator/DataParser 直接使用。"""
        logs = handled.get("logs", [])
        exp = handled.get("experience", [])
        if not logs and not exp:
            return ""
        lines = ["\n\n【本次上传的文件（系统已落盘，请优先解析）】"]
        for p in logs:
            lines.append(f"- 临时日志（本次任务用完即删）：{p}")
        for p in exp:
            lines.append(f"- 长期经验文档（已入库，可复用）：{p}")
        return "\n".join(lines)

    def cleanup(self) -> int:
        """删除本会话的临时日志目录（含上传日志与解析中间结果）。返回删除文件数。"""
        if not self.temp_dir.exists():
            return 0
        count = sum(1 for _ in self.temp_dir.rglob("*") if _.is_file())
        try:
            shutil.rmtree(self.temp_dir)
            logger.info("已清理临时上传目录：%s（%d 个文件）", self.temp_dir, count)
        except Exception as exc:
            logger.warning("清理临时目录失败：%s", exc)
            return 0
        return count
