"""Phase1 主入口：解析用例 → 执行 → 输出报告。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from llm.gateway import LLMGateway
from orchestrator.engine import Orchestrator
from parser.case_parser import CaseParser
from tools.log_tool import LogToolAdapter
from tools.registry import ToolRegistry


def build_default_config() -> dict:
    return {
        "llm": {
            "provider": "mock",          # mock | ollama | openai
            "model": "qwen2.5",
            "base_url": "http://localhost:11434/v1",
            "api_key": "ollama",
            "temperature": 0.0,
        }
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="LTE/NR 智能测试助手 Phase1")
    ap.add_argument("--case", required=True, help="用例文件路径(.xlsx/.md)")
    ap.add_argument("--config", default=None, help="配置文件(可选)")
    ap.add_argument("--out", default="run_result.json", help="执行报告输出路径")
    ap.add_argument("--limit", type=int, default=0, help="只执行前N个用例(0=全部)")
    args = ap.parse_args()

    config = build_default_config()
    if args.config and Path(args.config).exists():
        import yaml
        user_cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
        config["llm"].update(user_cfg.get("llm", {}))

    llm = LLMGateway(config["llm"])
    parser = CaseParser(llm)

    registry = ToolRegistry()
    registry.register(LogToolAdapter(out_dir="logs"))

    engine = Orchestrator(registry)

    cases = parser.parse_file(args.case)
    print(f"解析到 {len(cases)} 个用例")
    if args.limit > 0:
        cases = cases[:args.limit]
        print(f"按 --limit={args.limit} 只执行前 {len(cases)} 个用例")
    reports = []
    for case in cases:
        print(f"\n=== 执行用例 {case.case_id}: {case.case_name} ===")
        for s in case.steps:
            print(f"  {s.seq}. {s.action.value} target={s.target} params={s.params}")
        run = engine.run(case)
        reports.append(run.summary())
        for r in run.step_results:
            print(f"  [step {r.seq}] {r.status} {r.action.value} "
                  f"elapsed={r.elapsed_sec}s out={r.output} err={r.error}")

    Path(args.out).write_text(
        json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入: {args.out}")


if __name__ == "__main__":
    main()
