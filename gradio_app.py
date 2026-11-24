"""LTE/NR 多 Agent 测试平台 Gradio Web 前端。

职责边界：
- 后端业务逻辑、LangGraph 图、Agent/RAG/QXDM 工具全部在 lte_nr_agent.py；
- 本文件只负责：提交任务、展示实时日志、接收人工反馈、展示最终报告；
- 每个浏览器会话使用独立 thread_id，基于 LangGraph MemorySaver 实现断点恢复。
"""

from __future__ import annotations

import contextvars
import logging
import os
import queue
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Gradio 启动时会用 httpx 访问本机 startup-events；若系统代理指向不可用端口会报 WinError 10061。
# 后端模型客户端已使用 trust_env=False，这里清除代理只影响前端自检。
for _proxy_key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_proxy_key, None)
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

import gradio as gr

import lte_nr_agent as backend
from upload_manager import UploadManager


# ===================== 1. 按会话隔离的日志队列 =====================
CURRENT_SESSION = contextvars.ContextVar("current_session", default="system")
SESSION_QUEUES: dict[str, queue.Queue] = {}


class SessionQueueLogHandler(logging.Handler):
    """把 root logger 日志按会话写入独立队列，供前端轮询。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            session_id = CURRENT_SESSION.get()
            session_queue = SESSION_QUEUES.setdefault(session_id, queue.Queue())
            session_queue.put(message)
        except Exception:
            self.handleError(record)


queue_handler = SessionQueueLogHandler()
queue_handler.setFormatter(logging.Formatter("%%H:%%M:%%S [%(levelname)s] %(name)s: %(message)s"))
logging.getLogger().addHandler(queue_handler)


# ===================== 2. 会话状态管理 =====================
@dataclass
class WebSession:
    session_id: str
    thread_id: str | None = None
    running: bool = False
    need_human: bool = False
    question: str = ""
    feedback: str = ""
    result: dict[str, Any] | None = None
    error: str | None = None
    log_text: str = ""
    upload_info: str = ""
    upload_mgr: UploadManager | None = None


SESSIONS: dict[str, WebSession] = {}
SESSION_LOCK = threading.Lock()


def get_session(session_id: str | None) -> tuple[str, WebSession]:
    """获取或创建浏览器会话；session_id 为 None 时创建新会话。"""
    with SESSION_LOCK:
        sid = session_id or str(uuid.uuid4())
        if sid not in SESSIONS:
            SESSIONS[sid] = WebSession(session_id=sid)
            SESSION_QUEUES.setdefault(sid, queue.Queue())
        return sid, SESSIONS[sid]


def append_log(session: WebSession, message: str) -> None:
    """追加前端日志，限制最大长度避免浏览器内存膨胀。"""
    session.log_text = (session.log_text + "\n" + message).strip()[-20000:]


def drain_session_logs(session: WebSession) -> None:
    """拉取当前会话队列中的新日志。"""
    log_queue = SESSION_QUEUES.setdefault(session.session_id, queue.Queue())
    while True:
        try:
            append_log(session, log_queue.get_nowait())
        except queue.Empty:
            break


def status_text(session: WebSession) -> str:
    if session.error:
        return f"**状态：失败**  \n{session.error}"
    if session.need_human:
        return "**状态：等待人工补充参数**  \n请在左侧弹窗中填写反馈并提交。"
    if session.running:
        return "**状态：运行中**  \n专家Agent正在分析（数据解析/无线推理/知识检索），日志会实时刷新。"
    if session.result is not None:
        return "**状态：完成**  \n最终报告已生成。"
    return "**状态：空闲**  \n请输入LTE/NR测试任务后点击“启动任务”。"


# ===================== 3. 上传文件累积（支持多次拖入） =====================
def _extract_upload_paths(files) -> list[str]:
    """兼容 Gradio 6 FileData 对象、dict、str、Path，提取本地临时路径。"""
    if not files:
        return []
    if isinstance(files, (str, Path)):
        return [str(files)]
    paths = []
    for item in files:
        path = getattr(item, "path", None)
        if path is None and isinstance(item, dict):
            path = item.get("path")
        if path is None:
            path = str(item)
        paths.append(str(path))
    return paths


def stage_uploads(new_files, accumulated: list[str] | None, session_id: str | None):
    """把本次拖入的文件复制到会话暂存区并累积，返回更新后的列表。

    Gradio 的 gr.File(multiple) 每次选择会替换旧值，因此用 upload 事件 +
    State 累积：文件立即从 Gradio 临时目录复制到我们自己的会话目录，
    避免 Gradio 清理临时文件导致丢失。返回时清空文件框，便于再次拖入。
    """
    sid, _ = get_session(session_id)
    accumulated = list(accumulated or [])
    incoming = _extract_upload_paths(new_files)
    if not incoming:
        return accumulated, sid, gr.update()

    stage_dir = Path("artifacts/uploads/temp") / sid / "staged"
    stage_dir.mkdir(parents=True, exist_ok=True)
    seen = {Path(p).name for p in accumulated}
    for src in incoming:
        src_path = Path(src)
        if not src_path.exists():
            continue
        name = src_path.name
        if name in seen:
            continue  # 同名文件只保留首次
        dst = stage_dir / name
        try:
            shutil.copy2(str(src_path), str(dst))
        except Exception as exc:
            logging.getLogger("lte_nr_agent").warning("暂存上传文件失败：%s", exc)
            continue
        accumulated.append(str(dst))
        seen.add(name)
    # 清空文件框，使组件回到可接收新拖入的状态
    return accumulated, sid, gr.update(value=[])


# ===================== 4. 后端任务包装：启动与断点恢复 =====================
def start_task(user_query: str, files, session_id: str | None):
    """启动新任务；上传文件按生命周期落地，临时日志任务结束即删，经验文档长期保留。"""
    sid, session = get_session(session_id)
    if session.running:
        append_log(session, "已有任务运行中，忽略重复启动请求")
        return sid, status_text(session), []

    # 1) 落地上传文件：临时日志 / 长期经验文档
    mgr = UploadManager(sid)
    session.upload_mgr = mgr
    handled = mgr.handle_uploads(files, keep_experience=True)
    suffix = mgr.build_query_suffix(handled)
    full_query = f"{user_query}{suffix}"
    if handled["details"]:
        session.upload_info = "\n".join(
            f"- {d['name']}：{d['category']}（{'长期入库' if d['persist'] else '临时日志，用完即删'}）→ {d['path']}"
            for d in handled["details"]
        )
        append_log(session, "上传文件处理：\n" + session.upload_info)
    else:
        session.upload_info = "本次未上传文件"

    session.thread_id = f"lte-nr-{uuid.uuid4()}"
    session.running = True
    session.need_human = False
    session.question = ""
    session.feedback = ""
    session.result = None
    session.error = None
    session.log_text = (session.log_text + "\n").strip() if session.log_text else ""
    append_log(session, f"{time.strftime('%H:%M:%S')} 任务已提交，thread_id={session.thread_id}")

    def cleanup_temp():
        if session.upload_mgr:
            removed = session.upload_mgr.cleanup()
            if removed:
                append_log(session, f"已清理本次临时日志 {removed} 个（经验文档已保留）")

    def worker():
        token = CURRENT_SESSION.set(sid)
        try:
            result = backend.start_graph_task(full_query, session.thread_id)
            question = backend.extract_interrupt_question(result)
            if question:
                session.need_human = True
                session.question = question
                append_log(session, "Orchestrator触发Human-in-Loop，等待工程师反馈")
            else:
                session.result = result
                append_log(session, "任务完成，已生成汇总报告")
                cleanup_temp()
        except Exception as exc:
            session.error = f"{type(exc).__name__}: {exc}"
            append_log(session, f"任务失败：{session.error}")
            logging.getLogger("lte_nr_agent").exception("Web任务失败：%s", exc)
            cleanup_temp()
        finally:
            session.running = False
            CURRENT_SESSION.reset(token)

    threading.Thread(target=worker, name=f"web-task-{sid[:8]}", daemon=True).start()
    # 任务已接手文件，清空累积列表，避免下次启动重复处理
    return sid, status_text(session), []


def submit_feedback(feedback: str, session_id: str | None):
    """向暂停的 Human-in-Loop 节点提交反馈并恢复图执行。

    使用 generator 持续 yield：恢复后的日志、状态和最终报告会直接推给当前页面，
    不再只依赖 Timer 轮询，避免人工反馈后报告不刷新。
    """
    sid, session = get_session(session_id)
    if session.running or not session.need_human or not session.thread_id:
        yield status_text(session), gr.update(visible=False), "", session.log_text, ""
        return

    session.feedback = feedback
    session.need_human = False
    session.running = True
    session.question = ""
    append_log(session, f"收到工程师反馈：{feedback[:200]}")

    def worker():
        token = CURRENT_SESSION.set(sid)
        try:
            result = backend.resume_graph_task(feedback, session.thread_id)
            question = backend.extract_interrupt_question(result)
            if question:
                session.need_human = True
                session.question = question
                append_log(session, "恢复后再次触发Human-in-Loop")
            else:
                session.result = result
                append_log(session, "工程师反馈已提交，任务恢复并完成")
                if session.upload_mgr:
                    removed = session.upload_mgr.cleanup()
                    if removed:
                        append_log(session, f"已清理本次临时日志 {removed} 个（经验文档已保留）")
        except Exception as exc:
            session.error = f"{type(exc).__name__}: {exc}"
            append_log(session, f"恢复任务失败：{session.error}")
            logging.getLogger("lte_nr_agent").exception("Web恢复任务失败：%s", exc)
        finally:
            session.running = False
            CURRENT_SESSION.reset(token)

    threading.Thread(target=worker, name=f"web-resume-{sid[:8]}", daemon=True).start()

    while session.running:
        drain_session_logs(session)
        report = session.result.get("final_answer", "") if session.result else ""
        yield (
            status_text(session),
            gr.update(visible=session.need_human),
            "",
            session.log_text,
            report,
        )
        time.sleep(0.5)

    drain_session_logs(session)
    report = session.result.get("final_answer", "") if session.result else ""
    yield (
        status_text(session),
        gr.update(visible=session.need_human),
        "",
        session.log_text,
        report,
    )


def poll_ui(session_id: str | None):
    """定时轮询：刷新日志、状态、人工弹窗和最终报告。"""
    _, session = get_session(session_id)
    drain_session_logs(session)
    report = session.result.get("final_answer", "") if session.result else ""
    return (
        session.log_text,
        status_text(session),
        gr.update(visible=session.need_human),
        session.question,
        report,
    )


# ===================== 4. Gradio 页面 =====================
with gr.Blocks(title="LTE/NR 多Agent测试平台", analytics_enabled=False) as demo:
    gr.Markdown(
        "# LTE/NR 多Agent测试平台\n"
        "Orchestrator总指挥 + DataParser数据解析 + RadioAnalyst无线推理 + KnowledgeRetriever知识检索 + Critic评审 + Memory长期记忆 + Human-in-the-Loop"
    )

    session_state = gr.State(value=None)
    staged_files_state = gr.State(value=[])

    with gr.Row():
        with gr.Column(scale=1):
            query_box = gr.Textbox(
                label="LTE/NR 排障任务需求",
                lines=8,
                placeholder="例如：解析上传的QXDM日志定位N28接入失败根因；或：读取WebLMT配置校验PRACH参数；或：基于经验库分析切换失败",
            )
            file_box = gr.File(
                label="上传日志或经验文档（可多选）",
                file_count="multiple",
                file_types=[".isf", ".isfx", ".dlf", ".hdf", ".h5", ".bin", ".pcap", ".pcapng",
                            ".xst", ".bei", ".log", ".txt", ".csv", ".xlsx", ".xls",
                            ".md", ".docx", ".doc", ".pdf"],
            )
            gr.Markdown(
                "> 文件处理规则：日志类（QXDM/pcap/Xtrace/SV/device log）为临时数据，任务结束自动删除；"
                "经验文档（Markdown/Word/Excel/PDF）长期保存到本地经验库供后续复用。后台按格式自动匹配解析器。"
            )
            start_button = gr.Button("启动任务", variant="primary")
            status_md = gr.Markdown(value=status_text(WebSession(session_id="preview")))

            with gr.Group(visible=False) as human_group:
                gr.Markdown("### 需要工程师补充信息")
                question_md = gr.Markdown()
                feedback_box = gr.Textbox(label="工程师反馈", lines=4)
                resume_button = gr.Button("提交反馈并继续", variant="primary")

        with gr.Column(scale=2):
            log_box = gr.Textbox(label="实时日志", lines=22, interactive=False)
            report_box = gr.Markdown(label="最终汇总测试报告")

    # 每次拖入/选择文件都立即暂存并累积，随后清空文件框以便继续拖入补充文件
    file_box.upload(
        fn=stage_uploads,
        inputs=[file_box, staged_files_state, session_state],
        outputs=[staged_files_state, session_state, file_box],
    )
    start_button.click(
        fn=start_task,
        inputs=[query_box, staged_files_state, session_state],
        outputs=[session_state, status_md, staged_files_state],
    )
    resume_button.click(
        fn=submit_feedback,
        inputs=[feedback_box, session_state],
        outputs=[status_md, human_group, feedback_box, log_box, report_box],
    )

    timer = gr.Timer(1.0)
    timer.tick(
        fn=poll_ui,
        inputs=[session_state],
        outputs=[log_box, status_md, human_group, question_md, report_box],
    )


# ===================== 5. 启动入口 =====================
if __name__ == "__main__":
    host = os.getenv("GRADIO_HOST", "0.0.0.0")
    port = int(os.getenv("GRADIO_PORT", "7860"))
    demo.queue(max_size=20)
    demo.launch(
        server_name=host,
        server_port=port,
        share=False,
    )
