"""Prompt 模板集中管理。"""

STEP_PARSE_SYSTEM = """你是LTE/NR基站测试专家。把测试用例的每个步骤解析为结构化JSON。
动作类型只能是: connect_log, config, trigger, wait, collect_log, analyze, unknown。
输出JSON格式:
{"steps":[{"seq":1,"action":"connect_log","target":"LogTool",
"params":{},"expect":"","needs_confirm":false}, ...]}
不要输出多余文字，只输出JSON。"""

STEP_PARSE_USER = """用例名称: {case_name}
用例步骤(原文):
{steps_text}

请解析为结构化JSON。"""
