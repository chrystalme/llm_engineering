"""DECISION_AGENT: combine tech + sentiment, apply alert rules.

Market-hours gating:
  - Stocks / indices         → only alert during US equity hours (9am–5pm ET, weekdays)
  - Crypto / commodities     → alert 24/7 (ASSETS_24_7)
  - Forex                    → alert 24/5 weekdays any hour (ASSETS_24_5)
  - ASSETS_ALWAYS_ON         → union of the above two sets
"""
from datetime import datetime, timezone, timedelta
from typing import List, Dict

from agents.base import AgentBase
from models import TechResult, SentimentResult, DecisionRecord, AssetData

from config import (
    ALERT_SCORE_THRESHOLD,
    MAX_ALERTS_PER_HOUR,
    ASSET_COOLDOWN_HOURS,
    SCORE_BUMP_TO_OVERRIDE_COOLDOWN,
    ASSETS_ALWAYS_ON,
    MARKET_OPEN_HOUR_ET,
    MARKET_CLOSE_HOUR_ET,
)


def _now_et() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now(timezone.utc) - timedelta(hours=5)


def _is_market_hours_et() -> bool:
    now = _now_et()
    if now.weekday() >= 5:  # Sat=5, Sun=6
        return False
    return MARKET_OPEN_HOUR_ET <= now.hour < MARKET_CLOSE_HOUR_ET


def _is_weekday() -> bool:
    """True Mon–Fri; used to gate forex alerts (24/5 but not weekends)."""
    return _now_et().weekday() < 5


class DecisionAgent(AgentBase):
    name = "DECISION_AGENT"
    logger_name = "aria.decision_agent"

    def __init__(self):
        super().__init__()
        self._last_alert_per_asset: Dict[str, datetime] = {}
        self._last_score_per_asset: Dict[str, float] = {}
        self._alerts_this_hour: List[datetime] = []

    def _sentiment_points(self, sentiment: str) -> float:
        if sentiment == "POSITIVE":
            return 30.0
        if sentiment == "NEGATIVE":
            return -30.0
        return 0.0

    def _trim_hour_alerts(self):
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        self._alerts_this_hour = [t for t in self._alerts_this_hour if t > cutoff]

    def _allowed_outside_hours(self, asset: str) -> bool:
        """Return True if this asset may alert when US equity market is closed."""
        if asset in ASSETS_ALWAYS_ON:
            # Forex is 24/5 — block on weekends
            from config import ASSETS_24_5
            if asset in ASSETS_24_5:
                return _is_weekday()
            return True  # crypto / commodities: truly 24/7
        return False

    def run(
        self,
        tech_results: List[TechResult],
        sentiment_results: List[SentimentResult],
        asset_data: List[AssetData],
    ) -> List[DecisionRecord]:
        self.log("Computing final scores and alert decisions")
        sentiment_by_asset = {s.asset: s for s in sentiment_results}
        market_open = _is_market_hours_et()
        self._trim_hour_alerts()
        records: List[DecisionRecord] = []

        for tr in tech_results:
            sent = sentiment_by_asset.get(tr.asset)
            sent_points = self._sentiment_points(sent.sentiment) if sent else 0.0
            final_score = (tr.tech_score * 0.70) + sent_points

            decision = "SKIP"
            skip_reason = None
            priority = 0

            if final_score >= ALERT_SCORE_THRESHOLD:
                # Gate by market hours unless asset trades outside equity hours
                if not market_open and not self._allowed_outside_hours(tr.asset):
                    skip_reason = "Market closed; equity-only asset"
                else:
                    now = datetime.now(timezone.utc)
                    last = self._last_alert_per_asset.get(tr.asset)
                    if last and (now - last).total_seconds() < ASSET_COOLDOWN_HOURS * 3600:
                        prev_score = self._last_score_per_asset.get(tr.asset, 0)
                        if final_score < prev_score + SCORE_BUMP_TO_OVERRIDE_COOLDOWN:
                            skip_reason = "Cooldown"
                    if not skip_reason and len(self._alerts_this_hour) >= MAX_ALERTS_PER_HOUR:
                        skip_reason = "Max alerts per hour"
                    if not skip_reason:
                        decision = "ALERT"
                        priority = 2 if final_score >= 90 else (1 if final_score >= 75 else 0)

            records.append(DecisionRecord(
                asset=tr.asset,
                tech_score=tr.tech_score,
                sentiment=sent.sentiment if sent else "MIXED",
                final_score=round(final_score, 1),
                decision=decision,
                priority=priority,
                skip_reason=skip_reason,
            ))

            if decision == "ALERT":
                now = datetime.now(timezone.utc)
                self._last_alert_per_asset[tr.asset] = now
                self._last_score_per_asset[tr.asset] = final_score
                self._alerts_this_hour.append(now)

            self.log(
                f"{tr.asset}: final={final_score:.1f} -> {decision}"
                + (f" ({skip_reason})" if skip_reason else "")
            )

        return records