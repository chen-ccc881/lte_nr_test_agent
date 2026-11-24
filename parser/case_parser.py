"""测试用例解析器：真实模板 Excel + Markdown，LLM 辅助结构化。

真实模板特征（每个用例一行，步骤在单元格内用换行分隔）：
  用例编号 | 测试项目 | 测试子项 | 测试用例标题 | 测试目的 | 预置条件 |
  测试步骤 | 预期结果 | 优先级 | 实测结果 | 测试结果说明 | 测试人员 | 测试日期 | 备注

同时兼容旧骨架格式（每步骤一行，列 case_id/case_name/step_text）。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, List

import pandas as pd

from llm.gateway import LLMGateway
from llm.prompts import STEP_PARSE_SYSTEM, STEP_PARSE_USER
from models.schema import ActionType, TestCase, TestStep

# 真实模板列名
COL_CASE_ID = "用例编号"
COL_CASE_NAME = "测试用例标题"
COL_PRECOND = "预置条件"
COL_STEPS = "测试步骤"
COL_EXPECTED = "预期结果"
META_COLUMNS = [
    "测试项目", "测试子项", "测试目的", "优先级",
    "实测结果", "测试结果说明", "测试人员", "测试日期", "备注",
]

# 单元格内步骤行首编号，如 "1." "2、" "3)" "（4）"
STEP_PREFIX_RE = re.compile(r"^\s*(?:\d+\s*[\.、\)）]|[（(]\s*\d+\s*[)）])\s*")


class CaseParser:
    def __init__(self, llm: LLMGateway):
        self.llm = llm

    def parse_file(self, path: str) -> List[TestCase]:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"用例文件不存在: {p}")
        suffix = p.suffix.lower()
        if suffix in (".xlsx", ".xls"):
            return self._parse_excel(p)
        if suffix == ".md":
            return self._parse_markdown(p)
        raise ValueError(f"不支持的用例格式: {suffix}，支持 .xlsx/.md")

    # ---------- Excel ----------
    def _parse_excel(self, path: Path) -> List[TestCase]:
        df = pd.read_excel(path, dtype=str).fillna("")
        if COL_STEPS in df.columns:
            return self._parse_real_excel(df, path)
        return self._parse_skeleton_excel(df, path)

    def _parse_real_excel(self, df: pd.DataFrame, path: Path) -> List[TestCase]:
        """真实模板：每个用例一行，步骤/预置条件在单元格内换行分隔。"""
        required = {COL_CASE_ID, COL_CASE_NAME, COL_STEPS}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"真实模板缺少列: {sorted(missing)}")

        cases: List[TestCase] = []
        for _, row in df.iterrows():
            case_id = str(row.get(COL_CASE_ID, "")).strip()
            if not case_id:
                continue
            case_name = str(row.get(COL_CASE_NAME, "")).strip()
            precondition_lines = self._split_lines(row.get(COL_PRECOND, ""))
            step_lines = self._split_lines(row.get(COL_STEPS, ""))
            expected = str(row.get(COL_EXPECTED, "")).strip()

            steps_text = "\n".join(
                f"{i + 1}. {line}" for i, line in enumerate(step_lines)
            )
            case = self._build_case(case_id, case_name, steps_text, str(path))
            case.preconditions = precondition_lines
            case.expected_result = expected
            case.metadata = {
                col: str(row[col]).strip()
                for col in META_COLUMNS
                if col in df.columns and str(row.get(col, "")).strip()
            }
            # 用真实原文回填 raw_text，覆盖 LLM 可能丢失的原始步骤描述
            for step, raw in zip(case.steps, step_lines):
                step.raw_text = raw
            cases.append(case)
        return cases

    def _parse_skeleton_excel(self, df: pd.DataFrame, path: Path) -> List[TestCase]:
        """旧骨架格式：每行一个步骤，按 case_id 分组。"""
        required = {"case_id", "case_name", "step_text"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(
                f"Excel 列不匹配：既非真实模板(需 '测试步骤')，"
                f"也非骨架模板(缺 {sorted(missing)})"
            )
        cases: List[TestCase] = []
        for cid, group in df.groupby("case_id", sort=False):
            steps_text = "\n".join(
                f"{int(row.step_seq) if 'step_seq' in df.columns else i + 1}. {row.step_text}"
                for i, (_, row) in enumerate(group.iterrows())
            )
            cases.append(self._build_case(
                str(cid), str(group.iloc[0].case_name), steps_text, str(path)))
        return cases

    @staticmethod
    def _split_lines(text: Any) -> List[str]:
        """拆分单元格内的多行文本，并去掉行首编号。"""
        if text is None:
            return []
        lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
        out: List[str] = []
        for line in lines:
            cleaned = STEP_PREFIX_RE.sub("", line).strip()
            if cleaned:
                out.append(cleaned)
        return out

    # ---------- Markdown ----------
    def _parse_markdown(self, path: Path) -> List[TestCase]:
        text = path.read_text(encoding="utf-8")
        blocks = [b for b in text.split("# 用例") if b.strip()]
        cases: List[TestCase] = []
        for i, block in enumerate(blocks, start=1):
            lines = [l.strip() for l in block.splitlines() if l.strip()]
            name = lines[0].lstrip("# ").strip() if lines else f"CASE_{i}"
            steps = [l.lstrip("- ").strip() for l in lines if l.startswith("-")]
            case = self._build_case(
                f"CASE_{i:03d}", name,
                "\n".join(f"{j + 1}. {s}" for j, s in enumerate(steps)),
                str(path),
            )
            cases.append(case)
        return cases

    # ---------- 通用 ----------
    def _build_case(self, case_id: str, case_name: str, steps_text: str,
                    source_file: str) -> TestCase:
        prompt = STEP_PARSE_USER.format(case_name=case_name, steps_text=steps_text)
        parsed = self.llm.parse_structured(
            f"{STEP_PARSE_SYSTEM}\n\n{prompt}"
        )
        steps: List[TestStep] = []
        for item in parsed.get("steps", []):
            try:
                action = ActionType(item.get("action", "unknown"))
            except ValueError:
                action = ActionType.UNKNOWN
            steps.append(TestStep(
                seq=int(item.get("seq", len(steps) + 1)),
                action=action,
                target=item.get("target"),
                params=item.get("params", {}),
                expect=item.get("expect"),
                raw_text=item.get("raw_text"),
                needs_confirm=bool(item.get("needs_confirm", False)),
            ))
        return TestCase(case_id=case_id, case_name=case_name, steps=steps,
                        source_file=source_file)
