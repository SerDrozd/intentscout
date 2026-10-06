from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from lead_radar.database import Database
from lead_radar.llm import OpenAILeadClassifier, parse_lead_brief
from lead_radar.models import ContactStrategy, LeadBrief, Opportunity


def test_parse_lead_brief_sanitizes_long_dashes() -> None:
    brief = parse_lead_brief(
        {
            "opportunity_id": 42,
            "problem_summary": "Lead routing is broken.",
            "why_fit": "API and webhook work.",
            "money_signal": "Payment not explicit.",
            "suggested_scope": "Fix one webhook path.",
            "suggested_price_band": "$100-200",
            "contact_strategy": "PUBLIC_REPLY",
            "draft": "I can help with the webhook — first I would verify the event payload – then the routing.",
            "confidence": 88,
        }
    )
    assert brief.contact_strategy == ContactStrategy.PUBLIC_REPLY
    assert "—" not in brief.draft
    assert "–" not in brief.draft


def test_database_persists_lead_brief_and_counts_detailed_generation(tmp_path) -> None:
    db = Database(tmp_path / "radar.db")
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO opportunities (
                source, external_id, url, title, body, author, created_at,
                discovered_at, score, score_breakdown_json, content_hash, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("make", "x", "https://example.test/x", "Lead issue", "Body", "buyer", now, now, 10, "{}", "h", "{}"),
        )
        opportunity_id = int(cur.lastrowid or 0)

    brief = LeadBrief(
        opportunity_id=opportunity_id,
        problem_summary="Meta leads are not reaching the workflow.",
        why_fit="This is an API/webhook routing problem.",
        money_signal="No explicit budget yet.",
        suggested_scope="Trace one lead end to end and fix attribution delivery.",
        suggested_price_band="$125-250",
        contact_strategy=ContactStrategy.PUBLIC_REPLY,
        draft="Your click-to-WhatsApp flow can be traced from the incoming event through Make to the CRM destination.",
        confidence=91,
    )
    db.save_lead_brief(brief, model="gpt-6-luna")
    loaded = db.get_lead_brief(opportunity_id)
    assert loaded == brief

    db.record_llm_usage(
        purpose="lead_brief",
        model="gpt-6-luna",
        response_id="resp_brief",
        candidate_count=1,
        input_tokens=500,
        cached_input_tokens=0,
        cache_write_tokens=0,
        output_tokens=150,
        estimated_cost_usd=0.000125,
    )
    usage = db.llm_usage_summary()
    assert usage["detailed_generations_today"] == 1


@pytest.mark.asyncio
async def test_openai_generates_structured_lead_brief() -> None:
    payload = {
        "opportunity_id": 7,
        "problem_summary": "Meta click-to-WhatsApp leads are not reaching the CRM workflow.",
        "why_fit": "The task is mainly webhook payload handling, attribution fields and API delivery.",
        "money_signal": "Payment is not explicit; this is a business workflow pain signal.",
        "suggested_scope": "Trace one lead from Meta through Make into Sheets or HubSpot and fix the broken mapping.",
        "suggested_price_band": "$125-250",
        "contact_strategy": "PUBLIC_REPLY",
        "draft": "The useful first check is whether the click-to-WhatsApp event actually includes the campaign and ad identifiers you need before Make receives it. If you can share the webhook payload shape with private values removed, I can help map the attribution fields and trace where they disappear before Sheets or HubSpot. I would keep the first step narrow: one lead path, one destination, and a repeatable test so you can verify the fix.",
        "confidence": 93,
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["text"]["format"]["name"] == "lead_radar_brief_v1"
        return httpx.Response(
            200,
            json={
                "id": "resp_brief",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(payload)}]}],
                "usage": {"input_tokens": 900, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 180},
            },
        )

    classifier = OpenAILeadClassifier(
        api_key="test-key",
        model="gpt-6-luna",
        max_post_chars=5000,
        max_output_tokens=1200,
        input_price_per_mtok=0.10,
        cached_input_price_per_mtok=0.01,
        cache_write_price_per_mtok=0.125,
        output_price_per_mtok=0.50,
        transport=httpx.MockTransport(handler),
    )
    opportunity = Opportunity(
        source="make",
        external_id="7",
        url="https://example.test/7",
        title="Click to WhatsApp leads missing attribution",
        body="Our leads are not reaching the expected workflow.",
        author="buyer",
        created_at=datetime.now(timezone.utc),
    )
    brief, usage = await classifier.generate_brief(
        7,
        opportunity,
        {"cash_score": 58, "value_score": 74, "llm_decision": "REVIEW"},
    )
    assert brief.opportunity_id == 7
    assert brief.contact_strategy == ContactStrategy.PUBLIC_REPLY
    assert "—" not in brief.draft
    assert usage.response_id == "resp_brief"


def test_phase4_schema_migrates_with_lead_briefs_table(tmp_path) -> None:
    db = Database(tmp_path / "radar.db")
    with db.connect() as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "lead_briefs" in tables
