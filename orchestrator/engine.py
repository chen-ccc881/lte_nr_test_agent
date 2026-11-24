"""编排引擎（Phase1 顺序执行版）。"""
from __future__ import annotations

import time
from datetime import datetime

from models.schema import ActionType, RunResult, StepResult, TestCase, TestStep
from tools.registry import ToolRegistry


class Orchestrator:
    def __init__(self, registry: ToolRegistry):
        self.registry = registry
        # 动作到工具的默认映射，Phase2 可由 LLM 动态决策
        self.action_tool_map = {
            "connect_log": "log_tool",
            "collect_log": "log_tool",
            "start_trace": "xtrace",
            "stop_trace": "xtrace",
            "start_sv": "sv",
            "stop_sv": "sv",
            "start_ue_log": "qxdm",
            "stop_ue_log": "qxdm",
            "export_device_log": "devicelog",
            "lmt_login": "weblmt",
            "lmt_config": "weblmt",
            "exec_cmd": "devicelog",
        }
        # 动作到适配器内部 action 的映射（工具名与动作名不一致时）
        self.action_alias = {
            "lmt_login": "login",
            "lmt_config": "config",
            "exec_cmd": "export",
        }

    def run(self, case: TestCase) -> RunResult:
        run = RunResult(case_id=case.case_id, status="running")
        for step in sorted(case.steps, key=lambda s: s.seq):
            result = self._execute_step(step)
            run.step_results.append(result)

        failed = [r for r in run.step_results if r.status == "error"]
        run.status = "failed" if failed else "success"
        run.finished_at = datetime.now().isoformat()
        return run

    def _execute_step(self, step: TestStep) -> StepResult:
        started = time.time()
        try:
            if step.action == ActionType.WAIT:
                sec = min(int(step.params.get("seconds", 1)), 5)  # 骨架上限
                time.sleep(sec)
                return StepResult(seq=step.seq, action=step.action, status="ok",
                                  output={"waited_sec": sec},
                                  elapsed_sec=round(time.time() - started, 3))

            tool_name = self.action_tool_map.get(step.action.value)
            if tool_name is None:
                return StepResult(
                    seq=step.seq, action=step.action, status="skipped",
                    output=f"Phase1未映射动作: {step.action.value}",
                    elapsed_sec=round(time.time() - started, 3),
                )

            tool = self.registry.get(tool_name)
            adapter_action = self.action_alias.get(step.action.value, step.action.value)
            outcome = tool.execute(adapter_action, step.params)
            return StepResult(
                seq=step.seq, action=step.action,
                status="ok" if outcome.get("ok") else "error",
                output=outcome.get("data"), error=outcome.get("error"),
                elapsed_sec=round(time.time() - started, 3),
            )
        except Exception as exc:
            return StepResult(seq=step.seq, action=step.action, status="error",
                              error=f"{type(exc).__name__}: {exc}",
                              elapsed_sec=round(time.time() - started, 3))
