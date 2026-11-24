"""QXDM/QCAT 日志工具辅助解析适配器。

依据 QCAT 6.x User Guide 第 5 章 Command Line Operations：
- `QCAT -export -analyzer=... -outputdir=... file.hdf`  导出结构化分析结果（推荐，体积极小）
- `QCAT -txt -filter=... file.hdf`  导出解析文本（按需，用 filter 限定 log/event ID）

设计要点：
- 用 -export + 精选 analyzer，只产出排障相关 KPI 表，避免 -txt 全量文本的 GB 级膨胀；
- 所有中间文件落在系统临时目录，解析后整目录删除（用完即删）；
- 输出为 tab/逗号分隔文本，解析为结构化摘要供 DataParser/RadioAnalyst 使用；
- QCAT 路径可通过环境变量 QCAT_EXE 覆盖，默认安装路径。

导出档位（实测 263MB HDF）：
- 轻量（默认）：analyzer="QCAT Sample;LTE;Summary"，2 文件 / <1MB / ~12s，含 RI、RLC DL 统计；
- 均衡：analyzer="QCAT Sample;LTE;Time Grids;Phy/RLC/PDCP Grids"，3 文件 / ~13s，含 DL 吞吐；
- 全 KPI：Physical Grid 组（RSRP/RSRQ/SNR/BLER/CQI/MCS）约 16 文件 / ~150s，时序文件磁盘峰值约 162MB，
  仅在确实需要物理层 KPI 时按需启用，默认不跑。档位可用环境变量 QCAT_ANALYZERS 覆盖。

依赖：QCAT.exe（外部工具，用户已接受）。本模块只负责调用与结果解析，不含任何 Qualcomm 私有格式实现。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger("lte_nr_agent.qcat")

DEFAULT_QCAT = r"C:\Program Files (x86)\Qualcomm\Qcat 6.x\Bin\QCAT.exe"
# 默认只跑 LTE Summary 分析器组（实测仅 2 个文件、<1MB、~12s）；可用 QCAT_ANALYZERS 覆盖
DEFAULT_ANALYZERS = os.getenv("QCAT_ANALYZERS", "QCAT Sample;LTE;Summary").strip() or "QCAT Sample;LTE;Summary"


def qcat_exe() -> Path:
    custom = os.getenv("QCAT_EXE")
    path = Path(custom) if custom else Path(DEFAULT_QCAT)
    return path


def is_available() -> bool:
    return qcat_exe().exists()


def _run_qcat(args: list[str], timeout: int = 300) -> tuple[bool, str]:
    exe = qcat_exe()
    if not exe.exists():
        return False, f"QCAT工具不存在：{exe}（可用环境变量QCAT_EXE指定）"
    try:
        proc = subprocess.run(
            [str(exe), *args],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        ok = "Success:" in out or proc.returncode == 0
        if not ok:
            logger.warning("QCAT执行异常：%s", out[-500:])
        return ok, out
    except subprocess.TimeoutExpired:
        return False, f"QCAT执行超时（{timeout}s）"
    except Exception as exc:
        return False, f"QCAT执行失败：{type(exc).__name__}: {exc}"


def _parse_export_file(path: Path, max_rows: int) -> str:
    """解析 QCAT -export 输出：% 开头为注释，数据行 tab/逗号分隔。"""
    title = ""
    columns = []
    data_lines = []
    with path.open("r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.rstrip("\n\r")
            if not line.strip():
                continue
            if line.startswith("% Column #"):
                columns.append(line.split(":", 1)[-1].strip() if ":" in line else line)
                continue
            if line.startswith("%"):
                continue
            cells = line.split("\t") if "\t" in line else line.split(",")
            cells = [c.strip() for c in cells if c.strip()]
            if not cells:
                continue
            # 纯文字、无分隔符且位于列定义之前的行视为标题
            if not columns and len(cells) == 1 and not title and not any(ch.isdigit() for ch in line):
                title = line.strip()
                continue
            if len(data_lines) < max_rows:
                data_lines.append("  " + " | ".join(cells))
    parts = [f"文件={path.name}"]
    if title:
        parts.append(f"标题={title}")
    if columns:
        parts.append("列=" + ", ".join(columns[:12]))
    body = "\n".join(data_lines) if data_lines else "  （无数据行）"
    if len(data_lines) >= max_rows:
        parts.append(f"（仅展示前{max_rows}行）")
    return "；".join(parts) + "\n" + body


def export_qxdm(
    log_path: str,
    analyzers: str | None = None,
    max_rows_per_file: int = 15,
    full: bool = False,
    timeout: int = 300,
) -> str:
    """主入口：用 QCAT -export 把 QXDM 专有 log 导出为结构化 KPI 摘要。

    Args:
        log_path: .hdf/.isf/.dlf/.qmdl 路径。
        analyzers: -analyzer= 参数；默认 DEFAULT_ANALYZERS（LTE Summary 组）。
        max_rows_per_file: 每个分析文件展示的最大数据行数。
        full: True 则导出全部分析器（产物更多，约百MB级）。
        timeout: QCAT 执行超时秒数。
    """
    target = Path(log_path).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    if not target.exists():
        return f"QXDM文件不存在：{target}"
    if not is_available():
        return f"QCAT工具不可用：{qcat_exe()}"

    tmpdir = Path(tempfile.mkdtemp(prefix="qcat_export_"))
    try:
        args = ["-export", f"-outputdir={tmpdir}"]
        if not full:
            args.append(f"-analyzer={analyzers or DEFAULT_ANALYZERS}")
        args.append(str(target))

        ok, output = _run_qcat(args, timeout=timeout)
        if not ok:
            return f"QCAT导出失败：{output[-800:]}"

        files = sorted(p for p in tmpdir.rglob("*") if p.is_file() and p.suffix.lower() in (".txt", ".csv"))
        if not files:
            return (
                f"【QCAT导出】执行成功但未生成分析文件。\n"
                f"分析器={analyzers or DEFAULT_ANALYZERS}\n"
                "可能原因：该日志不含所选分析器的数据，或analyzer路径不匹配。"
            )

        mode = "全量" if full else "轻量摘要"
        sections = [
            f"【QCAT结构化分析】文件={target.name}, 模式={mode}, "
            f"分析器={analyzers or DEFAULT_ANALYZERS}, 文件数={len(files)}"
        ]
        for f in files[:40]:
            try:
                sections.append("--- " + _parse_export_file(f, max_rows_per_file))
            except Exception as exc:
                sections.append(f"--- {f.name} 解析失败：{type(exc).__name__}: {exc}")
        if len(files) > 40:
            sections.append(f"（共{len(files)}个文件，仅展示前40个）")
        return "\n".join(sections)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        logger.info("已清理QCAT临时导出目录：%s", tmpdir)


def list_analyzers(log_path: str, timeout: int = 120) -> str:
    """导出全部分析器并列出可用分析文件名，帮助用户选择 -analyzer 路径。"""
    return export_qxdm(log_path, full=True, max_rows_per_file=0, timeout=timeout)
