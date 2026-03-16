# ARIA — Automated Real-time Investment Alert Agent

Multi-agent pipeline that monitors commodities, stocks, indices, and crypto; runs technical and sentiment analysis; and sends Pushover alerts when opportunities meet the score threshold.

## Agents

1. **DATA_FETCHER** — Fetches prices and OHLCV from Alpha Vantage (stocks/indices), CommodityPriceAPI (gold/silver), CoinGecko (crypto).
2. **TECH_ANALYST** — RSI, MACD, Bollinger Bands, EMA cross; scores each asset and assigns a bias (STRONG BUY … STRONG SELL).
3. **SENTIMENT_AGENT** — NewsAPI headlines + LLM (via LiteLLM) for sentiment and a one-sentence narrative.
4. **DECISION_AGENT** — Combines tech and sentiment into a final score; applies alert threshold, cooldowns, and market-hours rules.
5. **NOTIFIER** — Sends formatted Pushover push notifications for assets that qualify.

## Setup

```bash
cd week8/community_contributions/chrys
uv venv
source .venv/bin/activate   # on Windows: .venv\Scripts\activate
uv pip install -r requirements.txt
cp .env.example .env
# Edit .env with your API keys (Pushover, Alpha Vantage, CommodityPriceAPI, NewsAPI, OpenRouter).
# First time with Modal: modal setup   (or modal token new)
```

## Run

- **Gradio UI** (agent-colored log + results table):
  ```bash
  python app.py
  ```
- **Notebook** (same Gradio app from Jupyter/VS Code):
  ```bash
  cd week8/community_contributions/chrys
  jupyter notebook ARIA_Gradio_App.ipynb
  ```
  Or open `ARIA_Gradio_App.ipynb` in your editor and run all cells. The notebook sets the path and launches the Gradio UI; click **Run pipeline** to run all agents.
- **CLI one-off run:**
  ```bash
  python main.py run-once
  ```
- **Background scheduler** (30 min interval):
  ```bash
  python main.py schedule
  ```

## Deploy pipeline to Modal (optional)

Run the full pipeline on Modal so you only need the notebook + Gradio UI locally.

**Requires:** `modal` CLI. If you get `command not found: modal`, run `uv pip install -r requirements.txt` in your activated venv (see Setup), then run `modal` from that same shell.

1. **Create a Modal secret** with your API keys (in [Modal dashboard](https://modal.com) or CLI):
   ```bash
   modal secret create aria-env \
     ALPHA_VANTAGE_API_KEY=... \
     COMMODITY_PRICE_API_KEY=... \
     NEWSAPI_KEY=... \
     PUSHOVER_USER=... \
     PUSHOVER_TOKEN=... \
     OPENROUTER_API_KEY=...
   ```
2. **Deploy** from the `chrys` directory:
   ```bash
   cd week8/community_contributions/chrys && modal deploy aria_modal.py
   ```
3. **Use in the notebook or app:** set `USE_MODAL=1` (e.g. in a notebook cell: `os.environ["USE_MODAL"] = "1"`) before launching the Gradio app. "Run pipeline" will call the deployed function; results appear in the table and are logged locally to `data/aria_audit.db`. Agent log stream is shown when running locally; with Modal you still get the results table.

## Config

- Watchlist: default list in `config.py`; override with `ARIA_WATCHLIST` (comma-separated).
- Alert threshold: 65+ final score; max 3 alerts/hour; 2-hour cooldown per asset.
- Outside US market hours (9am–5pm ET), only 24/7 assets (crypto, gold, silver, oil) can trigger alerts.

## Audit log

Runs are logged to `data/aria_audit.db` (SQLite). Optional: purge rows older than 90 days (see `db.purge_older_than_days()`).

---

## Screenshot / Preview

![ARIA Gradio app preview](aria-preview.png)
