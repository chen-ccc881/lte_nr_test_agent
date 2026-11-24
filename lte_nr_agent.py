"""LTE/NR 无线排障 Multi-Agent 工程助手。

架构（Orchestrator + 多 Specialist + Critic + Memory）：
- Orchestrator 总指挥：判断数据源、拆解任务、分发专家、收敛结果、控制终止/人工补充；
- DataParser 数据解析专家：统一读取并标准化 QXDM / WebLMT / pcap / device log / Xtrace / SV / 经验文档；
- RadioAnalyst 无线算法与信令专家：故障假设、信令核查、参数校验、定量计算的核心推理；
- KnowledgeRetriever 协议与经验检索专家：检索 3GPP 协议 / 内部 SOP / 历史案例 / 联网资料；
- Critic 独立评审专家：校验证据链、参数正确性、逻辑冲突与结论完备性，标记置信度并可触发重跑；
- MemoryManager 记忆 Agent：任务结束后沉淀案例，后续同类故障自动召回；
- Human-in-the-Loop：缺关键参数时暂停，工程师反馈后继续执行；
- LangGraph 调度：Orchestrator → DataParser →（RadioAnalyst ∥ KnowledgeRetriever）→ [Critic] → Memory → Summary；
  Critic 默认关闭（ENABLE_CRITIC=0），设 ENABLE_CRITIC=1 可恢复评审节点。

支撑模块：memory_manager.py（长期记忆）、data_sources.py（多源解析），二者零外部框架依赖。
依赖：langchain、langgraph、langchain-openai、langchain-chroma、pypdf、python-docx、python-dotenv。
"""

from __future__ import annotations

import hashlib
import html as html_lib
import importlib
import json
import logging
import operator
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from functools import wraps
from pathlib import Path
from typing import Annotated, Any, Callable, Sequence, TypedDict
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import csv_tti_analyzer
import data_sources
import memory_manager
import qcat_adapter
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

try:
    import httpx2 as httpx
except ImportError:
    import httpx


# ===================== 0. 全局配置与日志 =====================
BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "lte_nr_agent.log", encoding="utf-8"),
    ],
)
logging.getLogger("httpx2").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("lte_nr_agent")


# ===================== 异常分类：可重试 / 不可重试 =====================
class RetryableAgentError(Exception):
    """可重试异常：API超时、网络连接异常、临时性服务错误。"""


class NonRetryableAgentError(Exception):
    """不可重试异常：参数错误、文件不存在、业务格式错误。"""


# 可重试关键词必须足够具体：普通脚本里的 timeout=200ms、业务结论里的“无法判定”都不能命中。
RETRYABLE_KEYWORDS = (
    "apitimeouterror", "readtimeout", "connecttimeout", "timed out", "请求超时",
    "调用超时", "读取超时", "连接超时", "connectionerror", "connecterror",
    "api connection error", "apiconnectionerror", "networkerror", "网络异常",
    "连接失败", "temporarily unavailable", "service unavailable", "502", "503", "504",
)
NON_RETRYABLE_KEYWORDS = (
    "参数错误", "日志文件不存在", "文件不存在", "no such file", "invalid argument",
    "validationerror", "valueerror", "typeerror", "missing parameter",
    "unsupported", "暂不支持", "不支持的场景",
)
FAILURE_PREFIXES = (
    "QXDM日志解析失败", "计算失败", "RAG检索失败", "Web RAG检索失败",
    "参数错误", "日志文件不存在", "HDF日志解析失败",
)


def detect_agent_error(message: str) -> Exception | None:
    """只识别明确技术失败；Agent正常结论（如“无法判定”）一律返回 None。"""
    text = str(message).strip()
    lowered = text.lower()
    if not text.startswith(FAILURE_PREFIXES) and "日志文件不存在：" not in text:
        if not any(keyword.lower() in lowered for keyword in RETRYABLE_KEYWORDS):
            return None
    if any(keyword.lower() in lowered for keyword in NON_RETRYABLE_KEYWORDS):
        return NonRetryableAgentError(text)
    if any(keyword.lower() in lowered for keyword in RETRYABLE_KEYWORDS):
        return RetryableAgentError(text)
    return None


def classify_agent_error(message: str) -> Exception:
    """异常对象兜底分类；无法识别时默认不可重试。"""
    return detect_agent_error(message) or NonRetryableAgentError(str(message))


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


SUB_AGENT_TIMEOUT_SECONDS = _env_float("LTE_NR_SUB_AGENT_TIMEOUT", 300)
# ReAct 循环轮数上限：每轮约一次 LLM 调用，限制在 12 轮防止无限试探耗尽超时
SUB_AGENT_RECURSION_LIMIT = int(os.getenv("LTE_NR_SUB_AGENT_RECURSION", "12"))
LLM_TIMEOUT_SECONDS = _env_float("LTE_NR_LLM_TIMEOUT", 300)
GRAPH_TIMEOUT_SECONDS = _env_float("LTE_NR_GRAPH_TIMEOUT", 600)
REQUEST_TIMEOUT_SECONDS = _env_float("HUNYUAN_REQUEST_TIMEOUT", 300)


def _resolve_doc_dirs() -> list[Path]:
    """解析本地资料目录；支持 RAG_DOC_DIRS 多目录，兼容旧 RAG_DOC_DIR/RAG_PDF_DIR。"""
    raw_dirs = os.getenv("RAG_DOC_DIRS")
    if raw_dirs:
        parts = [p.strip() for p in raw_dirs.split(",") if p.strip()]
    else:
        legacy = os.getenv("RAG_DOC_DIR") or os.getenv("RAG_PDF_DIR")
        # 默认纳入长期经验库目录（前端上传的经验文档会落到这里）
        parts = [legacy] if legacy else [str(BASE_DIR / "docs" / "3gpp"), str(BASE_DIR / "artifacts" / "experience")]
    dirs = []
    for part in parts:
        path = Path(part).expanduser()
        if not path.is_absolute():
            path = BASE_DIR / path
        path.mkdir(parents=True, exist_ok=True)
        dirs.append(path)
    return dirs


RAG_DOC_DIRS = _resolve_doc_dirs()
RAG_DOC_DIR = RAG_DOC_DIRS[0]
RAG_COLLECTION_NAME = os.getenv("RAG_COLLECTION_NAME", "lte_nr_tech_docs")
CHROMA_DIR = Path(os.getenv("CHROMA_DIR", BASE_DIR / ".chroma" / "3gpp"))
CHROMA_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv()

API_KEY = os.getenv("HUNYUAN_API_KEY") or os.getenv("OPENAI_API_KEY")
API_BASE = (
    os.getenv("HUNYUAN_API_BASE")
    or os.getenv("OPENAI_API_BASE")
    or "https://tokenhub.tencentmaas.com/v1"
)
ROUTER_MODEL = os.getenv("HUNYUAN_ROUTER_MODEL", "hy3")
LOG_MODEL = os.getenv("HUNYUAN_LOG_MODEL", "hy3")
PROTOCOL_MODEL = os.getenv("HUNYUAN_PROTOCOL_MODEL", "hy3")
LIGHT_MODEL = os.getenv("HUNYUAN_LIGHT_MODEL", "hy4-flash")
CRITIC_MODEL = os.getenv("HUNYUAN_CRITIC_MODEL", "hy3")
# Critic 评审开关：默认关闭（暂不执行评审）；设为 1/true/yes 可恢复接入。
ENABLE_CRITIC = os.getenv("ENABLE_CRITIC", "0").strip().lower() in {"1", "true", "yes", "on"}
DATA_PARSER_MODEL = os.getenv("HUNYUAN_DATA_PARSER_MODEL", LOG_MODEL)
RADIO_MODEL = os.getenv("HUNYUAN_RADIO_MODEL", ROUTER_MODEL)
KNOWLEDGE_MODEL = os.getenv("HUNYUAN_KNOWLEDGE_MODEL", PROTOCOL_MODEL)
SUB_MODEL = os.getenv("HUNYUAN_SUB_MODEL", LIGHT_MODEL)

if not API_KEY:
    raise RuntimeError("请在 .env 中配置 HUNYUAN_API_KEY 或 OPENAI_API_KEY")


def build_chat_model(model_name: str) -> ChatOpenAI:
    """构建 OpenAI 兼容接口的混元模型。"""
    return ChatOpenAI(
        model=model_name,
        api_key=API_KEY,
        base_url=API_BASE,
        temperature=0.1,
        request_timeout=REQUEST_TIMEOUT_SECONDS,
        max_retries=1,
        http_client=httpx.Client(trust_env=False),
        http_async_client=httpx.AsyncClient(trust_env=False),
        http_socket_options=(),
    )


router_llm = build_chat_model(ROUTER_MODEL)
orchestrator_llm = router_llm
log_llm = build_chat_model(LOG_MODEL)
data_parser_llm = build_chat_model(DATA_PARSER_MODEL)
protocol_llm = build_chat_model(PROTOCOL_MODEL)
radio_llm = build_chat_model(RADIO_MODEL)
knowledge_llm = build_chat_model(KNOWLEDGE_MODEL)
light_llm = build_chat_model(LIGHT_MODEL)
critic_llm = build_chat_model(CRITIC_MODEL)
sub_llm = light_llm
summary_llm = router_llm
memory_manager_instance = memory_manager.get_memory_manager()
logger.info(
    "模型初始化完成：orchestrator=%s, data_parser=%s, radio=%s, knowledge=%s, protocol=%s, light=%s, critic=%s, api_base=%s",
    ROUTER_MODEL, DATA_PARSER_MODEL, RADIO_MODEL, KNOWLEDGE_MODEL,
    PROTOCOL_MODEL, LIGHT_MODEL, CRITIC_MODEL, API_BASE,
)


# ===================== 1. 通用工程能力：超时、耗时、异常 =====================
def run_with_timeout(func: Callable[[], Any], timeout: float, task_name: str) -> Any:
    """守护线程方式执行任务，超时立即返回；Python 无法安全强杀后台线程。"""
    if timeout <= 0:
        return func()

    result_queue: queue.Queue = queue.Queue(maxsize=1)

    def worker():
        try:
            result_queue.put((True, func()))
        except Exception as exc:
            result_queue.put((False, exc))

    thread = threading.Thread(target=worker, name=f"timeout-{task_name}", daemon=True)
    thread.start()
    try:
        ok, value = result_queue.get(timeout=timeout)
    except queue.Empty as exc:
        logger.warning("%s 超时，后台线程可能仍在运行（%gs）", task_name, timeout)
        raise TimeoutError(f"{task_name} 超时（>{timeout:g}秒）") from exc
    if not ok:
        raise value
    return value


def call_llm(model: ChatOpenAI, prompt: str, name: str) -> str:
    """带超时和异常捕获的 LLM 调用。"""
    try:
        response = run_with_timeout(lambda: model.invoke(prompt), LLM_TIMEOUT_SECONDS, name)
        content = getattr(response, "content", "")
        return content.strip() if isinstance(content, str) else str(content).strip()
    except Exception as exc:
        logger.exception("%s 调用失败：%s", name, exc)
        raise


def message_content(message: Any) -> str:
    """兼容 AIMessage / dict 的最终文本提取。"""
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        return "".join(parts).strip()
    return str(content or "").strip()


def with_timing_and_error(name: str, error_builder: Callable[[Exception], dict]):
    """节点级耗时统计与异常捕获装饰器。"""

    def decorator(func):
        @wraps(func)
        def wrapper(state):
            start = time.perf_counter()
            logger.info("%s开始", name)
            try:
                return func(state)
            except Exception as exc:
                logger.exception("%s失败：%s", name, exc)
                return error_builder(exc)
            finally:
                logger.info("%s耗时=%.2fs", name, time.perf_counter() - start)

        return wrapper

    return decorator


# ===================== 2. 工具 A：QXDM txt / isf / hdf 日志解析 =====================
def _read_qxdm_text(path: Path) -> str:
    """优先按文本读取，兼容 UTF-8 / GBK / 忽略不可打印字符。"""
    if not path.is_file():
        raise RuntimeError(f"无法读取日志文件：{path}（不是文件）")
    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            return path.read_text(encoding=encoding, errors="ignore")
        except Exception:
            continue
    raise RuntimeError(f"无法读取日志文件：{path}")


QXDM_FILE_PATTERNS = ("*.txt", "*.log", "*.isf", "*.isfx", "*.bin", "*.hdf", "*.hdf5", "*.h5")
MAX_QXDM_FILES = 50


def _expand_qxdm_paths(path: Path, base_dir: Path) -> list[Path]:
    """支持具体文件、目录（递归扫描）和通配符表达式。"""
    raw = str(path)
    if any(ch in raw for ch in "*?["):
        parent = path.parent if path.parent.exists() else base_dir
        return sorted(p for p in parent.glob(path.name) if p.is_file())[:MAX_QXDM_FILES]
    if path.is_dir():
        found: set[Path] = set()
        for pattern in QXDM_FILE_PATTERNS:
            found.update(p for p in path.rglob(pattern) if p.is_file())
        return sorted(found)[:MAX_QXDM_FILES]
    if path.is_file():
        return [path]
    return []


def _parse_single_qxdm_file(path: Path, keyword: str, max_lines: int) -> str:
    """解析单个 QXDM 文件，返回带文件名的结果片段。"""
    try:
        suffix = path.suffix.lower()
        if suffix in {".hdf", ".hdf5", ".h5"}:
            return _parse_hdf_log(path, keyword, max_lines)

        text = _read_qxdm_text(path)
        matches = [
            (idx, line.strip())
            for idx, line in enumerate(text.splitlines(), start=1)
            if keyword in line.upper()
        ]
        if matches:
            preview = "\n".join(f"L{idx}: {line}" for idx, line in matches[:max_lines])
            return (
                f"【QXDM文本解析】文件={path.name}, 关键词={keyword}, "
                f"命中={len(matches)}行\n{preview}"
            )
        if suffix in {".isf", ".isfx", ".bin"}:
            return _try_qxdm_parser(path, keyword)
        return f"【QXDM文本解析】文件={path.name}, 关键词={keyword}, 未命中相关信令"
    except Exception as exc:
        logger.exception("QXDM单文件解析失败：%s", exc)
        return f"QXDM日志解析失败：{type(exc).__name__}: {exc}"


def _try_qxdm_parser(log_path: Path, keyword: str) -> str:
    """可选分支：尝试调用外部 qxdm-parser 模块解析 .isf 原始文件。"""
    module = None
    for module_name in ("qxdm_parser", "qxdmparser", "qxdmParser"):
        try:
            module = importlib.import_module(module_name)
            break
        except ImportError:
            continue
    if module is None:
        return "未检测到 qxdm-parser 模块，请安装后再解析原始 .isf 文件"

    parser = None
    for attr in ("parse", "parse_log", "parse_isf", "QXDMParser"):
        candidate = getattr(module, attr, None)
        if callable(candidate):
            parser = candidate
            break
    if parser is None:
        return "qxdm-parser 模块可用，但未找到可调用的解析函数"

    try:
        result = parser(str(log_path))
        text = str(result)
        hits = [line for line in text.splitlines() if keyword.upper() in line.upper()]
        preview = "\n".join(hits[:50]) if hits else text[:2000]
        return f"【qxdm-parser解析结果】关键词={keyword}\n{preview}"
    except Exception as exc:
        logger.exception("qxdm-parser 解析失败：%s", exc)
        return f"qxdm-parser 解析失败：{type(exc).__name__}: {exc}"


def _decode_qxdm_bytes(value: bytes) -> str:
    """按常见编码解码 HDF 中的字节串。"""
    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            return value.decode(encoding)
        except UnicodeDecodeError:
            continue
    return value.decode("utf-8", errors="ignore")


def _iter_hdf_lines(value: Any):
    """递归展开 HDF5 数据集，兼容标量、数组、结构化数组和嵌套字段。"""
    if isinstance(value, bytes):
        text = _decode_qxdm_bytes(value)
        for line in text.splitlines() or [text]:
            yield line
        return

    if isinstance(value, str):
        for line in value.splitlines() or [value]:
            yield line
        return

    dtype = getattr(value, "dtype", None)
    if dtype is not None and getattr(dtype, "names", None):
        for field in dtype.names:
            yield from _iter_hdf_lines(value[field])
        return

    if hasattr(value, "reshape") and hasattr(value, "__len__"):
        for item in value.reshape(-1):
            yield from _iter_hdf_lines(item)
        return

    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_hdf_lines(item)
        return

    if value is not None:
        yield str(value)


def _parse_hdf_log(log_path: Path, keyword: str, max_lines: int) -> str:
    """解析 HDF5 格式 QXDM 日志；非 HDF5 文件回退到文本扫描。"""
    try:
        import h5py
    except ImportError:
        return "未检测到 h5py 模块，无法解析 HDF 日志；请安装 h5py 后重试"

    keyword_upper = keyword.upper()

    if not h5py.is_hdf5(log_path):
        try:
            text = _read_qxdm_text(log_path)
            matches = [
                (idx, line.strip())
                for idx, line in enumerate(text.splitlines(), start=1)
                if keyword_upper in line.upper()
            ]
            if matches:
                preview = "\n".join(f"L{idx}: {line}" for idx, line in matches[:max_lines])
                return (
                    f"【HDF兼容文本解析】文件={log_path.name}, 关键词={keyword}, "
                    f"非HDF5格式但文本回退命中={len(matches)}行\n{preview}"
                )
            return (
                f"【HDF解析】文件={log_path.name} 不是HDF5格式，且文本回退未命中关键词；"
                "可能是HDF4或二进制导出，建议转换为HDF5或导出txt"
            )
        except Exception as exc:
            return f"HDF兼容文本回退失败：{type(exc).__name__}: {exc}"

    matches = []
    scanned = 0

    try:
        with h5py.File(log_path, "r") as h5file:
            def visit(name, obj):
                nonlocal scanned
                if len(matches) >= max_lines or not isinstance(obj, h5py.Dataset):
                    return
                try:
                    raw = obj[()]
                except Exception as exc:
                    logger.warning("HDF数据集读取失败：%s，错误=%s", name, exc)
                    return
                for line_no, line in enumerate(_iter_hdf_lines(raw), start=1):
                    scanned += 1
                    if keyword_upper in line.upper():
                        matches.append((f"{name}[{line_no}]", line.strip()))
                        if len(matches) >= max_lines:
                            return

            h5file.visititems(visit)
    except Exception as exc:
        logger.exception("HDF日志解析失败：%s", exc)
        return f"HDF日志解析失败：{type(exc).__name__}: {exc}"

    if not matches:
        return (
            f"【HDF解析】文件={log_path.name}, 关键词={keyword}, "
            f"共扫描={scanned}项，未命中相关信令"
        )
    preview = "\n".join(f"{loc}: {line}" for loc, line in matches[:max_lines])
    return (
        f"【HDF解析】文件={log_path.name}, 关键词={keyword}, "
        f"命中={len(matches)}项，扫描={scanned}项\n{preview}"
    )


@tool
def parse_qxdm_log(log_path: str, keyword: str = "PRACH", max_lines: int = 50) -> str:
    """解析 QXDM 日志，支持 txt / isf / hdf / dlf / qmdl 格式。
    专有二进制格式（hdf/isf/dlf/qmdl）用 QCAT 命令行导出结构化KPI分析（RSRP/SINR/BLER/吞吐等，
    临时文件用完即删），避免全量转文本导致GB级文件；txt 用关键词检索。
    Args:
        log_path: 文件路径、目录路径或通配符表达式；目录会递归扫描支持的格式。
        keyword: txt 检索关键词（hdf 等专有格式走结构化导出，此参数不生效）。
        max_lines: txt 每文件最多返回的命中行数。
    """
    try:
        raw = Path(log_path).expanduser()
        path = raw if raw.is_absolute() else BASE_DIR / raw
        keyword = keyword.upper()
        files = _expand_qxdm_paths(path, BASE_DIR)
        if not files:
            return f"日志文件不存在：{path}（支持具体文件、目录或通配符，格式：txt/isf/hdf）"

        sections = []
        qcat_formats = {".hdf", ".isf", ".dlf", ".qmdl"}
        for file_path in files:
            suffix = file_path.suffix.lower()
            # 专有二进制格式：优先用 QCAT -export 导出结构化KPI（体积极小、用完即删）
            if suffix in qcat_formats and qcat_adapter.is_available():
                logger.info("QCAT导出QXDM专有格式：%s", file_path.name)
                sections.append(
                    f"--- {file_path} ---\n"
                    + qcat_adapter.export_qxdm(str(file_path))
                )
            else:
                sections.append(
                    f"--- {file_path} ---\n"
                    + _parse_single_qxdm_file(file_path, keyword, max_lines)
                )
        return "\n\n".join(sections)[:30000]
    except Exception as exc:
        logger.exception("QXDM日志解析工具失败：%s", exc)
        return f"QXDM日志解析失败：{type(exc).__name__}: {exc}"


# ===================== 3. 工具 B：LTE/NR 通信参数计算 =====================
NCS_UNRESTRICTED = {
    0: 0, 1: 13, 2: 15, 3: 18, 4: 22, 5: 26, 6: 32, 7: 38,
    8: 46, 9: 59, 10: 76, 11: 93, 12: 119, 13: 167, 14: 279, 15: 419,
}
NCS_RESTRICTED = {
    0: 15, 1: 18, 2: 22, 3: 26, 4: 32, 5: 38, 6: 46, 7: 55,
    8: 68, 9: 82, 10: 100, 11: 128, 12: 158, 13: 202, 14: 237,
}


@tool
def lte_nr_calc(
    calc_type: str,
    prach_format: str = "format1",
    ncs: int = 14,
    ncs_is_index: bool = True,
    ncs_set: str = "unrestricted",
    delay_spread_us: float = 0.0,
    freq_ghz: float = 2.6,
    speed_kmh: float = 300,
    bandwidth_mhz: float = 20.0,
    modulation: str = "256qam",
    mimo_layers: int = 4,
    overhead: float = 0.25,
) -> str:
    """LTE/NR通信参数计算：PRACH 半径、多普勒频偏、下行峰值吞吐量。
    Args:
        calc_type: prach_radius / doppler / throughput。
        prach_format: 当前仅支持 format1。
        ncs: NCS配置索引或实际 N_CS 样本数。
        ncs_is_index: ncs 是否为配置索引，默认 True。
        ncs_set: unrestricted 或 restricted。
        delay_spread_us: 时延扩展，单位微秒。
        freq_ghz: 载波频率，单位 GHz。
        speed_kmh: UE速度，单位 km/h。
        bandwidth_mhz: throughput 用，系统带宽 MHz（1.4/3/5/10/15/20）。
        modulation: throughput 用，qpsk/16qam/64qam/256qam。
        mimo_layers: throughput 用，下行空间流数（1~8）。
        overhead: throughput 用，控制信道+参考信号+导频开销比例（0~1）。
    """
    try:
        calc_type = calc_type.lower()
        prach_format = prach_format.lower()
        if calc_type == "prach_radius":
            if prach_format != "format1":
                return f"暂不支持 {prach_format} 的半径计算，请使用 format1"

            if ncs_is_index:
                table = NCS_RESTRICTED if ncs_set.lower().startswith("r") else NCS_UNRESTRICTED
                if ncs not in table:
                    return f"NCS配置索引 {ncs} 不在 TS36.211 Table 5.7.2-2 范围内"
                ncs_value = table[ncs] or 839
                ncs_desc = f"NCS配置索引={ncs}({ncs_set}), 实际N_CS={ncs_value}"
            else:
                ncs_value = ncs
                ncs_desc = f"N_CS={ncs_value}"

            sample_duration_us = 1000.0 / (839 * 1.25)
            usable_time_us = ncs_value * sample_duration_us - delay_spread_us
            if usable_time_us <= 0:
                return f"{ncs_desc} 小于时延扩展，无法支持可靠接入"
            radius_m = usable_time_us * 300.0 / 2.0
            return (
                f"PRACH Format1 {ncs_desc}, 时延扩展={delay_spread_us}us, "
                f"NCS受限理论覆盖半径 {radius_m:.0f} 米（约{radius_m / 1000:.2f} km，依据TS36.211 Table 5.7.2-2）"
            )

        if calc_type == "doppler":
            fd = (speed_kmh / 3.6) * freq_ghz * 1e9 / 3e8
            return f"多普勒频偏：{fd:.2f} Hz（freq={freq_ghz}GHz, speed={speed_kmh}km/h）"

        if calc_type == "throughput":
            # 标准带宽 -> 下行RB数（TS36.101 Table 5.1-1）
            rb_map = {1.4: 6, 3: 15, 5: 25, 10: 50, 15: 75, 20: 100}
            if bandwidth_mhz not in rb_map:
                closest = min(rb_map, key=lambda b: abs(b - bandwidth_mhz))
                return (
                    f"带宽 {bandwidth_mhz}MHz 非LTE标准带宽；"
                    f"标准值为 {sorted(rb_map)}MHz，最接近 {closest}MHz（{rb_map[closest]} RB）"
                )
            rb = rb_map[bandwidth_mhz]
            mod = modulation.lower()
            bits_per_symbol = {"qpsk": 2, "16qam": 4, "64qam": 6, "256qam": 8}.get(mod)
            if bits_per_symbol is None:
                return f"不支持的调制方式 {modulation}，可选 qpsk/16qam/64qam/256qam"
            if not 1 <= mimo_layers <= 8:
                return f"mimo_layers={mimo_layers} 超出范围（1~8）"
            if not 0 <= overhead < 1:
                return f"overhead={overhead} 超出范围（0~1）"

            subcarriers_per_rb = 12
            ofdm_symbols_per_subframe = 14  # normal CP
            subframes_per_sec = 1000
            # 峰值速率(Mbps) = RB × 12 × 14 × bits × layers × 1000 / 1e6 × (1-overhead)
            peak_mbps = (
                rb * subcarriers_per_rb * ofdm_symbols_per_subframe
                * bits_per_symbol * mimo_layers * subframes_per_sec
                / 1e6 * (1.0 - overhead)
            )
            return (
                f"LTE下行峰值吞吐量：{peak_mbps:.1f} Mbps "
                f"（带宽={bandwidth_mhz}MHz/{rb}RB, 调制={mod.upper()}/{bits_per_symbol}bit/sym, "
                f"{mimo_layers}流MIMO, normal CP 14符号/子帧, 开销={overhead:.0%}, "
                f"依据TS36.101/36.211物理层资源计算）"
            )

        return "参数错误：calc_type 仅支持 prach_radius / doppler / throughput"
    except Exception as exc:
        logger.exception("通信参数计算工具失败：%s", exc)
        return f"计算失败：{type(exc).__name__}: {exc}"


# ===================== 4. 工具 C：本地技术资料 RAG 向量库（PDF / DOCX / DOC） =====================
class HashingEmbeddings(Embeddings):
    """离线确定性哈希向量，保证无外部 Embedding 服务时仍可运行 Chroma。"""

    def __init__(self, dimension: int = 512):
        self.dimension = dimension

    def _tokens(self, text: str) -> list[str]:
        text = text.lower()
        tokens = re.findall(r"[\u4e00-\u9fff]|[a-z0-9_]+", text)
        bigrams = [a + b for a, b in zip(tokens, tokens[1:])]
        return tokens + bigrams

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vector = [0.0] * self.dimension
            for token in self._tokens(text):
                digest = hashlib.md5(token.encode("utf-8")).digest()
                value = int.from_bytes(digest[:8], "big")
                idx = value % self.dimension
                vector[idx] += 1.0 if value & 1 else -1.0
            norm = sum(v * v for v in vector) ** 0.5 or 1.0
            vectors.append([v / norm for v in vector])
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


class APIBasedEmbeddings(Embeddings):
    """调用 OpenAI 兼容 /embeddings 端点（tokenhub kinfra）生成语义向量。

    相比 HashingEmbeddings 的词袋哈希，这是真正的语义向量，能匹配
    "峰值吞吐量"↔"下行峰值速率"这类同义表述。不可用时可回退到哈希方案。
    """

    def __init__(self, model: str, api_key: str, api_base: str, timeout: float = 60):
        self.model = model
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.dimension: int | None = None
        self._fallback = HashingEmbeddings()
        self._client = httpx.Client(trust_env=False, timeout=timeout)

    def _embed(self, texts: list[str]) -> list[list[float]]:
        resp = self._client.post(
            f"{self.api_base}/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "input": texts},
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        vectors = [item["embedding"] for item in sorted(data, key=lambda d: d.get("index", 0))]
        if vectors and self.dimension is None:
            self.dimension = len(vectors[0])
        return vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # 分批避免单次请求过大
        results: list[list[float]] = []
        batch_size = 32
        for start in range(0, len(texts), batch_size):
            batch = texts[start:start + batch_size]
            results.extend(self._embed(batch))
        return results

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text])[0]


def build_embeddings() -> Embeddings:
    """按配置构建 embedding：auto/api 优先用远端语义向量，失败回退哈希。"""
    mode = os.getenv("RAG_EMBEDDING_MODE", "auto").strip().lower()
    if mode == "hash":
        return HashingEmbeddings()
    model = os.getenv("RAG_EMBEDDING_MODEL", "kinfra-text-embedding-0.6b")
    try:
        emb = APIBasedEmbeddings(model, API_KEY, API_BASE)
        # 探测一次，确认服务可用且维度有效
        probe = emb.embed_query("LTE 随机接入")
        if not probe or len(probe) < 8:
            raise ValueError("embedding 维度异常")
        logger.info("RAG Embedding 使用 %s（维度=%d）", model, len(probe))
        return emb
    except Exception as exc:
        if mode == "api":
            raise RuntimeError(f"RAG_EMBEDDING_MODE=api 但 embedding 不可用：{exc}") from exc
        logger.warning("远端 Embedding 不可用（%s），回退到 HashingEmbeddings", exc)
        return HashingEmbeddings()


def split_text(text: str, chunk_size: int = 1200, overlap: int = 200) -> list[str]:
    """递归切分文档文本：优先在段落/换行/句子边界断开，减少条款与表格被截断。

    失败时回退到简单的字符窗口切分，保证任何输入都能处理。
    """
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter

        splitter = RecursiveCharacterTextSplitter(
            separators=["\n\n", "\n", "。", ". ", "；", ";", " ", ""],
            chunk_size=chunk_size,
            chunk_overlap=overlap,
            length_function=len,
        )
        chunks = [c.strip() for c in splitter.split_text(text) if c.strip()]
        if chunks:
            return chunks
    except Exception as exc:
        logger.warning("RecursiveCharacterTextSplitter 不可用（%s），回退字符切分", exc)
    # 回退：字符窗口
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end == len(text):
            break
        start = end - overlap
    return chunks


def _clean_extracted_text(text: str) -> str:
    """清理 Word/PDF 提取文本中的控制字符和多余空行。"""
    if not text:
        return ""
    text = text.replace("\x00", "").replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _load_docx_text(path: Path) -> str:
    """python-docx 读取 .docx 段落与表格。"""
    from docx import Document as WordDocument

    document = WordDocument(str(path))
    parts = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return _clean_extracted_text("\n".join(parts))


def _load_doc_text(path: Path) -> str:
    """读取旧版 .doc：antiword/LibreOffice 优先，Word COM 次之，二进制兜底。"""
    tmp_doc: Path | None = None
    antiword = shutil.which("antiword")
    if antiword:
        try:
            proc = subprocess.run(
                [antiword, str(path)],
                capture_output=True,
                text=True,
                errors="ignore",
                timeout=60,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return _clean_extracted_text(proc.stdout)
        except Exception as exc:
            logger.warning("antiword 读取 .doc 失败：%s", exc)

    soffice = shutil.which("soffice") or shutil.which("soffice.exe")
    if soffice:
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                subprocess.run(
                    [soffice, "--headless", "--convert-to", "txt:Text", "--outdir", tmp_dir, str(path)],
                    capture_output=True,
                    text=True,
                    errors="ignore",
                    timeout=120,
                )
                txt_files = list(Path(tmp_dir).glob("*.txt"))
                if txt_files:
                    return _clean_extracted_text(txt_files[0].read_text(encoding="utf-8", errors="ignore"))
        except Exception as exc:
            logger.warning("LibreOffice 读取 .doc 失败：%s", exc)

    text = ""
    try:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        # 复制到临时路径再打开：避免文档已被 WPS 打开时 Documents.Open 报"文档打开失败"
        try:
            import tempfile

            tmp_dir = tempfile.mkdtemp(prefix="rag_doc_")
            tmp_doc = Path(tmp_dir) / path.name
            shutil.copy2(str(path), str(tmp_doc))
            open_path = str(tmp_doc)
        except Exception:
            open_path = str(path)

        # Dispatch(复用已有实例) 与 DispatchEx(新实例) 都尝试；WPS/Word 偶发
        # 实例冲突会导致某一种打开失败，换另一种通常可成功。
        for dispatch_name in ("Dispatch", "DispatchEx"):
            word = None
            doc = None
            try:
                word = getattr(win32com.client, dispatch_name)("Word.Application")
                word.Visible = False
                word.DisplayAlerts = 0
                doc = word.Documents.Open(
                    open_path,
                    ReadOnly=True,
                    AddToRecentFiles=False,
                    ConfirmConversions=False,
                )
                text = doc.Content.Text or ""
                # 表格在 Content.Text 中会丢失单元格边界；补一段 Markdown 管道表，
                # 让 3GPP 参数表（RB数/MCS/TBS 等）保留行列对应关系，便于 RAG 命中。
                table_blocks: list[str] = []
                try:
                    n_tables = min(int(doc.Tables.Count), 200)
                    for t_idx in range(1, n_tables + 1):
                        table = doc.Tables(t_idx)
                        rows: list[str] = []
                        for r_idx in range(1, min(int(table.Rows.Count), 60) + 1):
                            cells = []
                            for c_idx in range(1, min(int(table.Columns.Count), 20) + 1):
                                cell_text = table.Cell(r_idx, c_idx).Range.Text or ""
                                cells.append(cell_text.replace("\r", " ").replace("\x07", "").strip())
                            if any(cells):
                                rows.append("| " + " | ".join(cells) + " |")
                        if rows:
                            table_blocks.append(f"[表格{t_idx}]\n" + "\n".join(rows))
                except Exception as tbl_exc:
                    logger.warning("Word 表格提取部分失败：%s", tbl_exc)
                if table_blocks:
                    text += "\n\n" + "\n\n".join(table_blocks)
                    logger.info("Word 表格已提取 %d 个用于索引", len(table_blocks))
                break  # 成功即停止尝试
            except Exception as exc:
                logger.warning("Word COM(%s) 读取 .doc 失败：%s", dispatch_name, exc)
            finally:
                if doc is not None:
                    try:
                        doc.Close(False)
                    except Exception:
                        pass
                if word is not None:
                    # Dispatch 复用实例时不 Quit，避免关闭用户正在用的 WPS
                    if dispatch_name == "DispatchEx":
                        try:
                            word.Quit()
                        except Exception:
                            pass
        pythoncom.CoUninitialize()
    except Exception as exc:
        logger.warning("Word COM 初始化失败：%s", exc)
    if tmp_doc is not None:
        shutil.rmtree(tmp_doc.parent, ignore_errors=True)
    if text.strip():
        return _clean_extracted_text(text)

    data = path.read_bytes()
    text = ""
    encodings = ("utf-16-le", "gbk", "latin-1") if data[:2] == b"\xff\xfe" else ("gbk", "utf-16-le", "latin-1")
    for encoding in encodings:
        decoded = data.decode(encoding, errors="ignore")
        sample = decoded[:1000]
        printable_ratio = sum(ch.isprintable() for ch in sample) / len(sample) if sample else 0.0
        if len(sample.strip()) > 10 and printable_ratio > 0.7:
            text = decoded
            break
    cleaned = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9\s\.,;:()\[\]{}<>/'\-_]", " ", text)
    cleaned = _clean_extracted_text(cleaned)
    if len(cleaned) > 20:
        logger.warning("%s 使用二进制兜底读取 .doc，内容质量可能下降", path)
        return cleaned
    return ""


def _strip_html_text(raw_html: str) -> str:
    """去脚本/样式/标签，保留可读正文。"""
    text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>|<noscript.*?</noscript>", " ", raw_html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _pdf_page_text(path: Path, page_no: int) -> str | None:
    """用 pymupdf 的 html 选项提取单页文本，保留表格结构；不可用时返回 None。"""
    try:
        import pymupdf

        doc = pymupdf.open(str(path))
        try:
            if page_no >= len(doc):
                return None
            html = doc.get_page_text(page_no, option="html")
            text = _strip_html_text(html)
            return _clean_extracted_text(text) or None
        finally:
            doc.close()
    except Exception:
        return None


def load_local_documents(doc_dirs: Path | Sequence[Path]) -> tuple[list[Document], list[str]]:
    """加载本地技术资料，支持 pdf / docx / doc，不限制文档主题。"""
    if isinstance(doc_dirs, (str, Path)):
        doc_dirs = [Path(doc_dirs)]

    documents: list[Document] = []
    ids: list[str] = []

    pdf_reader_cls = None
    try:
        from pypdf import PdfReader

        pdf_reader_cls = PdfReader
    except Exception as exc:
        logger.exception("pypdf 导入失败，将跳过 PDF：%s", exc)

    doc_paths: list[Path] = []
    for doc_dir in doc_dirs:
        for pattern in ("*.pdf", "*.PDF", "*.docx", "*.DOCX", "*.doc", "*.DOC",
                        "*.md", "*.MD", "*.txt", "*.TXT"):
            doc_paths.extend(sorted(doc_dir.glob(pattern)))

    seen: set[Path] = set()
    for doc_path in doc_paths:
        resolved = doc_path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)

        suffix = doc_path.suffix.lower()

        def append_chunks(text: str, page_no: int, doc_type: str) -> None:
            for chunk_no, chunk in enumerate(split_text(text), start=1):
                documents.append(
                    Document(
                        page_content=chunk,
                        metadata={
                            "source": doc_path.name,
                            "path": str(doc_path),
                            "page": page_no,
                            "chunk": chunk_no,
                            "doc_type": doc_type,
                        },
                    )
                )
                ids.append(
                    f"{doc_path.parent.name}-{doc_path.stem}-{doc_type}-p{page_no}-c{chunk_no}"
                )

        try:
            before = len(documents)
            if suffix == ".pdf":
                n_pages = 0
                try:
                    import pymupdf

                    pdf = pymupdf.open(str(doc_path))
                    n_pages = len(pdf)
                    pdf.close()
                except Exception:
                    n_pages = 0
                # 优先 pymupdf html（保留表格），逐页失败回退 pypdf
                used_pymupdf = False
                if n_pages and pdf_reader_cls is not None:
                    for page_no in range(1, n_pages + 1):
                        page_text = _pdf_page_text(doc_path, page_no - 1)
                        if page_text:
                            append_chunks(page_text, page_no, "pdf")
                            used_pymupdf = True
                if not used_pymupdf and pdf_reader_cls is not None:
                    reader = pdf_reader_cls(str(doc_path))
                    for page_no, page in enumerate(reader.pages, start=1):
                        append_chunks(page.extract_text() or "", page_no, "pdf")
                    n_pages = len(reader.pages)
                logger.info("已加载PDF：%s，页数=%d（%s）", doc_path.name, n_pages,
                            "pymupdf" if used_pymupdf else "pypdf")
            elif suffix == ".docx":
                append_chunks(_load_docx_text(doc_path), 1, "docx")
                logger.info("已加载DOCX：%s", doc_path.name)
            elif suffix == ".doc":
                append_chunks(_load_doc_text(doc_path), 1, "doc")
                logger.info("已加载DOC：%s", doc_path.name)
            elif suffix in (".md", ".markdown", ".txt"):
                append_chunks(doc_path.read_text(encoding="utf-8", errors="ignore"), 1, suffix[1:])
                logger.info("已加载经验文档：%s", doc_path.name)
            if len(documents) == before:
                logger.warning("文档未提取到可索引文本：%s", doc_path)
        except Exception as exc:
            logger.exception("技术文档加载失败：%s，错误=%s", doc_path, exc)

    return documents, ids


load_3gpp_documents = load_local_documents


def build_protocol_retriever():
    """构建/加载 Chroma 向量库；无可用文档或构建失败时返回 None。

    collection 名带 embedding 维度后缀，避免切换 embedding 后与旧维度集合冲突。
    """
    documents, ids = load_local_documents(RAG_DOC_DIRS)
    if not documents:
        logger.warning(
            "未在 %s 发现可解析的技术文档（pdf/doc/docx），RAG不可用",
            ", ".join(str(p) for p in RAG_DOC_DIRS),
        )
        return None
    try:
        embeddings = build_embeddings()
        dim = getattr(embeddings, "dimension", None) or len(embeddings.embed_query("维度探测"))
        collection_name = f"{RAG_COLLECTION_NAME}_e{dim}"
        vectorstore = Chroma.from_documents(
            documents=documents,
            embedding=embeddings,
            ids=ids,
            collection_name=collection_name,
            persist_directory=str(CHROMA_DIR),
        )
        logger.info("Chroma向量库构建完成：docs=%d, collection=%s, dir=%s",
                    len(documents), collection_name, CHROMA_DIR)
        return vectorstore.as_retriever(search_kwargs={"k": 4})
    except Exception as exc:
        logger.exception("Chroma向量库构建失败：%s", exc)
        return None


protocol_retriever = build_protocol_retriever()


@tool
def rag_protocol_query(query: str, top_k: int = 4) -> str:
    """检索本地技术资料片段，来源支持 pdf / docx / doc，不限制主题。
    Args:
        query: 检索问题，例如 PRACH NCS配置、测试脚本规范或任意本地资料中的问题。
        top_k: 返回片段数量。
    """
    try:
        if protocol_retriever is None:
            dirs = ", ".join(str(p) for p in RAG_DOC_DIRS)
            return (
                "RAG不可用：未检索到本地技术文档。"
                f"请将 pdf/doc/docx 文件放入 {dirs} 后重启程序；禁止编造文档内容。"
            )
        retriever = protocol_retriever
        search_kwargs = getattr(retriever, "search_kwargs", {})
        search_kwargs["k"] = top_k
        docs = retriever.invoke(query)
        if not docs:
            return "未在本地技术资料向量库中检索到相关片段，禁止编造文档内容。"
        parts = []
        for idx, doc in enumerate(docs, start=1):
            meta = doc.metadata or {}
            source = meta.get("source", "unknown")
            page = meta.get("page", "?")
            parts.append(f"[片段{idx}] 来源={source}, 页码={page}\n{doc.page_content}")
        return "\n\n".join(parts)
    except Exception as exc:
        logger.exception("RAG检索工具失败：%s", exc)
        return f"RAG检索失败：{type(exc).__name__}: {exc}"


# ===================== 5. 工具 D：联网 Web RAG 检索 =====================
WEB_RAG_TIMEOUT_SECONDS = _env_float("WEB_RAG_TIMEOUT", 10)
# 外网不可用时熔断：首次连续失败后，短时间内不再尝试联网，避免每轮浪费数十秒
WEB_RAG_DISABLE_SECONDS = _env_float("WEB_RAG_DISABLE_SECONDS", 300)
_web_unavailable_until: float = 0.0
WEB_RAG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; LTE-NR-Agent/1.0; WebRAG)",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _normalize_search_url(href: str) -> str:
    href = html_lib.unescape(href).strip()
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com"):
        target = parse_qs(parsed.query).get("uddg", [None])[0]
        if target:
            return unquote(target)
    return href


def _web_search_results(query: str, max_results: int):
    """先用 DuckDuckGo HTML，失败再用 Bing HTML；返回标题/链接/摘要。"""
    results = []
    with httpx.Client(
        trust_env=False,
        timeout=WEB_RAG_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers=WEB_RAG_HEADERS,
    ) as client:
        try:
            resp = client.post("https://html.duckduckgo.com/html/", data={"q": query})
            resp.raise_for_status()
            titles = re.findall(
                r'class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                resp.text,
                re.S,
            )
            snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', resp.text, re.S)
            for idx, (href, title) in enumerate(titles[:max_results]):
                results.append({
                    "title": _strip_html_text(title),
                    "url": _normalize_search_url(href),
                    "snippet": _strip_html_text(snippets[idx]) if idx < len(snippets) else "",
                })
        except Exception as exc:
            logger.warning("DuckDuckGo检索失败，尝试Bing：%s", exc)

        if not results:
            try:
                resp = client.get("https://www.bing.com/search", params={"q": query})
                resp.raise_for_status()
                matches = re.findall(
                    r'<li class="b_algo".*?<h2><a href="([^"]+)"[^>]*>(.*?)</a>.*?<p[^>]*>(.*?)</p>',
                    resp.text,
                    re.S,
                )
                for href, title, snippet in matches[:max_results]:
                    results.append({
                        "title": _strip_html_text(title),
                        "url": _normalize_search_url(href),
                        "snippet": _strip_html_text(snippet),
                    })
            except Exception as exc:
                logger.warning("Bing检索失败：%s", exc)

    global _web_unavailable_until
    if not results:
        # 两个搜索引擎都失败 => 外网大概率不可达，熔断一段时间
        _web_unavailable_until = time.time() + WEB_RAG_DISABLE_SECONDS
        logger.warning("外网检索不可用，熔断 %.0fs（本地资料可用时不会等待联网）", WEB_RAG_DISABLE_SECONDS)
    return results


@tool
def web_rag_search(query: str, top_k: int = 5) -> str:
    """联网检索网页并按相关性返回文本片段，供Web RAG专家基于证据回答。
    Args:
        query: 联网检索问题。
        top_k: 返回的高相关片段数量。
    """
    if time.time() < _web_unavailable_until:
        return "外网检索当前不可用（已熔断），请改用本地资料工具 rag_protocol_query；禁止编造网页内容。"
    try:
        search_results = _web_search_results(query, max_results=max(top_k * 2, 6))
        if not search_results:
            return "联网检索失败或未返回结果，禁止编造网页内容。"

        documents = []
        with httpx.Client(
            trust_env=False,
            timeout=WEB_RAG_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers=WEB_RAG_HEADERS,
        ) as client:
            for item in search_results:
                url = item.get("url", "")
                if not url.startswith("http"):
                    continue
                text = item.get("snippet", "")
                try:
                    resp = client.get(url)
                    resp.raise_for_status()
                    page_text = _strip_html_text(resp.text)
                    if len(page_text) > len(text):
                        text = page_text[:20000]
                except Exception as exc:
                    logger.warning("网页正文抓取失败，使用搜索摘要：%s，错误=%s", url, exc)
                documents.append({**item, "text": text})

        if not documents:
            return "联网检索结果无有效网页，禁止编造网页内容。"

        embeddings = HashingEmbeddings()
        query_vector = embeddings.embed_query(query)
        candidates = []
        for doc in documents:
            for chunk_no, chunk in enumerate(split_text(doc["text"], 1000, 150)[:20], start=1):
                vector = embeddings.embed_query(chunk)
                score = sum(a * b for a, b in zip(query_vector, vector))
                candidates.append((score, doc, chunk_no, chunk))

        if not candidates:
            return "联网检索未获得可索引文本，禁止编造网页内容。"

        candidates.sort(key=lambda item: item[0], reverse=True)
        parts = []
        for idx, (score, doc, chunk_no, chunk) in enumerate(candidates[:top_k], start=1):
            parts.append(
                f"[联网片段{idx}] 标题={doc.get('title', 'unknown')}\n"
                f"来源={doc.get('url', 'unknown')}\n"
                f"相关度={score:.4f}, 分块={chunk_no}\n"
                f"{chunk}"
            )
        return "\n\n".join(parts)
    except Exception as exc:
        logger.exception("Web RAG检索失败：%s", exc)
        return f"Web RAG检索失败：{type(exc).__name__}: {exc}"


# ===================== 5b. DataParser 多数据源解析工具（委托 data_sources） =====================
@tool
def detect_data_sources(query: str = "", log_dir: str = "", config_dir: str = "", doc_dir: str = "") -> str:
    """探测本次任务可见的数据源：日志、WebLMT配置、经验文档。返回JSON标记与命中文件路径。
    Args:
        query: 用户原始问题，可包含文件路径线索。
        log_dir: 日志目录；留空则用默认 artifacts/qxdm。
        config_dir: 配置目录；留空则用默认 docs。
        doc_dir: 经验文档目录；留空则用默认 docs。
    """
    try:
        return json.dumps(
            data_sources.detect_data_sources(query, log_dir=log_dir, config_dir=config_dir, doc_dir=doc_dir),
            ensure_ascii=False,
        )
    except Exception as exc:
        return f"数据源探测失败：{type(exc).__name__}: {exc}"


@tool
def parse_weblmt_config(path: str, keyword: str = "", max_rows: int = 200) -> str:
    """解析 WebLMT 基站配置导出文件（csv/xlsx/xls/json/txt），提取小区、频点、功率、PRACH、定时器、邻区等参数。
    Args:
        path: 配置文件或目录路径。
        keyword: 可选关键字过滤，如 PRACH、邻区、频点。
        max_rows: 最多返回行数。
    """
    return data_sources.parse_weblmt_config(path, keyword, max_rows)


@tool
def analyze_tti_csv(path: str, max_ues: int = 20) -> str:
    """LTE空口TTI级CSV日志分析（CMAC/PDCP/RLC的dl/ulUeTtiInfo）。
    按UE统计HARQ失败率、重传次数、MCS、SINR/CQI、TA、PDCP SN丢包/乱序、RLC重传，并判定异常。
    传入单个CSV文件或包含多个TTI CSV的目录。
    Args:
        path: TTI CSV文件或目录路径，如 D:\\cwei\\log。
        max_ues: 报告中最多展示的UE数。
    """
    try:
        return csv_tti_analyzer.analyze_tti_csv(path, max_ues=max_ues)
    except Exception as exc:
        return f"TTI CSV分析失败：{type(exc).__name__}: {exc}"


@tool
def parse_pcap(path: str, max_packets: int = 5000) -> str:
    """纯 Python 深度解码 Wireshark pcap/pcapng：协议栈分层 + NGAP/S1AP/NAS 消息类型识别 + RRC 特征。
    无需 tshark/scapy。加密的 AS 层会如实标注不可解码。
    Args:
        path: pcap 文件路径。
        max_packets: 最多解码包数。
    """
    return data_sources.parse_pcap(path, max_packets)


@tool
def parse_device_log(path: str, keyword: str = "", max_lines: int = 100) -> str:
    """解析基站 device log（支持文件、目录、zip），按关键字过滤关键行。"""
    return data_sources.parse_device_log(path, keyword, max_lines)


@tool
def parse_xtrace_log(path: str, mode: str = "auto", max_lines: int = 100) -> str:
    """纯 Python 深度解码 Xtrace .xst 二进制：提取每条信令的时间/接口/方向/MsgID/事件名。
    无需任何外部框架或 DLL；标准信令 ASN.1 码流在无 DLL 时保留头部与 MsgID 证据。
    Args:
        path: .xst 文件路径（二进制）或文本导出路径。
        mode: 4G/5G/auto，auto 按文件名或内容自动判断。
        max_lines: 最多展示的明细条数（全量记录仍会计入聚合统计）。
    """
    return data_sources.parse_xtrace_log(path, max_lines, mode)


@tool
def parse_sv_log(path: str, max_lines: int = 100) -> str:
    """SV/EI 调度日志深度分析（纯 pandas）：BLER/HARQ/调度失败/MCS/PDCP 丢包异常判定。
    支持 .ei/.bei 二进制与 CSV 目录。
    Args:
        path: SV/EI 文件路径或目录。
    """
    return data_sources.parse_sv_log(path, max_lines)


@tool
def parse_experience_doc(path: str, keyword: str = "", max_rows: int = 80) -> str:
    """检索本地排障经验文档（Markdown/文本/Word/Excel/PDF）的命中片段。
    Args:
        path: 文档或目录路径。
        keyword: 关键字过滤，如故障现象、错误码。
        max_rows: 最多返回行数/条数。
    """
    return data_sources.parse_experience_doc(path, keyword, max_rows)


# ===================== 6. State 与 Router 参数缺失检测 =====================
class AgentState(TypedDict):
    user_query: str
    task_type: str
    sub_results: Annotated[Sequence[str], operator.add]
    final_answer: str
    need_human: bool
    human_question: str
    human_feedback: str
    selected_agents: list[str]
    task_plan: str
    critic_report: str
    critic_verdict: str
    revision_needed: bool
    needs_specialists: bool
    direct_answer: str
    expected_specialist_count: int
    # 新架构字段
    data_source_flags: dict[str, Any]   # Orchestrator 探测的数据源标记
    parsed_data: str                     # DataParser 标准化输出
    radio_analysis: str                  # RadioAnalyst 推理结论
    knowledge_evidence: str              # KnowledgeRetriever 证据片段
    memory_recall: str                   # MemoryManager 召回的历史案例
    critic_confidence: str               # Critic 给出的结论置信度
    memory_saved_id: str                 # 本轮沉淀的案例 ID


# 新架构 3 个专家；旧 6 节点名作为兼容别名映射到新角色
ALL_AGENT_NODES = ["data_parser", "radio_analyst", "knowledge_retriever"]
TASK_TO_AGENT = {
    "data_parse": "data_parser",
    "radio_analysis": "radio_analyst",
    "knowledge": "knowledge_retriever",
}
AGENT_ALIASES = {
    # DataParser 别名（含所有实时数据源与旧日志节点）
    "data_parser": "data_parser", "data": "data_parser", "parse": "data_parser",
    "log": "data_parser", "qxdm": "data_parser", "log_agent": "data_parser",
    "weblmt": "data_parser", "config": "data_parser", "pcap": "data_parser",
    "xtrace": "data_parser", "sv": "data_parser", "device_log": "data_parser",
    # RadioAnalyst 别名（含旧计算节点）
    "radio_analyst": "radio_analyst", "radio": "radio_analyst", "analyst": "radio_analyst",
    "calc": "radio_analyst", "calculate": "radio_analyst", "compute": "radio_analyst",
    "calc_agent": "radio_analyst",
    # KnowledgeRetriever 别名（含旧协议/联网节点）
    "knowledge_retriever": "knowledge_retriever", "knowledge": "knowledge_retriever",
    "protocol": "knowledge_retriever", "3gpp": "knowledge_retriever", "rag": "knowledge_retriever",
    "protocol_agent": "knowledge_retriever", "web": "knowledge_retriever",
    "web_rag": "knowledge_retriever", "online": "knowledge_retriever",
    "internet": "knowledge_retriever", "web_agent": "knowledge_retriever",
}


def normalize_selected_agents(value: Any) -> list[str]:
    """把 Router 输出的任务/别名归一化为内部 Agent 节点名。"""
    if value is None:
        return []
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple, set)):
        items = [str(part).strip() for part in value if str(part).strip()]
    else:
        items = [str(value).strip()]

    selected = []
    for item in items:
        lowered = item.lower()
        agent = TASK_TO_AGENT.get(lowered) or AGENT_ALIASES.get(lowered)
        if agent and agent not in selected:
            selected.append(agent)
    return selected


QUERY_AGENT_HINTS = {
    "data_parser": (
        "日志", "log", "qxdm", ".isf", ".hdf", ".h5", ".txt", ".pcap", "pcap",
        "信令", "weblmt", "配置", "device log", "xtrace", "sv log", "解析", "读取", "提取",
    ),
    "radio_analyst": (
        "计算", "半径", "覆盖距离", "多普勒", "频偏", "ncs", "prach", "排查", "定位",
        "根因", "假设", "失败", "掉话", "接入", "切换", "干扰", "弱覆盖", "邻区", "为什么",
    ),
    "knowledge_retriever": (
        "协议", "3gpp", "条款", "规范", "手册", "案例", "经验", "sop", "资料",
        "文档", "联网", "在线", "网页", "互联网", "最新", "官网", "web", "网站",
    ),
}


def derive_agents_from_query(query: str) -> list[str]:
    """Router 输出为空时的确定性兜底：按用户问题关键词推断必要 Agent。"""
    text = str(query).lower()
    selected = []
    for agent, hints in QUERY_AGENT_HINTS.items():
        if any(hint in text for hint in hints):
            selected.append(agent)
    return selected


def _as_bool(value: Any, default: bool | None = None) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y", "是"}:
            return True
        if lowered in {"false", "0", "no", "n", "否"}:
            return False
    return default


def parse_router_output(text: str) -> dict:
    """解析 Orchestrator JSON，并提取本轮是否调用 Specialist。"""
    fallback = {
        "task_type": "",
        "task_plan": "",
        "missing_params": [],
        "selected_agents": [],
        "needs_specialists": None,
        "direct_answer": "",
    }
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return fallback
    try:
        data = json.loads(match.group(0))
    except Exception as exc:
        logger.warning("Router输出JSON解析失败：%s", exc)
        return fallback

    task_type = data.get("task_type", "")
    if isinstance(task_type, list):
        task_type = ",".join(str(t) for t in task_type)
    missing = data.get("missing_params", [])
    if not isinstance(missing, list):
        missing = []

    selected = normalize_selected_agents(data.get("selected_agents"))
    if not selected:
        selected = normalize_selected_agents(task_type)

    data["task_type"] = str(task_type)
    data["task_plan"] = str(data.get("task_plan") or "").strip()
    data["missing_params"] = [str(p) for p in missing]
    data["selected_agents"] = selected
    data["needs_specialists"] = _as_bool(data.get("needs_specialists"))
    data["direct_answer"] = str(data.get("direct_answer") or "").strip()
    return data


@with_timing_and_error(
    "Orchestrator总指挥",
    lambda exc: {
        "task_type": "unclassified",
        "task_plan": "",
        "need_human": False,
        "human_question": "",
        "human_feedback": "",
        "selected_agents": ALL_AGENT_NODES[:],
        "needs_specialists": True,
        "direct_answer": "",
        "expected_specialist_count": len(ALL_AGENT_NODES),
    },
)
def orchestrator_node(state: AgentState) -> dict:
    """Orchestrator：判断数据源、理解目标、拆解任务、分配专家、检测缺失参数。"""
    # 1) 数据源探测：判断本次任务有哪些可用实时数据
    try:
        flags = data_sources.detect_data_sources(state["user_query"])
    except Exception as exc:
        logger.warning("数据源探测失败，按无实时数据处理：%s", exc)
        flags = {
            "has_log": False, "has_config": False, "has_experience_doc": False,
            "log_files": [], "config_files": [], "experience_files": [],
        }
    logger.info(
        "数据源探测：log=%s, config=%s, doc=%s",
        flags.get("has_log"), flags.get("has_config"), flags.get("has_experience_doc"),
    )

    # 2) 记忆召回：注入同类历史故障案例，供推理专家参考
    try:
        recall = memory_manager_instance.format_recall(state["user_query"], top_k=3)
    except Exception as exc:
        logger.warning("记忆召回失败：%s", exc)
        recall = "无历史相似案例。"

    flags_text = json.dumps(flags, ensure_ascii=False)
    prompt = f"""
你是LTE/NR无线排障任务的Orchestrator总指挥。你负责判断可用数据源、理解目标、拆解任务、分配专家、检查关键参数；当确实无需任何专家时，你也可以给出限定范围的直接回答，但不得编造工具证据。

用户请求：
{state['user_query']}

当前可用数据源（系统已探测，务必据此决策）：
{flags_text}

历史相似案例（MemoryManager召回，仅供参考，不得当作本次证据）：
{recall}

可用专家 Specialist：
- data_parser：数据解析专家。统一读取/标准化 QXDM、WebLMT配置、pcap、device log、Xtrace、SV LOG、本地经验文档。当存在实时数据源、或用户要求读取/解析日志配置时选择。无数据源时它也会返回“无可用数据，仅基于故障现象推理”。
- radio_analyst：无线算法与信令专家。做故障假设、信令核查、参数合规校验、定量计算。凡是需要排障推理、根因定位、参数计算的场景都应选择；它是核心推理专家。
- knowledge_retriever：协议与经验检索专家。检索3GPP协议、内部SOP、历史案例，必要时联网查最新资料。当用户要求依据协议条款、经验案例、联网资料，或 RadioAnalyst 可能需要协议佐证时选择。

严禁无脑选择全部专家。只选择完成用户请求所必需的专家，且 data_parser 仅在有实时数据源或需解析时选择。
无日志无配置的纯现象推理：只选 radio_analyst（必要时加 knowledge_retriever），不要选 data_parser。

如果你判断无需任何专家，请设置 needs_specialists=false，并在 direct_answer 中直接给出专业回答；此时 selected_agents 必须为空数组。
如果你判断需要专家，请设置 needs_specialists=true，selected_agents 列出必要专家，direct_answer 为空字符串。

输出格式（严格JSON，不要输出其他文字）：
{{
  "task_plan": "用2-4条短句说明如何拆解目标并分配专家",
  "needs_specialists": true 或 false,
  "direct_answer": "无需专家时的直接回答；需要专家时为空字符串",
  "task_type": "从 data_parse,radio_analysis,knowledge 中选择，逗号分隔；无匹配可为空字符串",
  "selected_agents": ["从 data_parse,radio_analysis,knowledge 中选择需要的项；无匹配为空数组"],
  "missing_params": ["只对已选专家检查缺失的关键参数；没有则空数组"],
  "human_question": "如果已选专家缺少参数，向工程师提出的具体问题；没有则空字符串"
}}

关键参数判定规则（仅对selected_agents中的专家生效，用户已给出的不要重复追问）：
- PRACH半径计算：需要 prach_format、ncs配置索引；多普勒：freq_ghz、speed_kmh；
- 日志/配置解析：需要 log_path / 配置路径；用户未提供且无任何可探测文件时才追问；
- 纯现象推理与协议咨询：通常无需追问，可基于理论与历史案例给出可能性排序。
"""
    raw = call_llm(orchestrator_llm, prompt, "Orchestrator总指挥")
    logger.info("Orchestrator原始输出：%s", raw[:500].replace("\n", " "))
    parsed = parse_router_output(raw)
    missing = parsed.get("missing_params", [])

    needs_specialists = parsed.get("needs_specialists")
    selected = parsed.get("selected_agents") or []

    if needs_specialists is False:
        selected = []
        missing = []
    else:
        if not selected:
            selected = derive_agents_from_query(state["user_query"])
            if selected:
                logger.warning("Orchestrator未给出有效专家，已按用户问题推断：%s", selected)
        needs_specialists = bool(selected)

    direct_answer = str(parsed.get("direct_answer") or "").strip()
    task_type = str(parsed.get("task_type") or "").strip()
    if not task_type and selected:
        reverse_task = {v: k for k, v in TASK_TO_AGENT.items()}
        task_type = ",".join(reverse_task.get(agent, agent) for agent in selected)

    need_human = bool(missing) and needs_specialists
    question = parsed.get("human_question", "").strip() if need_human else ""
    if need_human and not question:
        question = f"检测到缺少关键测试参数：{', '.join(missing)}。请补充后继续执行。"

    task_plan = parsed.get("task_plan") or ("；".join(selected) if selected else "总指挥直接回答")
    logger.info(
        "Orchestrator结果：task_type=%s, needs_specialists=%s, selected_agents=%s, need_human=%s",
        task_type, needs_specialists, selected, need_human,
    )
    return {
        "task_type": task_type or "unclassified",
        "task_plan": task_plan,
        "need_human": need_human,
        "human_question": question,
        "human_feedback": state.get("human_feedback", ""),
        "selected_agents": selected,
        "needs_specialists": needs_specialists,
        "direct_answer": direct_answer,
        "expected_specialist_count": len(selected) if needs_specialists else 1,
        "data_source_flags": flags,
        "memory_recall": recall,
    }


router_node = orchestrator_node


def human_in_loop_node(state: AgentState) -> dict:
    """Human-in-the-Loop：缺参数时暂停，工程师反馈后恢复。"""
    start = time.perf_counter()
    if not state.get("need_human"):
        logger.info("Human-in-Loop跳过：无缺失参数")
        return {}
    try:
        logger.info("Human-in-Loop等待工程师输入：%s", state.get("human_question"))
        answer = interrupt({"question": state.get("human_question", "")})
        feedback = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        logger.info("Human-in-Loop收到反馈，长度=%d", len(feedback))
        return {
            "human_feedback": feedback,
            "need_human": False,
            "user_query": f"{state['user_query']}\n工程师补充：{feedback}",
        }
    finally:
        logger.info("Human-in-Loop耗时=%.2fs", time.perf_counter() - start)


@with_timing_and_error(
    "Orchestrator直接回答",
    lambda exc: {
        "direct_answer": "",
        "sub_results": [f"【Orchestrator直接回答】失败：{type(exc).__name__}: {exc}"],
    },
)
def direct_answer_node(state: AgentState) -> dict:
    """无需 Specialist 时，由 Orchestrator 直接给出限定范围回答。"""
    answer = str(state.get("direct_answer") or "").strip()
    if not answer:
        prompt = f"""
你是LTE/NR测试任务Orchestrator总指挥。当前无需调用任何Specialist，请直接回答用户问题。
要求：
1. 只基于用户问题和你的通用工程知识；
2. 不得声称已经读取日志、计算工具结果、检索本地文档或联网；
3. 如果问题必须依赖具体工具证据，请明确说明需要哪类Specialist，而不是编造结论；
4. 回答要专业、简洁、可执行。

用户问题：
{state['user_query']}

你的任务拆解：
{state.get('task_plan', '')}
"""
        answer = call_llm(orchestrator_llm, prompt, "Orchestrator直接回答")
    answer = answer or "未生成直接回答。"
    return {
        "direct_answer": answer,
        "sub_results": [f"【Orchestrator直接回答】{answer}"],
    }


VALID_ROUTE_TARGETS = {"direct_answer", "specialists_run"}


def route_after_human(state: AgentState) -> list[str]:
    """根据Orchestrator决策选择：直接回答路径或专家编排路径。"""
    if not state.get("needs_specialists") or not state.get("selected_agents"):
        logger.info("路由决策：无需专家，进入Orchestrator直接回答")
        return ["direct_answer"]
    logger.info("路由决策：进入专家编排节点，selected=%s", state["selected_agents"])
    return ["specialists_run"]


# ===================== 7. 三个专家 ReAct Agent（DataParser / RadioAnalyst / KnowledgeRetriever） =====================
data_parser_agent = create_agent(
    model=data_parser_llm,
    tools=[
        parse_qxdm_log, parse_weblmt_config, parse_pcap,
        parse_device_log, parse_xtrace_log, parse_sv_log,
        parse_experience_doc, detect_data_sources, analyze_tti_csv,
    ],
    system_prompt="""
你是DataParser数据解析专家，负责统一读取并标准化本次任务的实时数据源。
可用工具：detect_data_sources（探测数据源）、parse_qxdm_log（QXDM日志）、parse_weblmt_config（WebLMT配置）、
parse_pcap（Wireshark）、parse_device_log（基站device log）、parse_xtrace_log（Xtrace）、parse_sv_log（SV LOG）、
analyze_tti_csv（LTE空口TTI级CSV：CMAC/PDCP/RLC的HARQ/重传/MCS/SINR/PDCP丢包统计）、
parse_experience_doc（本地经验文档：Markdown/Word/Excel/PDF）。
遇到 CMAC/PDCP/RLC 的 *UeTtiInfo_Lte*.csv 文件时，必须用 analyze_tti_csv 做TTI级统计分析，不要当普通文本读取。
工作规则：
1. 先调用 detect_data_sources 探测数据源（query 可留空或简述，工具会自动扫描上传目录与默认目录，无需你手动拼路径）。
2. 探测结果 log_files 中的 *UeTtiInfo_Lte*.csv 路径，直接传给 analyze_tti_csv（可一次性传整个目录），每类数据源的解析工具只调用一次，不要对同一文件重复调用、也不要反复调用 detect_data_sources。
3. 只输出工具真实返回的结构化片段与简短字段判断（如RRC/MAC/PHY/PRACH/测量报告/TA/RSRP/SINR/MCS/HARQ、小区/频点/GSCN/功率/PRACH/定时器/邻区）。
   解析 Xtrace 时，接入请求数/成功数/失败数必须按工具返回的"接入关键事件精确计数（全量）"字段逐项统计得出，
   禁止从"明细样本"里人工数数（明细仅供格式参考、可能被截断）；全量事件计数中的 Unknown 项不可当作某类具体信令。
4. 若无任何数据源或解析失败，必须明确输出“无可用数据，仅基于故障现象推理”，禁止编造信令与参数。
5. 最终输出便于其他专家读取的标准化文本，标注数据来源文件路径。
""",
    debug=False,
)

radio_analyst_agent = create_agent(
    model=radio_llm,
    tools=[lte_nr_calc],
    system_prompt="""
你是RadioAnalyst无线算法与信令专家，是LTE/NR空口排障的核心推理者。
输入可能包含：DataParser标准化日志/配置、MemoryManager召回的历史案例、用户故障现象。
工作规则：
1. 信令流程核查：随机接入、RRC建立/重配/切换、DRX、测量、掉话、乒乓切换等，逐条给出证据支撑。
2. 参数合规校验：频段、GSCN、SSB、PRACH NCS、TA、邻区、功率、定时器，必要时对照协议（可引用知识检索证据）。
3. 故障假设：生成多个并行假设（如弱覆盖/干扰/邻区漏配/PRACH配置错误/终端能力不匹配），并给出可能性排序。
4. 定量计算：GSCN、多普勒、覆盖距离、PRACH检测门限、峰值吞吐量等，必须调用 lte_nr_calc，禁止心算替代工具结果。
   峰值吞吐量用 calc_type="throughput"，并给出 bandwidth_mhz/modulation/mimo_layers/overhead；
   例如20MHz+256QAM+4流 := lte_nr_calc(calc_type="throughput", bandwidth_mhz=20, modulation="256qam", mimo_layers=4)。
5. 有日志时基于信令证据推理；无日志仅现象时，基于无线理论给出可能性排序并说明证据缺口。
6. 保持回答聚焦：一轮内完成必要工具调用并给出结论，不要反复试探工具或对同一问题多次调用同一工具。
7. 明确区分“工具/日志事实”与“工程推断”，禁止把推测包装成确定结论。
""",
    debug=False,
)

knowledge_retriever_agent = create_agent(
    model=knowledge_llm,
    tools=[rag_protocol_query, web_rag_search, parse_experience_doc],
    system_prompt="""
你是KnowledgeRetriever协议与经验检索专家，专门检索通用静态知识：3GPP TS38/TS36协议、内部排障SOP、历史故障案例、必要时的联网资料。
与DataParser的区别：DataParser解析本次任务的实时日志/配置；你检索通用协议和历史经验库。
可用工具：rag_protocol_query（本地协议/技术文档RAG）、parse_experience_doc（本地经验文档）、web_rag_search（联网最新资料）。
工作规则：
1. 本地优先：3GPP协议条款、技术文档参数、内部SOP、历史案例一律先用 rag_protocol_query / parse_experience_doc 检索本地库；
   只有本地库无相关内容、且问题确实需要最新/外部资料时，才调用 web_rag_search。禁止在本地已有相关资料时联网。
2. web_rag_search 若提示"外网不可用/已熔断"，立即停止联网尝试，改为基于本地资料回答并说明外网受限，不得反复调用。
3. 必须基于工具返回内容回答，标明来源（文件/页码 或 URL/标题）；禁止用模型记忆编造条款号、参数或案例。
4. 输出：相关协议原文片段、同类历史故障案例、可执行的排障检查清单。
5. 若检索失败或无结果，必须如实说明，不得编造。
""",
    debug=False,
)


def invoke_sub_agent(agent, query: str, label: str) -> str:
    """执行子 Agent；异常会被分类为可重试/不可重试后向上抛出。

    recursion_limit 限制 ReAct 循环轮数，防止模型反复试探工具耗尽超时。
    """
    try:
        result = run_with_timeout(
            lambda: agent.invoke(
                {"messages": [{"role": "user", "content": query}]},
                config={"recursion_limit": SUB_AGENT_RECURSION_LIMIT},
            ),
            SUB_AGENT_TIMEOUT_SECONDS,
            label,
        )
        text = message_content(result["messages"][-1])
        detected = detect_agent_error(text)
        if detected is not None:
            raise detected
        return text
    except (RetryableAgentError, NonRetryableAgentError):
        raise
    except Exception as exc:
        logger.exception("%s执行异常：%s", label, exc)
        raise classify_agent_error(f"{type(exc).__name__}: {exc}") from exc


def _retry_logger(label: str):
    """每次重试写日志，记录当前重试序号与异常。"""
    def log_retry(retry_state):
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        logger.warning("%s第%d次重试，原因=%s", label, retry_state.attempt_number, exc)
    return log_retry


def make_sub_agent_node(agent, label: str, max_retries: int, route_key: str):
    """构建带 tenacity 重试的子 Agent 节点。

    max_retries 表示额外重试次数：2 代表首次执行 + 最多 2 次重试 = 3 次尝试。
    route_key 由 Orchestrator 选中后才真正调用对应 Specialist Agent。
    """
    @with_timing_and_error(label, lambda exc: {"sub_results": [f"【{label}】节点失败：{exc}"]})
    def node(state: AgentState) -> dict:
        selected = state["selected_agents"] if state.get("selected_agents") is not None else ALL_AGENT_NODES
        if route_key not in selected:
            logger.info("%s未被Orchestrator选中，跳过执行", label)
            return {"sub_results": []}

        @retry(
            stop=stop_after_attempt(max_retries + 1),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception_type(RetryableAgentError),
            before_sleep=_retry_logger(label),
            reraise=True,
        )
        def _invoke():
            return invoke_sub_agent(agent, state["user_query"], label)

        try:
            text = _invoke()
            return {"sub_results": [f"【{label}】{text}"]}
        except Exception as exc:
            message = f"【{label}】重试耗尽/执行失败：{type(exc).__name__}: {exc}"
            logger.error(message)
            return {"sub_results": [message]}

    return node


def _run_specialist(agent, query: str, label: str, max_retries: int) -> str:
    """带 tenacity 重试地调用单个专家 Agent，失败返回可读错误串。"""
    @retry(
        stop=stop_after_attempt(max_retries + 1),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(RetryableAgentError),
        before_sleep=_retry_logger(label),
        reraise=True,
    )
    def _invoke():
        return invoke_sub_agent(agent, query, label)

    try:
        return _invoke()
    except Exception as exc:
        message = f"【{label}】重试耗尽/执行失败：{type(exc).__name__}: {exc}"
        logger.error(message)
        return message


@with_timing_and_error(
    "专家编排节点",
    lambda exc: {"sub_results": [f"专家编排节点失败：{type(exc).__name__}: {exc}"]},
)
def specialists_run_node(state: AgentState) -> dict:
    """执行 Orchestrator 选中的专家：DataParser→RadioAnalyst/KnowledgeRetriever。

    在单节点内按数据依赖顺序调度，避免多分支扇入 Critic 时的 LangGraph join 死锁；
    每个专家是否执行仍严格由 selected_agents 控制。
    """
    selected = state.get("selected_agents") or []
    sub_results: list[str] = []
    parsed_data = ""
    radio_analysis = ""
    knowledge_evidence = ""

    # 阶段1：DataParser 标准化实时数据，供后续专家使用
    if "data_parser" in selected:
        logger.info("专家编排：执行 DataParser 数据解析专家")
        parsed_data = _run_specialist(data_parser_agent, state["user_query"], "DataParser数据解析专家", 2)
        sub_results.append(f"【DataParser数据解析专家】{parsed_data}")

    # 阶段2：RadioAnalyst 核心推理（注入标准化数据 + 历史案例 + 任务拆解）
    if "radio_analyst" in selected:
        logger.info("专家编排：执行 RadioAnalyst 无线算法与信令专家")
        radio_query = (
            f"用户故障现象/问题：{state['user_query']}\n\n"
            f"Orchestrator任务拆解：{state.get('task_plan', '')}\n\n"
            f"历史相似案例（仅供参考，非本次证据）：\n{state.get('memory_recall', '无')}\n\n"
            f"DataParser标准化数据：\n{parsed_data or '（无实时数据，仅基于故障现象与无线理论推理）'}"
        )
        radio_analysis = _run_specialist(radio_analyst_agent, radio_query, "RadioAnalyst无线信令专家", 1)
        sub_results.append(f"【RadioAnalyst无线信令专家】{radio_analysis}")

    # 阶段3：KnowledgeRetriever 协议/经验/联网检索
    if "knowledge_retriever" in selected:
        logger.info("专家编排：执行 KnowledgeRetriever 协议与经验检索专家")
        knowledge_query = (
            f"排障/检索主题：{state['user_query']}\n"
            f"任务拆解：{state.get('task_plan', '')}"
        )
        knowledge_evidence = _run_specialist(
            knowledge_retriever_agent, knowledge_query, "KnowledgeRetriever知识检索专家", 0
        )
        sub_results.append(f"【KnowledgeRetriever知识检索专家】{knowledge_evidence}")

    if not sub_results:
        sub_results.append("专家编排：selected_agents 非空但无可执行专家。")

    return {
        "sub_results": sub_results,
        "parsed_data": parsed_data,
        "radio_analysis": radio_analysis,
        "knowledge_evidence": knowledge_evidence,
    }


# ===================== 8. Critic 独立评审 Agent =====================
def parse_critic_output(text: str) -> dict:
    """解析 Critic 的 JSON 评审结果，失败时返回保守默认。"""
    fallback = {"verdict": "UNKNOWN", "issues": [], "suggestions": [], "raw": text}
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return fallback
    try:
        data = json.loads(match.group(0))
    except Exception as exc:
        logger.warning("Critic输出JSON解析失败：%s", exc)
        return fallback

    def as_list(value):
        if isinstance(value, list):
            return [str(item) for item in value]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    return {
        "verdict": str(data.get("verdict", "UNKNOWN")).upper(),
        "issues": as_list(data.get("issues", [])),
        "suggestions": as_list(data.get("revision_suggestions", [])),
        "confidence": str(data.get("confidence", "") or "").strip().upper(),
        "raw": text,
    }


@with_timing_and_error(
    "Critic评审Agent",
    lambda exc: {
        "critic_report": f"Critic评审失败：{type(exc).__name__}: {exc}",
        "critic_verdict": "ERROR",
        "critic_confidence": "LOW",
        "revision_needed": True,
    },
)
def critic_node(state: AgentState) -> dict:
    """独立第三方评审：校验证据链、参数正确性、逻辑冲突与结论完备性，标记置信度。"""
    sub_results = state.get("sub_results") or []
    if not sub_results:
        return {
            "critic_report": "无专家输出，Critic跳过评审",
            "critic_verdict": "SKIPPED",
            "critic_confidence": "LOW",
            "revision_needed": False,
        }

    specialist_output = "\n\n".join(sub_results)
    data_source_flags = state.get("data_source_flags") or {}
    prompt = f"""
你是独立Critic评审Agent，不参与推理、不生成技术结论，只挑错与校验证据链。
请严格检查以下维度：
1. 证据一致性：每条结论是否有日志/配置/工具返回支撑，禁止无依据猜想；
2. 参数正确性：GSCN、频点、PRACH NCS、定时器、TA等计算与解读是否符合3GPP；
3. 逻辑冲突检测：不同专家的假设是否互相矛盾；
4. 结论完备性：是否遗漏高概率故障原因；
5. 幻觉风险：是否把“无可用数据/未命中/工具失败”包装成确定结论；
6. 数据缺口：若缺少关键数据，必须明确标注“缺少XX数据，结论置信度低”。

本次可用数据源标记：{json.dumps(data_source_flags, ensure_ascii=False)}

用户原始问题：
{state['user_query']}

Orchestrator任务拆解：
{state.get('task_plan', '')}

专家输出：
{specialist_output}

输出严格JSON，不要输出其他文字：
{{
  "verdict": "PASS 或 WARN 或 FAIL",
  "confidence": "HIGH 或 MEDIUM 或 LOW",
  "issues": ["发现的问题；没有问题则空数组"],
  "revision_suggestions": ["需要重跑某专家/修订或人工复核的建议；没有则空数组"]
}}
"""
    raw = call_llm(critic_llm, prompt, "Critic评审Agent")
    parsed = parse_critic_output(raw)
    verdict = parsed["verdict"]
    issues = parsed["issues"] or ["未发现明确问题"]
    suggestions = parsed["suggestions"] or ["无需修订"]
    confidence = parsed["confidence"]
    if confidence not in {"HIGH", "MEDIUM", "LOW"}:
        confidence = "LOW" if verdict == "FAIL" else (
            "MEDIUM" if verdict == "WARN" or not data_source_flags.get("has_log") else "HIGH"
        )

    report = "\n".join([
        f"评审结论：{verdict}",
        f"结论置信度：{confidence}",
        "发现的问题：",
        *[f"- {item}" for item in issues],
        "修订/复核建议：",
        *[f"- {item}" for item in suggestions],
    ])
    logger.info("Critic评审完成：verdict=%s, confidence=%s, issues=%d", verdict, confidence, len(parsed["issues"]))
    return {
        "critic_report": report,
        "critic_verdict": verdict,
        "critic_confidence": confidence,
        "revision_needed": verdict == "FAIL",
    }


# ===================== 9. Summary 汇总节点 =====================
@with_timing_and_error(
    "Summary汇总节点",
    lambda exc: {"final_answer": f"汇总失败：{type(exc).__name__}: {exc}"},
)
def summary_node(state: AgentState) -> dict:
    """合并专家输出（及可选的Critic评审），生成专业 LTE/NR 无线排障报告。"""
    all_sub = "\n\n".join(state.get("sub_results", []))
    if ENABLE_CRITIC:
        critic_intro = "、Critic评审"
        critic_requirement = "2. 必须纳入Critic评审结论与置信度；如WARN/FAIL或置信度低，必须明确写出风险与复核建议；\n"
        critic_section = "Critic评审、"
        critic_prompt = (
            f"Critic评审结论：{state.get('critic_verdict', 'UNKNOWN')}，置信度：{state.get('critic_confidence', 'UNKNOWN')}\n"
            f"{state.get('critic_report', '无')}\n\n"
        )
    else:
        critic_intro = ""
        critic_requirement = ""
        critic_section = ""
        critic_prompt = ""
    prompt = f"""
你是资深LTE/NR无线排障工程报告撰写专家。请基于Orchestrator拆解、专家输出{critic_intro}，输出专业、结构化的排障报告。
要求：
1. 不得编造专家未给出的参数、信令、协议条款或文档内容；
{critic_requirement}3. 明确区分“工具/日志事实”和“工程推断/建议”；
4. 若无任何数据源，必须说明“基于故障现象与无线理论推理，缺少XX数据，置信度低”；
5. 报告包含：任务概述、数据源情况、任务拆解、关键参数、信令/工具证据、协议与案例依据、{critic_section}故障可能性排序、结论与下一步建议。

用户原始问题：
{state['user_query']}

数据源标记：
{json.dumps(state.get('data_source_flags', {}), ensure_ascii=False)}

历史相似案例（仅供参考）：
{state.get('memory_recall', '无')}

Orchestrator任务拆解：
{state.get('task_plan', '') or '无'}

任务分类：
{state.get('task_type', '')}

是否需要专家：
{state.get('needs_specialists', False)}

实际调用的专家：
{', '.join(state.get('selected_agents', [])) or '无'}

Orchestrator直接回答：
{state.get('direct_answer', '') or '无'}

工程师补充反馈：
{state.get('human_feedback', '') or '无'}

DataParser标准化数据：
{state.get('parsed_data', '') or '无'}

RadioAnalyst推理：
{state.get('radio_analysis', '') or '无'}

KnowledgeRetriever证据：
{state.get('knowledge_evidence', '') or '无'}

{critic_prompt}专家原始输出：
{all_sub}
"""
    try:
        answer = call_llm(summary_llm, prompt, "Summary汇总节点")
        return {"final_answer": answer or all_sub}
    except Exception as exc:
        logger.warning("Summary模型汇总失败，改用专家结果直出：%s", exc)
        critic_fallback = f"### Critic评审\n{state.get('critic_report', '无')}\n\n" if ENABLE_CRITIC else ""
        fallback = (
            "## 汇总模型调用失败，以下为专家原始结果\n\n"
            f"> 失败原因：{type(exc).__name__}: {exc}\n\n"
            f"{critic_fallback}### 专家输出\n{all_sub}"
        )
        return {"final_answer": fallback}


# ===================== 9b. MemoryManager 记忆沉淀节点 =====================
@with_timing_and_error(
    "MemoryManager记忆节点",
    lambda exc: {"memory_saved_id": ""},
)
def memory_manager_node(state: AgentState) -> dict:
    """任务结束后抽取案例、推理轨迹与结论，沉淀到长期记忆；失败不影响主流程。"""
    conclusion = state.get("final_answer") or state.get("direct_answer") or ""
    if not conclusion:
        conclusion = "\n\n".join(state.get("sub_results", []))
    try:
        case_id = memory_manager_instance.save_case(
            state["user_query"],
            task_plan=state.get("task_plan", ""),
            parsed_data=state.get("parsed_data", ""),
            analysis=state.get("radio_analysis", ""),
            knowledge=state.get("knowledge_evidence", ""),
            conclusion=conclusion,
            critic_verdict=state.get("critic_verdict", ""),
            critic_report=state.get("critic_report", ""),
            data_sources=state.get("data_source_flags", {}),
            tags=list(state.get("selected_agents", [])),
        )
        return {"memory_saved_id": case_id}
    except Exception as exc:
        logger.warning("记忆沉淀失败（不影响报告）：%s", exc)
        return {"memory_saved_id": ""}


# ===================== 9. LangGraph 构建 =====================
builder = StateGraph(AgentState)
builder.add_node("orchestrator", orchestrator_node)
builder.add_node("human_in_loop", human_in_loop_node)
builder.add_node("direct_answer", direct_answer_node)
builder.add_node("specialists_run", specialists_run_node)
builder.add_node("summary", summary_node)
builder.add_node("memory_manager", memory_manager_node)

builder.set_entry_point("orchestrator")
builder.add_edge("orchestrator", "human_in_loop")
builder.add_conditional_edges(
    "human_in_loop",
    route_after_human,
    {
        "direct_answer": "direct_answer",
        "specialists_run": "specialists_run",
    },
)

if ENABLE_CRITIC:
    # 开启评审：两条路径各自独立连到 Critic，避免列表扇入在分支未激活时死锁。
    builder.add_node("critic", critic_node)
    builder.add_edge("direct_answer", "critic")
    builder.add_edge("specialists_run", "critic")
    builder.add_edge("critic", "summary")
    logger.info("Critic评审节点已接入图")
else:
    # 关闭评审（默认）：直接汇入 Summary，Critic 节点不注册、模型不调用。
    builder.add_edge("direct_answer", "summary")
    builder.add_edge("specialists_run", "summary")
    logger.info("Critic评审已关闭，专家输出直接进入Summary")

builder.add_edge("summary", "memory_manager")
builder.add_edge("memory_manager", END)

graph = builder.compile(checkpointer=MemorySaver())
logger.info("LangGraph编译完成，ENABLE_CRITIC=%s", ENABLE_CRITIC)


# ===================== 10. 运行入口 =====================
def initial_state(user_query: str) -> AgentState:
    return {
        "user_query": user_query,
        "task_type": "",
        "sub_results": [],
        "final_answer": "",
        "need_human": False,
        "human_question": "",
        "human_feedback": "",
        "selected_agents": [],
        "task_plan": "",
        "critic_report": "",
        "critic_verdict": "",
        "revision_needed": False,
        "needs_specialists": True,
        "direct_answer": "",
        "expected_specialist_count": 0,
        "data_source_flags": {},
        "parsed_data": "",
        "radio_analysis": "",
        "knowledge_evidence": "",
        "memory_recall": "",
        "critic_confidence": "",
        "memory_saved_id": "",
    }


def graph_config(thread_id: str) -> dict:
    """生成 LangGraph 会话配置；thread_id 决定 MemorySaver 状态隔离。"""
    return {"configurable": {"thread_id": thread_id}}


def extract_interrupt_question(result: dict) -> str | None:
    """从图执行结果中提取 Human-in-the-Loop 问题；无中断返回 None。"""
    interrupts = result.get("__interrupt__") or []
    if not interrupts:
        return None
    value = getattr(interrupts[0], "value", None)
    if isinstance(value, dict):
        return value.get("question", str(value))
    return str(value)


def start_graph_task(user_query: str, thread_id: str) -> dict:
    """启动一个带会话隔离的图任务，供 CLI/Web 前端调用。"""
    return run_with_timeout(
        lambda: graph.invoke(initial_state(user_query), graph_config(thread_id)),
        GRAPH_TIMEOUT_SECONDS,
        "多Agent图",
    )


def resume_graph_task(feedback: str, thread_id: str) -> dict:
    """向中断节点提交工程师反馈并恢复图执行。"""
    return graph.invoke(Command(resume=feedback), graph_config(thread_id))


if __name__ == "__main__":
    user_input = (
        "计算LTE PRACH format1 ncs配置索引14最大接入距离，查询本地3GPP协议，"
        "并联网查找最新的相关技术资料"
    )
    thread_id = f"lte-nr-{uuid.uuid4()}"
    logger.info("任务开始：%s", user_input[:200])

    try:
        result = start_graph_task(user_input, thread_id)
        question = extract_interrupt_question(result)
        if question:
            print("\n==== 需要工程师补充信息 ====")
            print(question)
            print("请运行 python gradio_app.py，在网页弹窗中提交反馈以继续执行。")
        else:
            print("\n==== LTE测试工程报告 ====")
            print(result.get("final_answer", "未生成最终报告"))
    except Exception as exc:
        logger.exception("任务执行失败：%s", exc)
        print(f"\n任务执行失败：{type(exc).__name__}: {exc}")
    finally:
        logger.info("任务结束")
