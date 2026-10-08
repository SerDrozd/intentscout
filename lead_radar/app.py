from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from time import perf_counter
from dataclasses import dataclass
from datetime import datetime

from .collectors import (
    AirtableWebCollector,
    BlueskyCollector,
    BubbleCommunityCollector,
    GitHubCollector,
    HackerNewsAlgoliaCollector,
    HackerNewsCollector,
    FlutterFlowWebCollector,
    HubSpotWebCollector,
    MakeCommunityCollector,
    N8nCommunityCollector,
    RSSCollector,
    RetoolCommunityCollector,
    ShopifyCommunityCollector,
    XanoWebCollector,
    ZapierWebCollector,
)
from .adaptive import AdaptiveScheduler
from .commercial import assess_commercial, has_direct_buyer_evidence
from .config import Settings, load_keyword_config
from .database import Database
from .llm import CLASSIFIER_SCHEMA_VERSION, LLMAPIError, LLMBudgetExceeded, OpenAILeadClassifier
from .matcher import Matcher
from .models import AlertClass, AuthorType, LeadBrief, LLMClassification, LLMDecision, Opportunity, ScoredOpportunity
from .scoring import score
from .collectors.bluesky import QUERY_FAMILIES as BLUESKY_QUERY_FAMILIES
from .collectors.github import BROAD_QUERIES as GITHUB_BROAD_QUERIES
from .collectors.hn_algolia import QUERY_FAMILIES as HN_QUERY_FAMILIES
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
    llm_classified: int = 0
    llm_rejected: int = 0
    llm_held: int = 0
    commercial_candidates: int = 0
    review_candidates: int = 0


@dataclass(slots=True)
class ManualIngestResult:
    status: str
    opportunity_id: int | None = None
    scored: ScoredOpportunity | None = None


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
        self._cycle_index = 0
        self.adaptive = AdaptiveScheduler(
            enabled=settings.adaptive_scheduling_enabled,
            window_days=settings.adaptive_window_days,
            refresh_cycles=settings.adaptive_refresh_cycles,
        )
        self.llm = OpenAILeadClassifier(
            api_key=settings.openai_api_key if settings.llm_enabled else None,
            model=settings.llm_model,
            max_post_chars=settings.max_post_chars,
            max_output_tokens=settings.llm_max_output_tokens,
            input_price_per_mtok=settings.llm_input_price_per_mtok,
            cached_input_price_per_mtok=settings.llm_cached_input_price_per_mtok,
            cache_write_price_per_mtok=settings.llm_cache_write_price_per_mtok,
            output_price_per_mtok=settings.llm_output_price_per_mtok,
        )

    def _refresh_adaptive(self, *, force: bool = False, days: int | None = None) -> None:
        if not self.settings.adaptive_scheduling_enabled:
            return
        if not force and not self.adaptive.needs_refresh(self._cycle_index):
            return
        window_days = days or self.settings.adaptive_window_days
        source_rows = self.db.roi_report(days=window_days, by_query=False)
        query_rows = self.db.roi_report(days=window_days, by_query=True)
        self.adaptive.refresh(source_rows, query_rows, cycle_index=self._cycle_index)

    @staticmethod
    def _adaptive_source_name(collector_name: str) -> str:
        if collector_name in {"hn_algolia", "hackernews"}:
            return "hackernews"
        return collector_name

    def _effective_poll_interval(self, source: str) -> int:
        plan_interval = self.adaptive.source_plan(source).interval_cycles
        if source.casefold() in self.settings.protected_buyer_sources:
            return min(plan_interval, self.settings.protected_buyer_max_poll_cycles)
        return plan_interval

    def _should_run_source(self, source: str) -> bool:
        if not self.settings.adaptive_scheduling_enabled:
            return True
        interval = self._effective_poll_interval(source)
        return self._cycle_index % interval == 0

    def adaptive_schedule_rows(self, *, days: int | None = None) -> list[dict[str, object]]:
        self._refresh_adaptive(force=True, days=days)
        query_specs = {
            "hackernews": (self.settings.hn_algolia_queries_per_cycle, len(HN_QUERY_FAMILIES)),
            "bluesky": (self.settings.bluesky_queries_per_cycle, len(BLUESKY_QUERY_FAMILIES)),
            "github": (len(GITHUB_BROAD_QUERIES), len(GITHUB_BROAD_QUERIES)),
        }
        sources = (
            "hackernews", "bluesky", "rss", "github", "make", "n8n",
            "shopify", "bubble", "airtable", "zapier", "retool", "xano", "flutterflow",
        )
        rows: list[dict[str, object]] = []
        for source in sources:
            plan = self.adaptive.source_plan(source)
            base_limit, available = query_specs.get(source, (0, 0))
            query_limit = self.adaptive.query_limit(source, base_limit, available) if available else None
            protected = source.casefold() in self.settings.protected_buyer_sources
            rows.append({
                "source": source,
                "recommendation": plan.recommendation.value,
                "confidence": plan.confidence.value,
                "interval_cycles": self._effective_poll_interval(source),
                "adaptive_interval_cycles": plan.interval_cycles,
                "protected": protected,
                "query_limit": query_limit,
                "query_available": available or None,
                "reason": plan.reason,
            })
        return rows

    def collectors(self):
        hn_query_keys: tuple[str, ...] | None = None
        bluesky_query_keys: tuple[str, ...] | None = None
        github_queries: tuple[str, ...] | None = None
        if self.settings.adaptive_scheduling_enabled:
            hn_query_keys = self.adaptive.select_query_keys(
                "hackernews",
                tuple(item.key for item in HN_QUERY_FAMILIES),
                base_limit=self.settings.hn_algolia_queries_per_cycle,
                cycle_index=self._cycle_index,
            )
            bluesky_query_keys = self.adaptive.select_query_keys(
                "bluesky",
                tuple(item.key for item in BLUESKY_QUERY_FAMILIES),
                base_limit=self.settings.bluesky_queries_per_cycle,
                cycle_index=self._cycle_index,
            )
            github_queries = self.adaptive.select_query_keys(
                "github",
                tuple(GITHUB_BROAD_QUERIES),
                base_limit=len(GITHUB_BROAD_QUERIES),
                cycle_index=self._cycle_index,
            )
        if self.settings.hn_algolia_enabled:
            yield HackerNewsAlgoliaCollector(
                self.db,
                lookback_hours=self.settings.hn_algolia_lookback_hours,
                results_per_query=self.settings.hn_algolia_results_per_query,
                queries_per_cycle=self.settings.hn_algolia_queries_per_cycle,
                query_keys=hn_query_keys,
            )
        if self.settings.hn_enabled:
            yield HackerNewsCollector(
                self.db,
                bootstrap_items=self.settings.hn_bootstrap_items,
                max_items_per_cycle=self.settings.hn_max_items_per_cycle,
                concurrency=self.settings.hn_concurrency,
            )
        if self.settings.bluesky_enabled:
            yield BlueskyCollector(
                self.db,
                lookback_hours=self.settings.bluesky_lookback_hours,
                results_per_query=self.settings.bluesky_results_per_query,
                queries_per_cycle=self.settings.bluesky_queries_per_cycle,
                query_keys=bluesky_query_keys,
            )
        if self.settings.rss_enabled:
            yield RSSCollector(
                self.db,
                config_path=self.settings.rss_feeds_path,
                extra_config_paths=(self.settings.rss_local_feeds_path,),
                concurrency=self.settings.rss_concurrency,
            )
        if self.settings.github_enabled:
            yield GitHubCollector(
                self.settings.github_token,
                lookback_hours=self.settings.github_lookback_hours,
                results_per_query=self.settings.github_results_per_query,
                queries=github_queries,
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
        if self.settings.hubspot_enabled:
            yield HubSpotWebCollector(
                self.db,
                lookback_hours=self.settings.hubspot_lookback_hours,
                max_topics_per_section=self.settings.hubspot_topics_per_category,
                concurrency=self.settings.web_fetch_concurrency,
            )
        if self.settings.shopify_enabled:
            yield ShopifyCommunityCollector(
                self.db,
                lookback_hours=self.settings.shopify_lookback_hours,
                max_topics_per_category=self.settings.shopify_topics_per_category,
                concurrency=self.settings.discourse_concurrency,
            )
        if self.settings.bubble_enabled:
            yield BubbleCommunityCollector(
                self.db,
                lookback_hours=self.settings.bubble_lookback_hours,
                max_topics_per_category=self.settings.bubble_topics_per_category,
                concurrency=self.settings.discourse_concurrency,
            )
        if self.settings.airtable_enabled:
            yield AirtableWebCollector(
                self.db,
                lookback_hours=self.settings.airtable_lookback_hours,
                max_topics_per_section=self.settings.airtable_topics_per_section,
                concurrency=self.settings.web_fetch_concurrency,
            )
        if self.settings.zapier_enabled:
            yield ZapierWebCollector(
                self.db,
                lookback_hours=self.settings.zapier_lookback_hours,
                max_topics_per_section=self.settings.zapier_topics_per_section,
                concurrency=self.settings.web_fetch_concurrency,
            )
        if self.settings.retool_enabled:
            yield RetoolCommunityCollector(
                self.db,
                lookback_hours=self.settings.retool_lookback_hours,
                max_topics_per_category=self.settings.retool_topics_per_category,
                concurrency=self.settings.discourse_concurrency,
            )
        if self.settings.xano_enabled:
            yield XanoWebCollector(
                self.db,
                lookback_hours=self.settings.xano_lookback_hours,
                max_topics_per_section=self.settings.xano_topics_per_section,
                concurrency=self.settings.web_fetch_concurrency,
            )
        if self.settings.flutterflow_enabled:
            yield FlutterFlowWebCollector(
                self.db,
                lookback_hours=self.settings.flutterflow_lookback_hours,
                max_topics_per_section=self.settings.flutterflow_topics_per_section,
                concurrency=self.settings.web_fetch_concurrency,
            )

    def ingest_manual(self, opportunity: Opportunity) -> ManualIngestResult:
        if self.db.exists(opportunity.source, opportunity.external_id):
            return ManualIngestResult(status="duplicate")
        analysis = self.matcher.analyze(opportunity)
        if analysis.excluded:
            return ManualIngestResult(status="excluded")
        direct_buyer = has_direct_buyer_evidence(opportunity, analysis)
        if not analysis.phrase_matches and not direct_buyer:
            return ManualIngestResult(status="no_match")
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
            return ManualIngestResult(status="duplicate")
        return ManualIngestResult(status="stored", opportunity_id=opportunity_id, scored=scored)

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

    @staticmethod
    def _row_to_opportunity(row: sqlite3.Row) -> Opportunity:
        metadata_raw = row["metadata_json"]
        try:
            metadata = json.loads(str(metadata_raw)) if metadata_raw else {}
        except json.JSONDecodeError:
            metadata = {}
        created_at = datetime.fromisoformat(str(row["created_at"]))
        return Opportunity(
            source=str(row["source"]),
            external_id=str(row["external_id"]),
            url=str(row["url"]),
            title=str(row["title"]),
            body=str(row["body"]),
            author=None if row["author"] is None else str(row["author"]),
            created_at=created_at,
            metadata=metadata if isinstance(metadata, dict) else {},
        )

    def _budget_capacity(self, *, purpose: str) -> tuple[int, float, float]:
        usage = self.db.llm_usage_summary()
        hour_cap = self.settings.max_llm_candidates_per_hour
        day_cap = self.settings.max_llm_candidates_per_day

        # Background shortlist enrichment must never consume the final slots that
        # a fresh Telegram-bound candidate may need later in the hour/day. Live
        # alert gating, manual classification and brief generation may use the
        # full configured limits.
        if purpose == "auto_shortlist":
            hour_cap = max(0, hour_cap - self.settings.llm_alert_reserve_per_hour)
            day_cap = max(0, day_cap - self.settings.llm_alert_reserve_per_day)

        hour_remaining = max(0, hour_cap - int(usage["classified_candidates_hour"]))
        day_remaining = max(0, day_cap - int(usage["classified_candidates_today"]))
        candidate_remaining = min(hour_remaining, day_remaining)
        daily_remaining = max(
            0.0,
            self.settings.daily_llm_budget_usd - float(usage["estimated_cost_today"]),
        )
        monthly_remaining = max(
            0.0,
            self.settings.monthly_llm_budget_usd - float(usage["estimated_cost_month"]),
        )
        return candidate_remaining, daily_remaining, monthly_remaining

    async def _classify_opportunities(
        self,
        opportunities: list[tuple[int, Opportunity]],
        *,
        purpose: str,
    ) -> list[LLMClassification]:
        if not opportunities:
            return []
        if not self.settings.llm_enabled or not self.llm.enabled:
            return []

        results: list[LLMClassification] = []
        offset = 0
        while offset < len(opportunities):
            remaining, daily_usd, monthly_usd = self._budget_capacity(purpose=purpose)
            if remaining <= 0:
                if results:
                    break
                raise LLMBudgetExceeded("LLM candidate budget reached")
            batch_size = min(self.settings.llm_batch_size, remaining, len(opportunities) - offset)
            batch = opportunities[offset : offset + batch_size]
            estimated = self.llm.estimate_request_cost(batch)
            if estimated > daily_usd or estimated > monthly_usd:
                if results:
                    break
                raise LLMBudgetExceeded("LLM dollar budget reached")
            classifications, usage = await self.llm.classify(batch)
            self.db.record_llm_usage(
                purpose=purpose,
                model=self.settings.llm_model,
                response_id=usage.response_id,
                candidate_count=len(batch),
                input_tokens=usage.input_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                output_tokens=usage.output_tokens,
                estimated_cost_usd=usage.estimated_cost_usd,
                opportunity_ids=[opportunity_id for opportunity_id, _ in batch],
            )
            for classification in classifications:
                self.db.save_llm_classification(
                    classification,
                    model=self.settings.llm_model,
                    schema_version=CLASSIFIER_SCHEMA_VERSION,
                )
            results.extend(classifications)
            offset += batch_size
        return results

    async def classify_shortlist(
        self,
        limit: int = 20,
        sources: tuple[str, ...] | None = None,
        *,
        force: bool = False,
    ) -> list[LLMClassification]:
        rows = self.db.llm_candidate_rows(limit, sources, force=force)
        opportunities = [(int(row["id"]), self._row_to_opportunity(row)) for row in rows]
        return await self._classify_opportunities(opportunities, purpose="shortlist")

    @staticmethod
    def _brief_context_from_row(row: sqlite3.Row) -> dict[str, object]:
        return {
            "author_type": str(row["author_type"]),
            "commercial_intent": str(row["commercial_intent"]),
            "opportunity_class": str(row["opportunity_class"]),
            "alert_class": str(row["alert_class"]),
            "cash_score": int(row["cash_score"]),
            "value_score": int(row["value_score"]),
            "relationship_score": int(row["relationship_score"]),
            "freshness_score": int(row["freshness_score"]),
            "competition_score": int(row["competition_score"]),
            "llm_decision": str(row["llm_decision"]),
            "llm_confidence": row["llm_confidence"],
        }

    async def _generate_lead_brief(
        self,
        opportunity_id: int,
        opportunity: Opportunity,
        context: dict[str, object],
    ) -> LeadBrief | None:
        existing = self.db.get_lead_brief(opportunity_id)
        if existing is not None:
            return existing
        if not self.settings.llm_enabled or not self.llm.enabled:
            return None

        remaining, daily_usd, monthly_usd = self._budget_capacity(purpose="lead_brief")
        if remaining <= 0:
            raise LLMBudgetExceeded("LLM candidate budget reached")
        estimated = self.llm.estimate_brief_cost(opportunity_id, opportunity, context)
        if estimated > daily_usd or estimated > monthly_usd:
            raise LLMBudgetExceeded("LLM dollar budget reached")

        brief, usage = await self.llm.generate_brief(opportunity_id, opportunity, context)
        self.db.record_llm_usage(
            purpose="lead_brief",
            model=self.settings.llm_model,
            response_id=usage.response_id,
            candidate_count=1,
            input_tokens=usage.input_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            output_tokens=usage.output_tokens,
            estimated_cost_usd=usage.estimated_cost_usd,
            opportunity_ids=[opportunity_id],
        )
        self.db.save_lead_brief(brief, model=self.settings.llm_model)
        return brief

    async def prepare_lead(self, opportunity_id: int) -> LeadBrief | None:
        row = self.db.opportunity_row(opportunity_id)
        if row is None:
            return None
        if str(row["llm_decision"]) == LLMDecision.REJECT.value:
            return None
        opportunity = self._row_to_opportunity(row)
        return await self._generate_lead_brief(
            opportunity_id,
            opportunity,
            self._brief_context_from_row(row),
        )

    async def _brief_for_scored(self, opportunity_id: int, scored: ScoredOpportunity) -> LeadBrief | None:
        existing = self.db.get_lead_brief(opportunity_id)
        if existing is not None:
            return existing
        assessment = scored.assessment
        if assessment is None:
            return None
        context: dict[str, object] = {
            "author_type": assessment.author_type.value,
            "commercial_intent": assessment.commercial_intent.value,
            "opportunity_class": assessment.opportunity_class.value,
            "alert_class": assessment.alert_class.value,
            "cash_score": assessment.cash_score,
            "value_score": assessment.value_score,
            "relationship_score": assessment.relationship_score,
            "freshness_score": assessment.freshness_score,
            "competition_score": assessment.competition_score,
            "llm_decision": self.db.llm_decisions([opportunity_id]).get(opportunity_id, "NOT_RUN"),
        }
        return await self._generate_lead_brief(opportunity_id, scored.opportunity, context)

    def _review_alert_eligible(self, scored: ScoredOpportunity) -> bool:
        assessment = scored.assessment
        if assessment is None or assessment.alert_class != AlertClass.P1:
            return False
        if not self.settings.review_alerts_enabled:
            return False
        if assessment.priority_score < self.settings.review_alert_min_priority:
            return False
        if assessment.freshness_score < self.settings.review_alert_min_freshness:
            return False
        if assessment.author_type not in {
            AuthorType.BUYER,
            AuthorType.POTENTIAL_BUYER,
            AuthorType.AGENCY_BUYER,
            AuthorType.TECHNICAL_BUYER,
        }:
            return False
        return has_direct_buyer_evidence(scored.opportunity, scored.analysis)

    async def _auto_classify_review_candidates(self, stats: CycleStats) -> None:
        if not self.settings.llm_enabled or not self.llm.enabled:
            return
        rows = self.db.llm_candidate_rows(self.settings.max_llm_candidates_per_hour)
        if not rows:
            return
        opportunities = [(int(row["id"]), self._row_to_opportunity(row)) for row in rows]
        try:
            classifications = await self._classify_opportunities(opportunities, purpose="auto_shortlist")
            stats.llm_classified += len(classifications)
        except LLMBudgetExceeded as exc:
            log.info("Automatic LLM shortlist classification skipped: %s", exc)
        except LLMAPIError as exc:
            log.warning("Automatic LLM shortlist classification failed safely: %s", exc)

    async def _llm_filter_pending(
        self,
        pending_alerts: list[tuple[int, int, ScoredOpportunity]],
        stats: CycleStats,
    ) -> list[tuple[int, int, ScoredOpportunity]]:
        if not pending_alerts:
            return []

        decisions = self.db.llm_decisions([item[1] for item in pending_alerts])
        llm_available = self.settings.llm_enabled and self.llm.enabled
        to_classify: list[tuple[int, Opportunity]] = []
        if llm_available:
            for _, opportunity_id, scored in pending_alerts:
                if decisions.get(opportunity_id, "NOT_RUN") == LLMDecision.NOT_RUN.value:
                    to_classify.append((opportunity_id, scored.opportunity))
        if to_classify:
            try:
                classifications = await self._classify_opportunities(to_classify, purpose="live_alert_gate")
                stats.llm_classified += len(classifications)
                decisions.update({item.opportunity_id: item.decision.value for item in classifications})
            except LLMBudgetExceeded as exc:
                log.info("LLM gate skipped: %s", exc)
            except LLMAPIError as exc:
                log.warning("LLM gate failed safely: %s", exc)

        kept: list[tuple[int, int, ScoredOpportunity]] = []
        for item in pending_alerts:
            _, opportunity_id, scored = item
            decision = decisions.get(opportunity_id, LLMDecision.NOT_RUN.value)
            if decision == LLMDecision.REJECT.value:
                stats.llm_rejected += 1
                continue
            if decision in {LLMDecision.PASS.value, LLMDecision.REVIEW.value}:
                kept.append(item)
                continue

            # No LLM decision: fail open only for a deterministic P0 with direct,
            # buyer-authored evidence. P1 review alerts and inferred/implicit P0s
            # wait for quota instead of leaking seller/noise posts to Telegram.
            assessment = scored.assessment
            direct_buyer = has_direct_buyer_evidence(scored.opportunity, scored.analysis)
            if assessment is not None and assessment.p0 and direct_buyer:
                kept.append(item)
            else:
                stats.llm_held += 1
                log.info(
                    "Holding alert candidate %s without LLM decision: class=%s direct_buyer=%s",
                    opportunity_id,
                    assessment.alert_class.value if assessment is not None else "UNKNOWN",
                    direct_buyer,
                )
        return kept

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

        if assessment.p0 or assessment.alert_class == AlertClass.P1:
            stats.commercial_candidates += 1

        review_alert = self._review_alert_eligible(scored)
        if review_alert:
            stats.review_candidates += 1
        should_alert = assessment.p0 or review_alert or (
            self.settings.alert_review_queue and assessment.alert_class == AlertClass.P1
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
            brief: LeadBrief | None = None
            try:
                brief = await self._brief_for_scored(opportunity_id, scored)
            except LLMBudgetExceeded as exc:
                log.info("Lead brief generation skipped for %s: %s", opportunity_id, exc)
            except LLMAPIError as exc:
                log.warning("Lead brief generation failed safely for %s: %s", opportunity_id, exc)
            try:
                if await self.telegram.send_alert(opportunity_id, scored, brief):
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

        self._refresh_adaptive()
        for collector in self.collectors():
            source = str(getattr(collector, "name", type(collector).__name__))
            adaptive_source = self._adaptive_source_name(source)
            if not self._should_run_source(adaptive_source):
                plan = self.adaptive.source_plan(adaptive_source)
                log.info(
                    "%s adaptive-skip cycle=%s recommendation=%s poll_every=%s",
                    source,
                    self._cycle_index,
                    plan.recommendation.value,
                    self._effective_poll_interval(adaptive_source),
                )
                continue
            started = perf_counter()
            matched_before = stats.matched
            inserted_before = stats.inserted
            excluded_before = stats.excluded
            fetched = 0
            error: str | None = None
            try:
                items = await collector.collect()
                fetched = len(items)
                stats.fetched += fetched
                log.info("%s fetched %s item(s)", source, fetched)
                for item in items:
                    await self.process(item, stats, pending_alerts)
            except Exception as exc:
                error = type(exc).__name__
                log.exception("Collector %s failed", source)
            finally:
                self.db.record_collector_run(
                    source=source,
                    fetched=fetched,
                    matched=stats.matched - matched_before,
                    inserted=stats.inserted - inserted_before,
                    excluded=stats.excluded - excluded_before,
                    duration_ms=int((perf_counter() - started) * 1000),
                    error=error,
                )

        # Fresh alert candidates get first claim on LLM quota and brief budget.
        # Background shortlist enrichment runs only after Telegram-bound work is
        # finished, so a noisy source cannot starve a later live opportunity.
        pending_alerts = await self._llm_filter_pending(pending_alerts, stats)
        await self._send_pending_alerts(pending_alerts, stats)
        await self._auto_classify_review_candidates(stats)
        self._cycle_index += 1
        return stats

    async def run_forever(self) -> None:
        while True:
            stats = await self.run_cycle()
            log.info(
                "Cycle done: fetched=%s matched=%s inserted=%s commercial=%s review_candidates=%s intent_filtered=%s alert_candidates=%s alerted=%s suppressed=%s excluded=%s llm_classified=%s llm_rejected=%s llm_held=%s",
                stats.fetched,
                stats.matched,
                stats.inserted,
                stats.commercial_candidates,
                stats.review_candidates,
                stats.intent_filtered,
                stats.alert_candidates,
                stats.alerted,
                stats.alert_suppressed,
                stats.excluded,
                stats.llm_classified,
                stats.llm_rejected,
                stats.llm_held,
            )
            await asyncio.sleep(self.settings.poll_interval_seconds)
