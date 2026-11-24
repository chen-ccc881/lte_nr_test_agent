"""工具注册中心。"""
from __future__ import annotations

from typing import Any, Dict, List

from tools.base import ToolAdapter


class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, ToolAdapter] = {}

    def register(self, tool: ToolAdapter) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolAdapter:
        if name not in self._tools:
            raise KeyError(f"未注册工具: {name}，可用: {list(self._tools)}")
        return self._tools[name]

    def list_tools(self) -> List[str]:
        return sorted(self._tools)

    def get_function_schemas(self) -> List[Dict[str, Any]]:
        return [t.to_function_schema() for t in self._tools.values()]
