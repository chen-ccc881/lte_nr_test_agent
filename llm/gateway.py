"""LLM 网关：统一封装 Ollama / OpenAI / Mock。"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from openai import OpenAI

from models.schema import ToolCall

# Mock 模式的关键词→动作映射（仅用于离线验证；真实语义判断由 LLM 完成）
# 真实用例步骤较长且含多个关键词，动作通常在句首，故分两级：句首优先、全文兜底。
_ACTION_HEAD = [
    ("config", ["配置", "设置", "修改", "关闭", "开启", "删除", "加载", "下发"]),
    ("trigger", ["触发", "发起", "启动", "重启", "上电"]),
    ("wait", ["等待", "保持", "持续", "延时"]),
    ("connect_log", ["连接日志", "开启日志", "信令跟踪", "启动跟踪"]),
]
_ACTION_FULL = [
    ("collect_log", ["导出日志", "保存日志", "抓取日志", "记录日志"]),
    ("analyze", ["观察", "检查", "确认", "验证", "查看", "判断"]),
]


def _mock_guess_action(text: str) -> str:
    head = text[:25]
    for action, keywords in _ACTION_HEAD:
        if any(k in head for k in keywords):
            return action
    # 连接类：句首"连接/打开"且提到日志/log
    if (head.startswith("连接") or head.startswith("打开")) and \
            ("日志" in text or "log" in text.lower()):
        return "connect_log"
    for action, keywords in _ACTION_FULL:
        if any(k in text for k in keywords):
            return action
    return "unknown"


def _mock_guess_params(text: str, action: str) -> Dict[str, Any]:
    if action == "wait":
        m = re.search(r"(\d+)\s*秒", text) or re.search(r"(\d+)\s*分钟", text)
        return {"seconds": min(int(m.group(1)) if m else 1, 5)}
    if action == "collect_log":
        m = re.search(r"(\d+)\s*秒", text)
        return {"duration_sec": min(int(m.group(1)) if m else 2, 3)}
    if action == "connect_log":
        low = text.lower()
        ifaces = []
        if "s1" in low:
            ifaces.append("S1")
        if "x2" in low or "xn" in low:
            ifaces.append("Xn")
        if "网管" in text:
            ifaces.append("OM")
        return {"interface": ",".join(ifaces) if ifaces else "Uu"}
    return {}


class LLMGateway:
    def __init__(self, config: Dict[str, Any]):
        self.provider = config.get("provider", "mock")
        self.model = config.get("model", "qwen2.5")
        self.base_url = config.get("base_url", "http://localhost:11434/v1")
        self.api_key = config.get("api_key", "ollama")
        self.temperature = config.get("temperature", 0.0)
        self._client: Optional[OpenAI] = None

        if self.provider in ("ollama", "openai"):
            self._client = OpenAI(base_url=self.base_url, api_key=self.api_key)

    def chat(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        response_format: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        if self.provider == "mock":
            return self._mock_chat(messages)

        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if response_format and self.provider == "openai":
            kwargs["response_format"] = response_format

        resp = self._client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        result: Dict[str, Any] = {"content": msg.content or ""}
        if getattr(msg, "tool_calls", None):
            result["tool_calls"] = [tc.function.arguments for tc in msg.tool_calls]
        return result

    def parse_structured(self, prompt: str, schema_hint: str = "") -> Dict[str, Any]:
        """要求 LLM 返回 JSON，并做健壮解析。"""
        raw = self.chat([
            {"role": "system", "content": "只输出合法JSON，不要解释。"},
            {"role": "user", "content": prompt},
        ])["content"]
        return self._extract_json(raw)

    def decide_tool(self, thought: str, tool_schemas: List[Dict]) -> ToolCall:
        """让 LLM 基于工具描述决定下一步调用（Phase1 提供骨架）。"""
        result = self.chat([
            {"role": "system",
             "content": "根据任务选择工具，输出JSON: {tool, action, params, thought}"},
            {"role": "user", "content": thought},
        ], tools=tool_schemas or None)
        if "tool_calls" in result:
            return ToolCall(**self._extract_json(result["tool_calls"][0]))
        return ToolCall(**self._extract_json(result["content"]))

    @staticmethod
    def _extract_json(text: str) -> Dict[str, Any]:
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise ValueError(f"无法从LLM输出解析JSON: {text[:200]}")

    @staticmethod
    def _mock_chat(messages: List[Dict[str, str]]) -> Dict[str, Any]:
        """离线验证：按输入步骤数动态生成结构化结果，关键词推断动作。"""
        user = next((m["content"] for m in messages if m["role"] == "user"), "")
        if "用例步骤" not in user and "解析" not in user:
            return {"content": "{}"}

        raw_steps: List[tuple] = []
        for line in user.splitlines():
            m = re.match(r"^\s*(\d+)\.\s*(.+)$", line)
            if m:
                raw_steps.append((int(m.group(1)), m.group(2).strip()))
        if not raw_steps:
            raw_steps = [(1, "连接日志工具"), (2, "等待5秒"), (3, "导出日志")]

        parsed = []
        for seq, raw in raw_steps:
            action = _mock_guess_action(raw)
            target = "LogTool" if action in ("connect_log", "collect_log") else None
            parsed.append({
                "seq": seq,
                "action": action,
                "target": target,
                "params": _mock_guess_params(raw, action),
                "expect": "",
                "raw_text": raw,
            })
        return {"content": json.dumps({"steps": parsed}, ensure_ascii=False)}
