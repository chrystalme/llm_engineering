"""
ARIA pipeline on Modal — deploy with: modal deploy aria_modal.py

From the chrys directory:
    modal deploy aria_modal.py

Required in "aria-env": NEWSAPI_KEY, PUSHOVER_USER, PUSHOVER_TOKEN, OPENROUTER_API_KEY.
Optional : ALPHA_VANTAGE_API_KEY, COMMODITY_PRICE_API_KEY, ARIA_SENTIMENT_MODEL,
           ARIA_WATCHLIST

Data sources (all free, no API key, cloud-safe):
  Stocks / indices / commodities : Stooq via pandas_datareader
  Forex                          : open.er-api.com
  Crypto                         : CoinGecko
  Metals spot price              : CommodityPriceAPI (optional, enriched with Stooq OHLCV)

Streaming: run_pipeline_remote yields JSON strings:
  {"type": "log",    "name": "<logger>", "msg": "<formatted message>"}
  {"type": "result", "records": [...], "asset_data": [...], "summary": {...}}
  {"type": "error",  "msg": "<error message>"}
"""
import json
import logging
import os
import queue
import sys
import threading
from typing import Iterator

import modal

CHRYS_DIR = os.path.dirname(os.path.abspath(__file__))

app = modal.App("aria-pipeline")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("tzdata")
    .pip_install(
        "requests",
        "pydantic>=2.0",
        "python-dotenv",
        "pandas",
        "numpy",
        "litellm",
        "yfinance",              # kept for CoinGecko fallback path
        "pandas_datareader",     # Stooq adapter — cloud-safe stock/index/commodity OHLCV
        "multitasking",
        "peewee",
    )
    .add_local_dir(CHRYS_DIR, remote_path="/root/aria")
)

_SENTINEL = object()


class _StreamHandler(logging.Handler):
    """Forward log records into a queue as JSON-serialisable dicts."""
    def __init__(self, log_q: queue.Queue):
        super().__init__()
        self.log_q = log_q

    def emit(self, record: logging.LogRecord):
        try:
            self.log_q.put({
                "type": "log",
                "name": record.name,
                "msg": self.format(record),
            })
        except Exception:
            self.handleError(record)


@app.function(
    image=image,
    secrets=[modal.Secret.from_name("aria-env")],
    timeout=300,
)
def run_pipeline_remote() -> Iterator[str]:
    """Stream ARIA pipeline — yields JSON strings as each agent runs."""
    sys.path.insert(0, "/root/aria")
    os.chdir("/root/aria")

    log_q: queue.Queue = queue.Queue()
    handler = _StreamHandler(log_q)
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
    )
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in (
        "aria.data_fetcher",
        "aria.tech_analyst",
        "aria.sentiment_agent",
        "aria.decision_agent",
        "aria.notifier",
    ):
        logging.getLogger(name).setLevel(logging.INFO)

    result_box: list = []
    error_box:  list = []

    def _run():
        try:
            from orchestrator import run_pipeline
            records, asset_data = run_pipeline(skip_db=True)
            result_box.append((records, asset_data))
        except Exception as exc:
            error_box.append(exc)
        finally:
            log_q.put(_SENTINEL)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    while True:
        item = log_q.get()
        if item is _SENTINEL:
            break
        yield json.dumps(item)

    root.removeHandler(handler)
    thread.join(timeout=5)

    if error_box:
        yield json.dumps({"type": "error", "msg": str(error_box[0])})
        return

    records, asset_data = result_box[0]
    n_fetched = len(asset_data)
    summary: dict = {"n_fetched": n_fetched, "n_records": len(records)}
    if n_fetched == 0:
        summary["hint"] = (
            "Data fetcher returned 0 assets. Check Modal logs for import errors — "
            "all sources (Stooq, open.er-api, CoinGecko) require no API keys."
        )

    yield json.dumps({
        "type": "result",
        "records":    [r.model_dump() for r in records],
        "asset_data": [a.model_dump() for a in asset_data],
        "summary":    summary,
    })


@app.function(image=image, timeout=10)
def wake() -> str:
    return "ok"


@app.local_entrypoint()
def main():
    """CLI test: cd chrys && modal run aria_modal.py"""
    print("Streaming run_pipeline_remote on Modal...")
    log_count = 0
    try:
        for raw in run_pipeline_remote.remote_gen():
            item = json.loads(raw)
            if item["type"] == "log":
                log_count += 1
                print(f"  [{item['name']}] {item['msg']}")
            elif item["type"] == "result":
                print(f"\nSummary: {item.get('summary', {})}")
                print(f"Records: {len(item.get('records', []))}")
                print(f"Log lines streamed: {log_count}")
            elif item["type"] == "error":
                print(f"ERROR: {item['msg']}")
    except Exception as e:
        print(f"ERROR calling remote_gen: {e}")
        import traceback
        traceback.print_exc()