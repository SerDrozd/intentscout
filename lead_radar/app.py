from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime

from .collectors import GitHubCollector, HackerNewsCollector, MakeCommunityCollector, N8nCommunityCollector
from .commercial import assess_commercial
from .config import Settings, load_keyword_config
from .database import Database
from .matcher import Matcher
from .models import Opportunity, ScoredOpportunity
from .scoring import score
from .telegram import TelegramAPIError, TelegramClient


log = logging.getLogger("lead_radar")


@dataclass(slots=True)
class CycleStats:
    fetched: int = 0
    matched: int = 0
    inserted: int = 0
    alerted: int = 0
    alert_candidates: int = 0
    alert_suppressed: int = 0
    excluded: int = 0
    intent_filtered: int = 0


class RadarApp:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Database(settings.db_path)
        self.keyword_config = load_keyword_config(settings.keywords_path)
        self.matcher = Matcher(self.keyword_config)
        coverage = self.matcher.coverage_stats()
        log.info(
            "Matcher coverage: exact=%s technical_aliases=%s intent_aliases=%s pair_variant_equivalents=%s",
            coverage["exact_phrases"],
            coverage["technical_aliases"],
            coverage["intent_aliases"],
            coverage["pair_variant_equivalents"],
        )
        self.telegram = TelegramClient(settings.telegram_bot_token, settings.telegram_chat_id, self.db)

    def collectors(self):
        if self.settings.hn_enabled:
            yield HackerNewsCollector(
                self.db,
                bootstrap_items=self.settings.hn_bootstrap_items,
                max_items_per_cycle=self.settings.hn_max_items_per_cycle,
                concurrency=self.settings.hn_concurrency,
            )
        if self.settings.github_enabled:
            yield GitHubCollector(
                self.settings.github_token,
                lookback_hours=self.settings.github_lookback_hours,
                results_per_query=self.settings.github_results_per_query,
            )
        if self.settings.make_enabled:
            yield MakeCommunityCollector(
                self.db,
                lookback_hours=self.settings.make_lookback_hours,
                max_topics_per_category=self.settings.make_topics_per_category,
                concurrency=self.settings.discourse_concurrency,
            )
        if self.settings.n8n_enabled:
            yield N8nCommunityCollector(
                self.db,
                lookback_hours=self.settings.n8n_lookback_hours,
                max_topics_per_category=self.settings.n8n_topics_per_category,
                concurrency=self.settings.discourse_concurrency,
            )


    def reclassify_stored(self, sources: tuple[str, ...] | None = None) -> dict[str, int]:
        rows = self.db.reclassification_rows(sources)
        counts = {"processed": 0, "p0": 0, "p1": 0, "reject": 0}
        for row in rows:
            metadata_raw = row["metadata_json"]
            try:
                metadata = json.loads(str(metadata_raw)) if metadata_raw else {}
            except json.JSONDecodeError:
                metadata = {}
            created_at = datetime.fromisoformat(str(row["created_at"]))
            opportunity = Opportunity(
                source=str(row["source"]),
                external_id=str(row["external_id"]),
                url=str(row["url"]),
                title=str(row["title"]),
                body=str(row["body"]),
                author=None if row["author"] is None else str(row["author"]),
                created_at=created_at,
                metadata=metadata if isinstance(metadata, dict) else {},
            )
            analysis = self.matcher.analyze(opportunity)
            assessment = assess_commercial(
                opportunity,
                analysis,
                cash_threshold=self.settings.cash_alert_threshold,
                value_threshold=self.settings.value_alert_threshold,
                relationship_threshold=self.settings.relationship_alert_threshold,
            )
            self.db.update_commercial_assessment(int(row["id"]), assessment)
            counts["processed"] += 1
            if assessment.p0:
                counts["p0"] += 1
            elif assessment.alert_class.value == "P1":
                counts["p1"] += 1
            elif assessment.alert_class.value == "REJECT":
                counts["reject"] += 1
        return counts

    async def process(
        self,
        opportunity: Opportunity,
        stats: CycleStats,
        pending_alerts: list[tuple[int, int, ScoredOpportunity]],
    ) -> None:
        if self.db.exists(opportunity.source, opportunity.external_id):
            return
        analysis = self.matcher.analyze(opportunity)
        if analysis.excluded:
            stats.excluded += 1
            return
        if not analysis.phrase_matches:
            return
        stats.matched += 1

        total, breakdown = score(opportunity, analysis)
        assessment = assess_commercial(
            opportunity,
            analysis,
            cash_threshold=self.settings.cash_alert_threshold,
            value_threshold=self.settings.value_alert_threshold,
            relationship_threshold=self.settings.relationship_alert_threshold,
        )
        scored = ScoredOpportunity(
            opportunity=opportunity,
            analysis=analysis,
            score=total,
            breakdown=breakdown,
            assessment=assessment,
        )
        opportunity_id = self.db.insert(scored)
        if opportunity_id is None:
            return
        stats.inserted += 1

        should_alert = assessment.p0 or (
            self.settings.alert_review_queue and assessment.alert_class.value == "P1"
        )
        if not should_alert:
            if total >= self.settings.alert_threshold:
                stats.intent_filtered += 1
                log.debug(
                    "Commercial scoring suppressed source=%s legacy_score=%s cash=%s value=%s relationship=%s author=%s intent=%s class=%s title=%r",
                    opportunity.source,
                    total,
                    assessment.cash_score,
                    assessment.value_score,
                    assessment.relationship_score,
                    assessment.author_type.value,
                    assessment.commercial_intent.value,
                    assessment.alert_class.value,
                    opportunity.title[:100],
                )
            return

        stats.alert_candidates += 1
        if self.telegram.enabled:
            pending_alerts.append((assessment.priority_score, opportunity_id, scored))
        else:
            log.warning(
                "ALERT class=%s cash=%s value=%s relationship=%s source=%s title=%r url=%s",
                assessment.alert_class.value,
                assessment.cash_score,
                assessment.value_score,
                assessment.relationship_score,
                opportunity.source,
                opportunity.title[:100],
                opportunity.url,
            )

    async def _send_pending_alerts(
        self,
        pending_alerts: list[tuple[int, int, ScoredOpportunity]],
        stats: CycleStats,
    ) -> None:
        if not pending_alerts or not self.telegram.enabled:
            return

        # Highest scores first. Fresh/newer items win score ties.
        pending_alerts.sort(
            key=lambda x: (x[0], x[2].opportunity.created_at),
            reverse=True,
        )
        selected = pending_alerts[: self.settings.max_alerts_per_cycle]
        stats.alert_suppressed = max(0, len(pending_alerts) - len(selected))

        for i, (_, opportunity_id, scored) in enumerate(selected):
            if i:
                await asyncio.sleep(self.settings.telegram_min_send_interval_seconds)
            try:
                if await self.telegram.send_alert(opportunity_id, scored):
                    stats.alerted += 1
            except TelegramAPIError as exc:
                # TelegramAPIError deliberately contains no bot token / URL.
                log.warning("Telegram send failed for opportunity %s: %s", opportunity_id, exc)

        if stats.alert_suppressed:
            log.warning(
                "Suppressed %s additional alert candidate(s) this cycle; they remain stored in SQLite",
                stats.alert_suppressed,
            )

    async def run_cycle(self) -> CycleStats:
        stats = CycleStats()
        pending_alerts: list[tuple[int, int, ScoredOpportunity]] = []
        try:
            handled = await self.telegram.poll_actions()
            if handled:
                log.info("Processed %s Telegram action(s)", handled)
        except TelegramAPIError as exc:
            log.warning("Telegram callback polling failed: %s", exc)

        for collector in self.collectors():
            try:
                items = await collector.collect()
                stats.fetched += len(items)
                log.info("%s fetched %s item(s)", collector.name, len(items))
                for item in items:
                    await self.process(item, stats, pending_alerts)
            except Exception:
                log.exception("Collector %s failed", getattr(collector, "name", type(collector).__name__))

        await self._send_pending_alerts(pending_alerts, stats)
        return stats

    async def run_forever(self) -> None:
        while True:
            stats = await self.run_cycle()
            log.info(
                "Cycle done: fetched=%s matched=%s inserted=%s intent_filtered=%s alert_candidates=%s alerted=%s suppressed=%s excluded=%s",
                stats.fetched,
                stats.matched,
                stats.inserted,
                stats.intent_filtered,
                stats.alert_candidates,
                stats.alerted,
                stats.alert_suppressed,
                stats.excluded,
            )
            await asyncio.sleep(self.settings.poll_interval_seconds)
