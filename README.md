# LTE/NR 智能测试 Agent

面向 LTE/NR 无线空口测试与排障的智能化 Agent 系统。以大语言模型为推理核心，通过 LangGraph 编排多专家 Agent，统一接入日志、配置、协议文档、经验案例等多源数据，完成数据解析、信令核查、参数计算、协议检索、结论汇总与经验沉淀的端到端闭环。

## 功能特性

- **多源日志解析**：XST 信令、QXDM/HDF（QCAT CLI）、SV/EI、pcap、CMAC/PDCP/RLC TTI CSV、WebLMT 配置
- **多专家编排**：Orchestrator 总指挥 + DataParser 数据解析 + RadioAnalyst 无线推理 + KnowledgeRetriever 协议检索
- **协议 RAG**：本地 3GPP 文档向量检索（语义向量 / 哈希双模式，失败自动降级）
- **长期记忆**：SQLite 持久化 + 语义近似去重，自动沉淀历史案例
- **Web 界面**：Gradio 交互式问答，支持多次拖入上传、实时日志、人工反馈中断
- **可配置**：Critic 评审开关、embedding 方案、QCAT 分析器、各超时参数均可通过环境变量调整

## 快速开始

### 1. 环境准备

```powershell
# Python 3.10+
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. 配置密钥

```powershell
copy .env.example .env
# 编辑 .env，填入 HUNYUAN_API_KEY
```

### 3. 准备本地资料（可选）

将 3GPP 协议文档（如 `36211-d30.doc`）放入 `docs/3gpp/`，系统启动时会构建 RAG 索引。文档因版权原因不随仓库分发。

### 4. 启动

```powershell
python gradio_app.py
```

浏览器访问 `http://localhost:7860`。

## 项目结构

| 目录/文件 | 说明 |
|---|---|
| `lte_nr_agent.py` | 主编排核心：LangGraph 图、工具、LLM 初始化、RAG |
| `gradio_app.py` | Web 前端（会话隔离、上传管理、人工反馈） |
| `upload_manager.py` | 上传文件分类与暂存 |
| `data_sources.py` | 多源解析与数据源探测 |
| `csv_tti_analyzer.py` | CMAC/PDCP/RLC TTI CSV 分析 |
| `qcat_adapter.py` | QCAT CLI 结构化导出适配器 |
| `memory_manager.py` | SQLite 长期记忆 + 语义去重 |
| `protocols/` | XST/SV/pcap 解码与 RAG embedding 实现 |
| `orchestrator/`, `parser/`, `llm/`, `models/`, `tools/`, `log_analyzer/` | Phase1 框架与独立子系统 |
| `requirements.txt` | 依赖清单 |
| `config.yaml`, `env_config.yaml` | 静态配置 |

## 主要环境变量

见 `.env.example`。关键项：

- `ENABLE_CRITIC=1` 开启 Critic 评审（默认关闭）
- `RAG_EMBEDDING_MODE=hash` 强制离线哈希向量（不调用远端 embedding）
- `LTE_NR_SUB_AGENT_TIMEOUT` 子 Agent 超时（默认 300s）
- `QCAT_EXE` / `QCAT_ANALYZERS` 本机 QCAT 配置

## 外部依赖

- **混元/tokenhub API**：需有效的 `HUNYUAN_API_KEY`
- **Qualcomm QCAT 6.x**（可选）：解析 QXDM/HDF 日志需要本机安装
- **WPS/Office COM**（可选）：读取 `.doc` 格式协议文档需要本机安装

## 注意事项

- `.env` 含密钥，已加入 `.gitignore`，切勿提交
- `.chroma/` 向量库为运行时生成，首次启动自动构建
- 3GPP 协议文档版权归标准组织所有，请自行合法获取
