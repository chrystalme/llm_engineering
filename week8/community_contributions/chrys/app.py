"""Gradio UI for ARIA: agent-colored log stream and results table."""
import html
import json
import logging
import queue
import sys
import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

# Ensure chrys is on path and .env is loaded from this directory
_this_dir = os.path.dirname(os.path.abspath(__file__))
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)
os.chdir(_this_dir)

import gradio as gr
from dotenv import load_dotenv
load_dotenv(os.path.join(_this_dir, ".env"))

from models import DecisionRecord

# Max time to wait for the entire Modal pipeline (cold start + run).
MODAL_CALL_TIMEOUT_SEC = 330


def _run_pipeline(log_queue: queue.Queue | None = None):
    """Run pipeline locally or on Modal when USE_MODAL=1.

    Modal path: iterates remote_gen(), routing log entries into log_queue in
    real time so the Gradio UI shows a live per-agent colored stream.
    Returns (records, asset_data).
    """
    use_modal = os.getenv("USE_MODAL", "").strip() not in ("", "0", "false", "False")

    if use_modal:
        import modal
        from models import AssetData

        aria_log = logging.getLogger("aria")
        aria_log.info(
            "Running pipeline on Modal... (first run can take 1–2 min while the container starts)"
        )

        try:
            fn = modal.Function.from_name("aria-pipeline", "run_pipeline_remote")
        except Exception as e:
            raise RuntimeError(f"Could not look up Modal function: {e}") from e

        records = []
        asset_data_list = []
        summary: dict = {}

        try:
            # remote_gen() streams yielded items as they arrive from the container.
            # Each item is a JSON string with a "type" field.
            for raw in fn.remote_gen():
                item = json.loads(raw)

                if item["type"] == "log":
                    # Forward to the Gradio log queue in real time
                    if log_queue is not None:
                        log_queue.put((item["name"], item["msg"]))

                elif item["type"] == "result":
                    records = [DecisionRecord(**r) for r in item["records"]]
                    asset_data_list = [AssetData(**a) for a in item["asset_data"]]
                    summary = item.get("summary") or {}

                elif item["type"] == "error":
                    raise RuntimeError(f"Modal pipeline error: {item['msg']}")

        except Exception as e:
            err = str(e).lower()
            if "token" in err or "auth" in err or "unauthorized" in err or "403" in err:
                aria_log.error(
                    "Modal call failed (auth): %s — run 'modal token new' in this environment.", e
                )
            else:
                aria_log.error("Modal call failed: %s", e)
            raise

        n_fetched = summary.get("n_fetched", len(asset_data_list))
        if n_fetched == 0 and summary.get("hint"):
            aria_log.warning(summary["hint"])
        elif not records and n_fetched > 0:
            aria_log.info("Modal pipeline finished (no alerts this run).")
        else:
            aria_log.info(
                "Modal pipeline finished: %d assets, %d decisions.",
                n_fetched, len(records),
            )

        from db import log_decisions
        log_decisions(records)
        return records, asset_data_list

    # Local run — logs are emitted live via AgentQueueHandler already attached
    from orchestrator import run_pipeline
    return run_pipeline()


# Agent name + color for UI (logger name -> (display name, hex color))
AGENT_COLORS = {
    "aria.data_fetcher":    ("DATA_FETCHER",    "#3498db"),
    "aria.tech_analyst":    ("TECH_ANALYST",    "#27ae60"),
    "aria.sentiment_agent": ("SENTIMENT_AGENT", "#f39c12"),
    "aria.decision_agent":  ("DECISION_AGENT",  "#9b59b6"),
    "aria.notifier":        ("NOTIFIER",        "#1abc9c"),
}
DEFAULT_COLOR = "#bdc3c7"
MAX_LOG_LINES = 150


class AgentQueueHandler(logging.Handler):
    """Put (logger_name, formatted_message) into queue for colored UI."""
    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            self.log_queue.put((record.name, msg))
        except Exception:
            self.handleError(record)


def agent_line_to_html(logger_name: str, message: str) -> str:
    label, color = AGENT_COLORS.get(logger_name, ("ARIA", DEFAULT_COLOR))
    safe_msg = html.escape(message)
    return f'<span style="color:{color};font-weight:600">[{label}]</span> {safe_msg}'


def html_for_log(log_lines: list) -> str:
    recent = log_lines[-MAX_LOG_LINES:]
    content = "<br>".join(recent)
    return f"""
    <div style="height:420px;overflow-y:auto;border:1px solid #444;background:#1e1e2e;padding:12px;
                font-family:monospace;font-size:13px;color:#cdd6f4;">
    {content}
    </div>
    """


def setup_aria_logging(log_queue: queue.Queue) -> None:
    root = logging.getLogger()
    handler = AgentQueueHandler(log_queue)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in AGENT_COLORS:
        logging.getLogger(name).setLevel(logging.INFO)


def table_for(records: list) -> list:
    if not records:
        return []
    return [
        [r.asset, str(r.tech_score), r.sentiment, f"{r.final_score:.1f}", r.decision, r.skip_reason or ""]
        for r in records
    ]


def run_pipeline_with_logging(log_queue: queue.Queue, result_queue: queue.Queue) -> None:
    try:
        # Pass log_queue so Modal streaming logs are forwarded in real time
        records, _ = _run_pipeline(log_queue=log_queue)
        print(f"DEBUG: putting {len(records)} records into result_queue")
        result_queue.put(records)
    except Exception as e:
        print(f"DEBUG: exception in pipeline: {e}")
        logging.exception("Pipeline failed: %s", e)
        result_queue.put(e)


def run_clicked(log_state):
    log_queue: queue.Queue = queue.Queue()
    result_queue: queue.Queue = queue.Queue()

    root = logging.getLogger()
    for h in root.handlers[:]:
        if isinstance(h, AgentQueueHandler):
            root.removeHandler(h)
    setup_aria_logging(log_queue)

    log_lines: list = []
    thread = __import__("threading").Thread(
        target=run_pipeline_with_logging,
        args=(log_queue, result_queue),
        daemon=True,
    )
    thread.start()

    table: list = []
    while True:
        # Drain whatever log entries have arrived since last poll
        # (for Modal these trickle in as the remote generator yields them)
        while True:
            try:
                logger_name, msg = log_queue.get_nowait()
                log_lines.append(agent_line_to_html(logger_name, msg))
            except queue.Empty:
                break

        # Check whether the pipeline thread has put a result yet
        try:
            records = result_queue.get_nowait()
            if isinstance(records, Exception):
                table = [[str(records), "", "", "", "ERROR", ""]]
            else:
                table = table_for(records)
            break
        except queue.Empty:
            pass

        yield log_lines, html_for_log(log_lines), table
        time.sleep(0.08)  # ~12 fps poll — fast enough for smooth streaming

        if not thread.is_alive():
            # Final drain after thread exits
            while True:
                try:
                    logger_name, msg = log_queue.get_nowait()
                    log_lines.append(agent_line_to_html(logger_name, msg))
                except queue.Empty:
                    break
            try:
                records = result_queue.get_nowait()
            except queue.Empty:
                records = []
            if isinstance(records, Exception):
                table = [[str(records), "", "", "", "ERROR", ""]]
            else:
                table = table_for(records)
            break

    yield log_lines, html_for_log(log_lines), table


def build_ui():
    with gr.Blocks(title="ARIA — Market Intelligence", theme=gr.themes.Soft(), css="""
        .log-panel { font-family: ui-monospace, monospace; }
    """) as ui:
        gr.Markdown("# ARIA — Automated Real-time Investment Alert Agent")
        gr.Markdown("Multi-agent pipeline: Data → Tech Analysis → Sentiment → Decision → Pushover alerts.")
        log_state = gr.State([])
        with gr.Row():
            run_btn = gr.Button("Run pipeline", variant="primary")
        with gr.Row():
            log_html = gr.HTML(value=html_for_log([]), label="Agent log")
        with gr.Row():
            results_df = gr.Dataframe(
                headers=["Asset", "Tech score", "Sentiment", "Final score", "Decision", "Skip reason"],
                datatype=["str", "str", "str", "str", "str", "str"],
                label="Last run results",
            )
        run_btn.click(
            fn=run_clicked,
            inputs=[log_state],
            outputs=[log_state, log_html, results_df],
        )
    return ui


if __name__ == "__main__":
    ui = build_ui()
    ui.launch(share=False, inbrowser=True)