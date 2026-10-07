from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timezone
from typing import Any, Iterable

import httpx

from .models import AuthorType, CommercialIntent, ContactStrategy, LeadBrief, LLMClassification, LLMDecision, Opportunity


CLASSIFIER_SCHEMA_VERSION = "lead-radar-v1"

BRIEF_SCHEMA_VERSION = "lead-radar-brief-v1"

BRIEF_SYSTEM_PROMPT = """You prepare one concise action brief for the developer, a Python backend developer focused on APIs, automation, business workflows and practical AI integrations.

This is for a private lead radar. Use only facts supplied in the candidate. Never invent clients, years of experience, production deployments, testimonials, Make/n8n specialist credentials, or technologies not supported by the supplied profile/context.

Return a practical brief that helps the developer decide and act in under 30 seconds:
- exact problem: concrete owned/business pain, not generic topic summary;
- why_fit: why the developer can realistically handle THIS technical slice;
- money_signal: explicit payment signal if present, otherwise clearly say payment is not explicit;
- suggested_scope: one bounded paid milestone, unless the opportunity is clearly larger, in which case make the first milestone a trust-reduction step without shrinking the whole project;
- suggested_price_band: realistic USD range. Typical anchors: webhook/debug $75-200, API integration $125-300, CSV/Excel automation $75-200, document extraction $100-300, AI/backend rescue $150-500, larger multi-day system $500-2000+;
- contact_strategy: PUBLIC_REPLY, DM, EMAIL, PUBLIC_REPLY_THEN_DM, APPLY_FORM, or IGNORE;
- draft: short natural English message, usually 60-140 words, tailored to concrete facts.

Outreach rules:
- Never use em dash or en dash characters. Use normal commas, periods, colons and ordinary hyphens only when needed.
- Do not write generic AI/sales phrases such as "I came across your post", "I am excited", "I am passionate", "I believe I would be a great fit", "With my extensive experience", "I'd love to leverage", "cutting-edge", "seamless", or "robust solutions".
- Do not list the full stack.
- For Make/n8n Questions, prefer a useful PUBLIC_REPLY and do not lead with a sales pitch unless the author explicitly asks to hire/pay.
- If the post is a normal recruiter/employment flow, service-provider post, learner request, OSS task, or otherwise not appropriate to contact, use IGNORE and leave draft empty.
- Be conservative and honest.
"""

BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "opportunity_id": {"type": "integer"},
        "problem_summary": {"type": "string"},
        "why_fit": {"type": "string"},
        "money_signal": {"type": "string"},
        "suggested_scope": {"type": "string"},
        "suggested_price_band": {"type": "string"},
        "contact_strategy": {
            "type": "string",
            "enum": ["PUBLIC_REPLY", "DM", "EMAIL", "PUBLIC_REPLY_THEN_DM", "APPLY_FORM", "IGNORE"],
        },
        "draft": {"type": "string"},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
    },
    "required": [
        "opportunity_id", "problem_summary", "why_fit", "money_signal",
        "suggested_scope", "suggested_price_band", "contact_strategy", "draft", "confidence"
    ],
    "additionalProperties": False,
}

FORBIDDEN_DRAFT_PHRASES = (
    "i came across your post",
    "i am excited",
    "i'm excited",
    "i am passionate",
    "i believe i would be a great fit",
    "with my extensive experience",
    "i'd love to leverage",
    "cutting-edge",
    "seamless",
    "robust solutions",
)

CLASSIFIER_SYSTEM_PROMPT = """You classify inbound public posts for a private lead radar used by one Python backend developer.

Goal: decide whether the AUTHOR is a plausible buyer of paid implementation help, not merely whether the text is technically relevant.

Good fit includes Python/backend, APIs, webhooks, integrations, automation, data workflows, PostgreSQL, document/CSV/PDF processing, scraping public data, scheduled jobs, practical AI/LLM integrations, and rescue of small production/backend problems.

Important distinctions:
- A buyer asks for help, describes an owned business/production problem, controls a workflow, or can approve paid work.
- A job seeker/service provider is selling their own services or applying to someone else's post. Reject.
- A recruiter advertising a normal employment role is not a direct client opportunity. Reject.
- A generic OSS issue/spec/backlog item without a directed request to hire/pay someone is not a commercial opportunity. Reject.
- A tutorial, learner question, generic discussion, product/vendor self-promotion, or portfolio post is not an opportunity. Reject.
- A community reply from somebody other than the original buyer is usually an applicant/helper, even when the thread title contains buyer language.
- Google Alerts is only a discovery wrapper. For SOURCE=google_alerts, classify the TARGET PAGE described by TITLE, BODY, URL and metadata. The word "Freelancer" in a marketplace site name or title suffix is not evidence that the page author is a freelancer selling services. A marketplace project/request can be a buyer opportunity.
- Do not infer payment merely from words such as client, production, paid generation, budget field, overflow, bounty, or rate when they appear in technical/internal context.
- Be conservative. False positives interrupt the user.

Classify only from the supplied text and metadata. Keep summaries short and factual. Do not generate outreach."""


CLASSIFIER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "classifications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "opportunity_id": {"type": "integer"},
                    "is_opportunity": {"type": "boolean"},
                    "author_type": {
                        "type": "string",
                        "enum": [member.value for member in AuthorType],
                    },
                    "commercial_intent": {
                        "type": "string",
                        "enum": [member.value for member in CommercialIntent],
                    },
                    "payment_intent": {"type": "integer", "minimum": 0, "maximum": 100},
                    "business_impact": {"type": "integer", "minimum": 0, "maximum": 100},
                    "urgency": {"type": "integer", "minimum": 0, "maximum": 100},
                    "technical_fit": {"type": "integer", "minimum": 0, "maximum": 100},
                    "decision_maker_probability": {"type": "integer", "minimum": 0, "maximum": 100},
                    "bounded_task": {"type": "boolean"},
                    "should_contact": {"type": "boolean"},
                    "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
                    "problem_summary": {"type": "string", "maxLength": 220},
                    "rejection_reason": {"type": "string", "maxLength": 180},
                },
                "required": [
                    "opportunity_id",
                    "is_opportunity",
                    "author_type",
                    "commercial_intent",
                    "payment_intent",
                    "business_impact",
                    "urgency",
                    "technical_fit",
                    "decision_maker_probability",
                    "bounded_task",
                    "should_contact",
                    "confidence",
                    "problem_summary",
                    "rejection_reason",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["classifications"],
    "additionalProperties": False,
}


@dataclass(slots=True)
class LLMUsage:
    response_id: str | None
    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    output_tokens: int
    estimated_cost_usd: float


class LLMAPIError(RuntimeError):
    """Sanitized model/API error. Never includes the API key or request headers."""


class LLMBudgetExceeded(RuntimeError):
    pass


def decide_classification(classification: LLMClassification) -> LLMDecision:
    """Convert model evidence into a deterministic gate decision.

    The model may veto obvious false positives with high confidence, but it does
    not create P0 alerts or override hard deterministic rejects.
    """

    negative_authors = {
        AuthorType.JOB_SEEKER,
        AuthorType.SERVICE_PROVIDER,
        AuthorType.LEARNER,
        AuthorType.RECRUITER,
        AuthorType.OSS_MAINTAINER,
        AuthorType.SPAM,
    }
    if classification.confidence >= 75 and not classification.is_opportunity:
        return LLMDecision.REJECT
    if classification.confidence >= 70 and classification.author_type in negative_authors:
        return LLMDecision.REJECT
    if classification.confidence >= 80 and not classification.should_contact:
        return LLMDecision.REJECT
    if (
        classification.is_opportunity
        and classification.should_contact
        and classification.technical_fit >= 55
        and classification.confidence >= 60
    ):
        return LLMDecision.PASS
    return LLMDecision.REVIEW


def _clamp_score(value: Any) -> int:
    try:
        score = int(value)
    except (TypeError, ValueError):
        score = 0
    return max(0, min(100, score))


def parse_classification(item: dict[str, Any]) -> LLMClassification:
    try:
        opportunity_id = int(item["opportunity_id"])
        author_type = AuthorType(str(item["author_type"]))
        commercial_intent = CommercialIntent(str(item["commercial_intent"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise LLMAPIError("Model returned an invalid classification schema") from exc

    classification = LLMClassification(
        opportunity_id=opportunity_id,
        is_opportunity=bool(item.get("is_opportunity")),
        author_type=author_type,
        commercial_intent=commercial_intent,
        payment_intent=_clamp_score(item.get("payment_intent")),
        business_impact=_clamp_score(item.get("business_impact")),
        urgency=_clamp_score(item.get("urgency")),
        technical_fit=_clamp_score(item.get("technical_fit")),
        decision_maker_probability=_clamp_score(item.get("decision_maker_probability")),
        bounded_task=bool(item.get("bounded_task")),
        should_contact=bool(item.get("should_contact")),
        confidence=_clamp_score(item.get("confidence")),
        problem_summary=str(item.get("problem_summary") or "")[:220].strip(),
        rejection_reason=str(item.get("rejection_reason") or "")[:180].strip(),
        decision=LLMDecision.REVIEW,
    )
    classification.decision = decide_classification(classification)
    return classification


def extract_output_text(payload: dict[str, Any]) -> str:
    output = payload.get("output")
    if not isinstance(output, list):
        raise LLMAPIError("Model response did not contain output")
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "output_text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
    if not parts:
        raise LLMAPIError("Model response did not contain structured output text")
    return "".join(parts)


def _candidate_text(opportunity_id: int, opportunity: Opportunity, max_chars: int) -> str:
    metadata = opportunity.metadata
    body = opportunity.body[:max_chars]
    compact_meta = {
        "event_type": metadata.get("event_type"),
        "category_kind": metadata.get("category_kind"),
        "is_topic_author": metadata.get("is_topic_author"),
        "reply_count": metadata.get("reply_count", metadata.get("comments")),
        "topic_author": metadata.get("topic_author"),
        "feed_key": metadata.get("feed_key"),
        "query_key": metadata.get("query_key"),
        "search_query": metadata.get("search_query"),
        "target_host": metadata.get("target_host"),
    }
    return (
        f"ID: {opportunity_id}\n"
        f"SOURCE: {opportunity.source}\n"
        f"URL: {opportunity.url}\n"
        f"AUTHOR: {opportunity.author or ''}\n"
        f"CREATED_AT: {opportunity.created_at.astimezone(timezone.utc).isoformat()}\n"
        f"METADATA: {json.dumps(compact_meta, ensure_ascii=False)}\n"
        f"TITLE: {opportunity.title[:500]}\n"
        f"BODY:\n{body}"
    )



def _sanitize_draft(text: str) -> str:
    return text.replace("—", "-").replace("–", "-").strip()


def parse_lead_brief(item: dict[str, Any]) -> LeadBrief:
    try:
        opportunity_id = int(item["opportunity_id"])
        strategy = ContactStrategy(str(item["contact_strategy"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise LLMAPIError("Model returned an invalid lead brief schema") from exc
    draft = _sanitize_draft(str(item.get("draft") or ""))
    lowered = draft.casefold()
    if any(phrase in lowered for phrase in FORBIDDEN_DRAFT_PHRASES):
        raise LLMAPIError("Model returned generic outreach copy")
    return LeadBrief(
        opportunity_id=opportunity_id,
        problem_summary=str(item.get("problem_summary") or "")[:500].strip(),
        why_fit=str(item.get("why_fit") or "")[:500].strip(),
        money_signal=str(item.get("money_signal") or "")[:300].strip(),
        suggested_scope=str(item.get("suggested_scope") or "")[:500].strip(),
        suggested_price_band=str(item.get("suggested_price_band") or "")[:120].strip(),
        contact_strategy=strategy,
        draft=draft[:1200],
        confidence=_clamp_score(item.get("confidence")),
    )


def _brief_candidate_text(
    opportunity_id: int,
    opportunity: Opportunity,
    context: dict[str, Any],
    max_chars: int,
) -> str:
    return (
        f"ID: {opportunity_id}\n"
        f"SOURCE: {opportunity.source}\n"
        f"AUTHOR: {opportunity.author or ''}\n"
        f"URL: {opportunity.url}\n"
        f"CREATED_AT: {opportunity.created_at.astimezone(timezone.utc).isoformat()}\n"
        f"COMMERCIAL_CONTEXT: {json.dumps(context, ensure_ascii=False)}\n"
        f"TITLE: {opportunity.title[:500]}\n"
        f"BODY:\n{opportunity.body[:max_chars]}"
    )


class OpenAILeadClassifier:
    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        max_post_chars: int,
        max_output_tokens: int,
        input_price_per_mtok: float,
        cached_input_price_per_mtok: float,
        cache_write_price_per_mtok: float,
        output_price_per_mtok: float,
        timeout_seconds: float = 45.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.max_post_chars = max(500, max_post_chars)
        self.max_output_tokens = max(200, max_output_tokens)
        self.input_price_per_mtok = max(0.0, input_price_per_mtok)
        self.cached_input_price_per_mtok = max(0.0, cached_input_price_per_mtok)
        self.cache_write_price_per_mtok = max(0.0, cache_write_price_per_mtok)
        self.output_price_per_mtok = max(0.0, output_price_per_mtok)
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def estimate_request_cost(self, opportunities: Iterable[tuple[int, Opportunity]]) -> float:
        chars = len(CLASSIFIER_SYSTEM_PROMPT)
        count = 0
        for opportunity_id, opportunity in opportunities:
            chars += len(_candidate_text(opportunity_id, opportunity, self.max_post_chars))
            count += 1
        # Conservative local estimate. Actual usage from the API is persisted after every request.
        estimated_input_tokens = max(1, chars // 3)
        estimated_output_tokens = min(self.max_output_tokens, max(220, count * 180))
        return (
            estimated_input_tokens * self.input_price_per_mtok
            + estimated_output_tokens * self.output_price_per_mtok
        ) / 1_000_000

    def _cost_from_usage(self, usage: dict[str, Any]) -> LLMUsage:
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        details = usage.get("input_tokens_details")
        if not isinstance(details, dict):
            details = {}
        cached = min(input_tokens, int(details.get("cached_tokens") or 0))
        cache_write = int(details.get("cache_write_tokens") or 0)
        uncached = max(0, input_tokens - cached)
        cost = (
            uncached * self.input_price_per_mtok
            + cached * self.cached_input_price_per_mtok
            + cache_write * self.cache_write_price_per_mtok
            + output_tokens * self.output_price_per_mtok
        ) / 1_000_000
        return LLMUsage(
            response_id=None,
            input_tokens=input_tokens,
            cached_input_tokens=cached,
            cache_write_tokens=cache_write,
            output_tokens=output_tokens,
            estimated_cost_usd=cost,
        )

    def estimate_brief_cost(
        self,
        opportunity_id: int,
        opportunity: Opportunity,
        context: dict[str, Any],
    ) -> float:
        chars = len(BRIEF_SYSTEM_PROMPT) + len(
            _brief_candidate_text(opportunity_id, opportunity, context, self.max_post_chars)
        )
        estimated_input_tokens = max(1, chars // 3)
        estimated_output_tokens = min(self.max_output_tokens, 650)
        return (
            estimated_input_tokens * self.input_price_per_mtok
            + estimated_output_tokens * self.output_price_per_mtok
        ) / 1_000_000

    async def generate_brief(
        self,
        opportunity_id: int,
        opportunity: Opportunity,
        context: dict[str, Any],
    ) -> tuple[LeadBrief, LLMUsage]:
        if not self.api_key:
            raise LLMAPIError("OPENAI_API_KEY is not configured")
        user_content = _brief_candidate_text(opportunity_id, opportunity, context, self.max_post_chars)
        payload = {
            "model": self.model,
            "store": False,
            "reasoning": {"effort": "none"},
            "max_output_tokens": self.max_output_tokens,
            "input": [
                {"role": "system", "content": BRIEF_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "lead_radar_brief_v1",
                    "strict": True,
                    "schema": BRIEF_SCHEMA,
                }
            },
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport) as client:
                response = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)
        except httpx.HTTPError:
            raise LLMAPIError("OpenAI API network request failed") from None
        if response.status_code >= 400:
            raise LLMAPIError(f"OpenAI API returned HTTP {response.status_code}") from None
        try:
            raw = response.json()
            parsed = json.loads(extract_output_text(raw))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise LLMAPIError("OpenAI API returned invalid structured output") from exc
        if not isinstance(parsed, dict):
            raise LLMAPIError("OpenAI API lead brief payload was incomplete")
        brief = parse_lead_brief(parsed)
        if brief.opportunity_id != opportunity_id:
            raise LLMAPIError("OpenAI API returned a lead brief for the wrong opportunity")
        usage = self._cost_from_usage(raw.get("usage") if isinstance(raw.get("usage"), dict) else {})
        usage.response_id = str(raw.get("id")) if raw.get("id") else None
        return brief, usage

    async def classify(
        self,
        opportunities: list[tuple[int, Opportunity]],
    ) -> tuple[list[LLMClassification], LLMUsage]:
        if not self.api_key:
            raise LLMAPIError("OPENAI_API_KEY is not configured")
        if not opportunities:
            return [], LLMUsage(None, 0, 0, 0, 0, 0.0)

        user_content = "\n\n--- CANDIDATE ---\n".join(
            _candidate_text(opportunity_id, opportunity, self.max_post_chars)
            for opportunity_id, opportunity in opportunities
        )
        payload = {
            "model": self.model,
            "store": False,
            "reasoning": {"effort": "none"},
            "max_output_tokens": self.max_output_tokens,
            "input": [
                {"role": "system", "content": CLASSIFIER_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "lead_radar_classification_v1",
                    "strict": True,
                    "schema": CLASSIFIER_SCHEMA,
                }
            },
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport) as client:
                response = await client.post(
                    "https://api.openai.com/v1/responses",
                    headers=headers,
                    json=payload,
                )
        except httpx.HTTPError:
            raise LLMAPIError("OpenAI API network request failed") from None
        if response.status_code >= 400:
            raise LLMAPIError(f"OpenAI API returned HTTP {response.status_code}") from None
        try:
            raw = response.json()
            parsed = json.loads(extract_output_text(raw))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise LLMAPIError("OpenAI API returned invalid structured output") from exc
        rows = parsed.get("classifications") if isinstance(parsed, dict) else None
        if not isinstance(rows, list):
            raise LLMAPIError("OpenAI API classification payload was incomplete")

        classifications = [parse_classification(item) for item in rows if isinstance(item, dict)]
        expected_ids = {opportunity_id for opportunity_id, _ in opportunities}
        returned_ids = {item.opportunity_id for item in classifications}
        if returned_ids != expected_ids:
            raise LLMAPIError("OpenAI API did not return exactly one classification per candidate")

        usage = self._cost_from_usage(raw.get("usage") if isinstance(raw.get("usage"), dict) else {})
        usage.response_id = str(raw.get("id")) if raw.get("id") else None
        return classifications, usage
