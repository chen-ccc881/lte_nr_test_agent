"""工具适配器抽象基类。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict


class ToolAdapter(ABC):
    name: str = "base"
    description: str = ""
    actions: Dict[str, str] = {}

    @abstractmethod
    def connect(self, config: Dict[str, Any]) -> bool:
        """建立到工具的连接。"""

    @abstractmethod
    def execute(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """执行动作，返回 {ok, data, error}。"""

    @abstractmethod
    def health_check(self) -> bool:
        """健康检查。"""

    def collect(self, query: Dict[str, Any]) -> Dict[str, Any]:
        return self.execute("collect_log", query)

    def to_function_schema(self) -> Dict[str, Any]:
        """转换为 OpenAI Function Calling 格式，供 LLM 决策。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": list(self.actions)},
                        "params": {"type": "object"},
                    },
                    "required": ["action"],
                },
            },
        }
