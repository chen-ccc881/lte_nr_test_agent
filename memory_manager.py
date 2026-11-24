"""长期记忆管理 Agent（MemoryManager）——SQLite 持久化 + 语义近似去重。

职责：任务结束后抽取故障案例、推理结论、评审结果，持久化到 SQLite；
后续同类故障通过字符 n-gram 哈希向量余弦相似度召回。新案例入库前与库内
案例做语义近似判定（领域同义词归一化 + 余弦），近似重复则合并更新而非新增。

设计约束：
- 仅依赖标准库（sqlite3），不 import lte_nr_agent，避免与 LangGraph 图循环依赖；
- 对外 API 与旧 JSONL 版完全兼容：save_case / recall / format_recall / stats / get_memory_manager；
- 所有写操作在事务 + 锁内完成，失败吞掉异常，绝不因记忆失败影响主分析流程；
- 数据库默认 memory/cases.db，可用 LTE_NR_MEMORY_PATH 覆盖；启动自动迁移旧 cases.jsonl。
- 语义去重阈值可用 LTE_NR_MEMORY_DEDUP_THRESHOLD 覆盖（默认 0.82）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger("lte_nr_agent.memory")

_VECTOR_DIM = 4096
_NGRAM = 2
DEDUP_THRESHOLD = float(os.getenv("LTE_NR_MEMORY_DEDUP_THRESHOLD", "0.82"))


def _default_db_path() -> Path:
    env = os.getenv("LTE_NR_MEMORY_PATH")
    if env:
        path = Path(env)
        if path.suffix.lower() in (".jsonl", ".json"):  # 兼容旧值指向 JSONL
            path = path.with_suffix(".db")
        return path
    return Path(__file__).resolve().parent / "memory" / "cases.db"


DEFAULT_MEMORY_PATH = _default_db_path()
LEGACY_JSONL_PATH = Path(__file__).resolve().parent / "memory" / "cases.jsonl"

# 领域同义词归一化：把常见等价表述映射到统一词，提升"换问法"下的语义近似判定
_SYNONYMS = {
    "attach": "附着",
    "registration": "注册",
    "rlf": "掉话",
    "handover": "切换",
    "interference": "干扰",
    "random access": "随机接入",
    "rach": "随机接入",
    "sinr": "信噪比",
    "rsrp": "参考信号功率",
    "prach": "随机接入前导",
    "doppler": "多普勒",
}
_SYN_PATTERN = re.compile(
    "|".join(re.escape(k) for k in sorted(_SYNONYMS, key=len, reverse=True)),
    flags=re.IGNORECASE,
)


def _normalize_text(text) -> str:
    normalized = _SYN_PATTERN.sub(lambda m: _SYNONYMS.get(m.group(0).lower(), m.group(0)), str(text or ""))
    return re.sub(r"\s+", " ", normalized).strip().lower()


def _ngrams(text) -> list[str]:
    bare = re.sub(r"[\s\W_]+", "", _normalize_text(text))
    if not bare:
        return []
    if len(bare) <= _NGRAM:
        return [bare]
    return [bare[i:i + _NGRAM] for i in range(len(bare) - _NGRAM + 1)]


def _vectorize(text) -> dict[int, float]:
    vec: dict[int, float] = {}
    for gram in _ngrams(text):
        idx = int.from_bytes(hashlib.md5(gram.encode("utf-8")).digest()[:4], "big") % _VECTOR_DIM
        vec[idx] = vec.get(idx, 0.0) + 1.0
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {k: v / norm for k, v in vec.items()}


def _load_vector(value) -> dict[int, float]:
    """从 SQLite/JSON 读取向量：JSON 会把 int key 转成 str，需还原为 int。"""
    raw = _json(value, {})
    return {int(k): float(v) for k, v in raw.items()}


def _cosine(left: dict[int, float], right: dict[int, float]) -> float:
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    return sum(v * right.get(k, 0.0) for k, v in left.items())


def _merge_vectors(vecs: list[dict[int, float]]) -> dict[int, float]:
    acc: dict[int, float] = {}
    for vector in vecs:
        for key, weight in vector.items():
            acc[key] = acc.get(key, 0.0) + weight
    norm = math.sqrt(sum(v * v for v in acc.values())) or 1.0
    return {key: value / norm for key, value in acc.items()}


# 领域概念词典：把等价表述（中英/缩写）归一到同一概念，提升语义近似去重能力
_CONCEPT_RULES: dict[str, tuple[str, ...]] = {
    "接入": ("接入", "attach", "附着", "access", "注册", "registration", "initial access"),
    "随机接入": ("随机接入", "prach", "rach", "msg1", "message1", "前导", "preamble", "rar", "msg2", "msg3", "msg4", "前导序列"),
    "失败": ("失败", "failure", "fail", "reject", "拒绝", "异常", "error", "unsuccessful"),
    "重传": ("重传", "retx", "retransmission", "反复重发", "一直重发", "反复发送", "重发"),
    "切换": ("切换", "handover", "ho", "重选", "reselection"),
    "掉话": ("掉话", "rlf", "drop", "无线链路失败", "release", "释放"),
    "干扰": ("干扰", "interference", "sinr", "噪声", "noise", "signal"),
    "覆盖": ("覆盖", "coverage", "rsrp", "弱覆盖", "信号弱", "路损"),
    "邻区": ("邻区", "neighbor", "邻小区", "漏配"),
    "吞吐": ("吞吐", "throughput", "速率", "pusch", "pdsch", "调度"),
    "功率": ("功率", "power", "txpower", "发射功率"),
    "定时器": ("定时器", "timer", "t300", "t310", "n310", "t304"),
    "n28": ("n28", "28频段", "700m", "band n28"),
    "prach配置": ("prach配置", "ncs", "prach format", "前导格式", "根序列"),
}


def _extract_concepts(text) -> set[str]:
    normalized = _normalize_text(text)
    concepts: set[str] = set()
    for concept, aliases in _CONCEPT_RULES.items():
        if any(alias in normalized for alias in aliases):
            concepts.add(concept)
    return concepts


def _concept_jaccard(left: set[str], right: set[str]) -> float:
    if len(left) < 2 or len(right) < 2:
        return 0.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _semantic_similarity(text_a: str, text_b: str) -> float:
    """字符余弦与领域概念 Jaccard 取最大值：兼顾字面相近与跨语言语义等价。"""
    char_score = _cosine(_vectorize(text_a), _vectorize(text_b))
    concept_score = _concept_jaccard(_extract_concepts(text_a), _extract_concepts(text_b))
    return max(char_score, concept_score)


def _json(value, default):
    try:
        return json.loads(value) if value else default
    except (json.JSONDecodeError, TypeError):
        return default


class MemoryManager:
    """故障案例的 SQLite 持久化与召回，线程安全，语义近似去重。"""

    def __init__(self, path=None):
        raw = Path(path) if path else DEFAULT_MEMORY_PATH
        if raw.suffix.lower() in (".jsonl", ".json"):
            raw = raw.with_suffix(".db")
        self.path = raw
        self._lock = threading.RLock()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning("创建记忆目录失败：%s", exc)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA busy_timeout=5000")
                self._create_schema()
                self._conn.commit()
                self._migrate_jsonl()
            except Exception as exc:
                logger.warning("初始化记忆数据库失败：%s", exc)

    def _create_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cases (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                hits INTEGER DEFAULT 1,
                query TEXT,
                aliases TEXT,
                task_plan TEXT,
                parsed_data TEXT,
                analysis TEXT,
                knowledge TEXT,
                conclusion TEXT,
                critic_verdict TEXT,
                critic_report TEXT,
                data_sources TEXT,
                tags TEXT,
                qvec TEXT,
                fvec TEXT
            )
            """
        )
        self._conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_cases_created ON cases(created_at)")

    def _migrate_jsonl(self) -> None:
        """启动时一次性把旧 cases.jsonl 导入 SQLite，避免历史案例丢失。"""
        if self._conn.execute("SELECT value FROM meta WHERE key='jsonl_migrated'").fetchone():
            return
        # 若库内已有案例，认为无需迁移（例如用户已手工导入）
        if self._conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] > 0:
            self._conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('jsonl_migrated','1')")
            self._conn.commit()
            return

        legacy = self.path.with_suffix(".jsonl")
        if not legacy.exists():
            legacy = LEGACY_JSONL_PATH
        migrated = 0
        if legacy.exists():
            try:
                for line in legacy.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        old = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(old, dict):
                        continue
                    query = str(old.get("query") or "")
                    qvec = _vectorize(query)
                    full_text = " ".join([
                        query,
                        str(old.get("analysis") or ""),
                        str(old.get("conclusion") or ""),
                        " ".join(str(t) for t in old.get("tags", [])),
                    ])
                    tags = [str(t) for t in old.get("tags", [])][:20]
                    created = str(old.get("created_at") or time.strftime("%Y-%m-%d %H:%M:%S"))
                    self._conn.execute(
                        """INSERT OR REPLACE INTO cases
                        (id, created_at, updated_at, hits, query, aliases, task_plan, parsed_data,
                         analysis, knowledge, conclusion, critic_verdict, critic_report,
                         data_sources, tags, qvec, fvec)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            str(old.get("id") or hashlib.md5(f"{query}{migrated}".encode()).hexdigest()[:12]),
                            created, created, 1, query, json.dumps([query], ensure_ascii=False),
                            str(old.get("task_plan") or ""), str(old.get("parsed_data") or ""),
                            str(old.get("analysis") or ""), str(old.get("knowledge") or ""),
                            str(old.get("conclusion") or ""), str(old.get("critic_verdict") or ""),
                            str(old.get("critic_report") or ""),
                            json.dumps(old.get("data_sources") or {}, ensure_ascii=False),
                            json.dumps(tags, ensure_ascii=False),
                            json.dumps(qvec), json.dumps(_vectorize(full_text)),
                        ),
                    )
                    migrated += 1
            except Exception as exc:
                logger.warning("迁移旧记忆文件失败：%s", exc)
        self._conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('jsonl_migrated','1')")
        self._conn.commit()
        if migrated:
            logger.info("已从旧 JSONL 迁移 %d 条记忆案例", migrated)

    @staticmethod
    def _row_to_case(row: sqlite3.Row) -> dict:
        case = dict(row)
        case["tags"] = _json(case.get("tags"), [])
        case["aliases"] = _json(case.get("aliases"), [])
        case["data_sources"] = _json(case.get("data_sources"), {})
        case.pop("qvec", None)
        case.pop("fvec", None)
        return case

    def save_case(
        self,
        query,
        *,
        task_plan="",
        parsed_data="",
        analysis="",
        knowledge="",
        conclusion="",
        critic_verdict="",
        critic_report="",
        data_sources=None,
        tags=None,
    ) -> str:
        query = str(query or "")
        tags = [str(t) for t in (tags or [])][:20]
        qvec = _vectorize(query)
        full_text = " ".join([query, str(analysis or ""), str(conclusion or ""), " ".join(tags)])
        fvec = _vectorize(full_text)

        try:
            with self._lock:
                # 语义近似去重：与库内所有案例的 query 向量比对，命中阈值则合并更新
                rows = self._conn.execute("SELECT * FROM cases").fetchall()
                best_row = None
                best_score = 0.0
                for row in rows:
                    # 用原始 query + 历史别名 + 结论做语义近似判定，覆盖"换问法"场景
                    row_text = " ".join([
                        str(row["query"] or ""),
                        " ".join(str(a) for a in _json(row["aliases"], [])),
                        str(row["analysis"] or ""),
                        str(row["conclusion"] or ""),
                    ])
                    score = _semantic_similarity(query, row_text)
                    if score > best_score:
                        best_score = score
                        best_row = row

                if best_row is not None and best_score >= DEDUP_THRESHOLD:
                    case_id = best_row["id"]
                    aliases = _json(best_row["aliases"], [])
                    if query and query not in aliases:
                        aliases.append(query)
                    merged_tags = list(dict.fromkeys(_json(best_row["tags"], []) + tags))[:20]
                    old_qvec = _load_vector(best_row["qvec"])
                    old_fvec = _load_vector(best_row["fvec"])
                    merged_qvec = _merge_vectors([old_qvec, qvec])
                    merged_fvec = _merge_vectors([old_fvec, fvec])

                    def pick(new_value, old_value):
                        return new_value if str(new_value or "").strip() else old_value

                    self._conn.execute(
                        """UPDATE cases SET
                           updated_at=?, hits=hits+1, query=?, aliases=?, tags=?,
                           task_plan=?, parsed_data=?, analysis=?, knowledge=?, conclusion=?,
                           critic_verdict=?, critic_report=?, data_sources=?, qvec=?, fvec=?
                           WHERE id=?""",
                        (
                            time.strftime("%Y-%m-%d %H:%M:%S"),
                            pick(query, best_row["query"]),
                            json.dumps(aliases, ensure_ascii=False),
                            json.dumps(merged_tags, ensure_ascii=False),
                            pick(task_plan, best_row["task_plan"]),
                            pick(parsed_data, best_row["parsed_data"]),
                            pick(analysis, best_row["analysis"]),
                            pick(knowledge, best_row["knowledge"]),
                            pick(conclusion, best_row["conclusion"]),
                            pick(critic_verdict, best_row["critic_verdict"]),
                            pick(critic_report, best_row["critic_report"]),
                            json.dumps(data_sources if isinstance(data_sources, dict) else _json(best_row["data_sources"], {}), ensure_ascii=False),
                            json.dumps(merged_qvec), json.dumps(merged_fvec),
                            case_id,
                        ),
                    )
                    self._conn.commit()
                    logger.info("记忆近似去重合并：id=%s, 相似度=%.3f, 累计=%d", case_id, best_score, best_row["hits"] + 1)
                    return case_id

                case_id = hashlib.md5(f"{query}{time.time_ns()}".encode("utf-8")).hexdigest()[:12]
                now = time.strftime("%Y-%m-%d %H:%M:%S")
                self._conn.execute(
                    """INSERT INTO cases
                    (id, created_at, updated_at, hits, query, aliases, task_plan, parsed_data,
                     analysis, knowledge, conclusion, critic_verdict, critic_report,
                     data_sources, tags, qvec, fvec)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        case_id, now, now, 1, query, json.dumps([query], ensure_ascii=False),
                        str(task_plan or "")[:4000], str(parsed_data or "")[:8000],
                        str(analysis or "")[:8000], str(knowledge or "")[:8000],
                        str(conclusion or "")[:8000], str(critic_verdict or ""),
                        str(critic_report or "")[:4000],
                        json.dumps(data_sources if isinstance(data_sources, dict) else {}, ensure_ascii=False),
                        json.dumps(tags, ensure_ascii=False),
                        json.dumps(qvec), json.dumps(fvec),
                    ),
                )
                self._conn.commit()
                logger.info("记忆案例已保存：id=%s", case_id)
                return case_id
        except Exception as exc:
            logger.warning("保存记忆案例失败：%s", exc)
            try:
                self._conn.rollback()
            except Exception:
                pass
            return ""

    def recall(self, query, top_k=3, min_score=0.05) -> list[tuple[float, dict]]:
        query_vec = _vectorize(query)
        scored: list[tuple[float, dict]] = []
        try:
            with self._lock:
                rows = self._conn.execute("SELECT * FROM cases").fetchall()
            for row in rows:
                score = _cosine(query_vec, _load_vector(row["fvec"]))
                if score >= min_score:
                    scored.append((score, self._row_to_case(row)))
        except Exception as exc:
            logger.warning("记忆召回失败：%s", exc)
            return []
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[:max(0, top_k)]

    def format_recall(self, query, top_k=3) -> str:
        hits = self.recall(query, top_k)
        if not hits:
            return "无历史相似案例。"
        parts = ["【历史相似案例召回】"]
        for index, (score, case) in enumerate(hits, start=1):
            parts.append(f"案例{index}（相似度{score:.3f}）：{str(case.get('query', ''))[:160]}")
            if case.get("conclusion"):
                parts.append(f"  既往结论：{str(case['conclusion'])[:300]}")
            if case.get("critic_verdict"):
                parts.append(f"  既往评审：{case['critic_verdict']}")
        return "\n".join(parts)

    def stats(self) -> dict:
        try:
            with self._lock:
                count = self._conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
        except Exception:
            count = 0
        return {"path": str(self.path), "count": count, "dedup_threshold": DEDUP_THRESHOLD}


_default_manager: MemoryManager | None = None


def get_memory_manager() -> MemoryManager:
    global _default_manager
    if _default_manager is None:
        _default_manager = MemoryManager()
    return _default_manager
