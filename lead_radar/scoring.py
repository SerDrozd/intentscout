from __future__ import annotations

import re
from datetime import datetime, timezone

from .models import Analysis, Opportunity


PROBLEM_CATEGORIES = {
    "api_failures",
    "integrations",
    "webhooks",
    "auth_oauth",
    "sync_data",
    "automation_workflows",
    "scraping_extraction",
    "backend_production",
    "ship_fix_mvp",
}

PAY_TERMS = {
    "willing to pay for help",
    "happy to pay for help",
    "can pay someone to fix this",
    "pay someone to build this",
    "need a freelancer to fix",
    "need a contractor to fix",
}

# These are safe, directional buyer signals. v1.4 treated generic phrases such
# as "contract work", "freelance work", "paid work", "day rate" and "bounty"
# as commercial intent. That confused people advertising themselves, docs and
# historical bounty records with buyers. Keep those broad phrases in the
# matcher for recall, but do not let them open the Telegram gate by themselves.
STRONG_BUYER_COMMERCIAL_TERMS = (
    "willing to pay",
    "happy to pay",
    "we can pay",
    "i can pay",
    "can pay someone",
    "can pay a developer",
    "pay someone to",
    "pay a developer to",
    "budget available",
    "we have a budget",
    "i have a budget",
    "have a budget for",
    "our budget",
    "my budget",
    "budget of $",
    "budget of €",
    "budget of £",
    "need a freelancer",
    "need freelancer",
    "looking for a freelancer",
    "looking for freelancer",
    "hire a freelancer",
    "need a contractor",
    "need contractor",
    "looking for a contractor",
    "looking for contractor",
    "hire a contractor",
    "contractor needed",
    "freelancer needed",
    "paid bounty",
    "open bounty",
    "bounty available",
    "reward for fixing",
    "offering a bounty",
)

# Broad commercial mentions are still useful evidence in SQLite but are not
# enough to interrupt the user. They intentionally include ambiguous language.
COMMERCIAL_MENTION_TERMS = (
    "paid task",
    "paid gig",
    "paid work",
    "paid project",
    "contract work",
    "contract role",
    "freelance work",
    "freelance project",
    "fixed price",
    "hourly rate",
    "day rate",
    "bounty",
    "reward",
)

NEED_SOMEONE_TERMS = (
    "need someone",
    "need somebody",
    "looking for someone",
    "looking for somebody",
    "looking for api developer",
    "looking for backend developer",
    "need a python developer",
    "looking for python developer",
    "anyone available to help",
    "anyone able to help",
)

# High-confidence reverse intent: the author is selling their own labour rather
# than buying help. HN Who Wants to Be Hired comments use several of these.
SELLER_INTENT_TERMS = (
    "open to work",
    "open to contract work",
    "open to freelance work",
    "open to freelance",
    "seeking freelance work",
    "seeking contract work",
    "seeking work",
    "available for work",
    "available for contract work",
    "available for freelance work",
    "available for freelance",
    "available for projects",
    "available for hire",
    "for hire",
    "hire me",
    "my resume",
    "my résumé",
)

SELLER_PROFILE_MARKERS = (
    "location:",
    "remote:",
    "willing to relocate:",
    "technologies:",
    "résumé/cv:",
    "resume/cv:",
    "portfolio:",
    "linkedin:",
    "email:",
)

# Plain "launch" was too broad (app launch != commercial urgency).
URGENT_TERMS = (
    "urgent",
    "asap",
    "deadline",
    "launch tomorrow",
    "launch today",
    "blocking our launch",
    "blocked our launch",
    "customers are waiting",
    "customer is waiting",
    "production is down",
    "prod is down",
)

SOLVED_TERMS = ("solved", "fixed it", "never mind")

# Business context must be owned/first-person. Generic mentions of customers,
# merchants, revenue, etc. are common in discussions and docs.
BUSINESS_TERMS = (
    "our company",
    "my company",
    "our business",
    "my business",
    "our team",
    "my team",
    "our client",
    "my client",
    "our customer",
    "our customers",
    "my customer",
    "paying customer",
    "paying customers",
    "our startup",
    "my startup",
    "we run",
    "we operate",
)

BUYER_INTENT_TERMS = (
    "willing to pay",
    "happy to pay",
    "need someone",
    "looking for someone",
    "looking for help",
    "need help",
    "need a freelancer",
    "looking for a freelancer",
    "need a contractor",
    "looking for a contractor",
    "can someone help",
    "can anyone help",
    "help me fix",
    "help us fix",
    "our client",
    "my client",
    "our customer",
    "our customers",
    "customers are waiting",
    "customer is waiting",
)

OWNERSHIP_TERMS = (
    "i'm",
    "i am",
    "i've",
    "i have",
    "my ",
    "we ",
    "we're",
    "we are",
    "we've",
    "we have",
    "our ",
)

HELP_ACTION_TERMS = (
    "need help",
    "need someone",
    "looking for help",
    "looking for someone",
    "can someone help",
    "can anyone help",
    "anyone know",
    "anyone available",
    "help me",
    "help us",
    "i can't",
    "i cannot",
    "we can't",
    "we cannot",
    "i'm trying",
    "i am trying",
    "we're trying",
    "we are trying",
    "struggling",
    "stuck",
    "stopped working",
    "keeps failing",
    "not working",
    "how do i",
    "how can i",
    "how do we",
    "how can we",
)

AUTOMATION_DESIRE_TERMS = (
    "can this be automated",
    "could this be automated",
    "is there a way to automate",
    "how can i automate",
    "how do i automate",
    "how can we automate",
    "how do we automate",
    "want to automate",
    "need to automate",
    "looking to automate",
    "trying to automate",
    "automate this process",
    "automate this workflow",
)

STRONG_MANUAL_BURDEN_TERMS = (
    "takes hours",
    "takes too long",
    "every day",
    "every week",
    "every monday",
    "every month",
    "manual data entry",
    "manually entering",
    "manually copying",
    "copying data by hand",
    "re-entering data",
    "entering data by hand",
    "too much manual work",
    "keeps getting missed",
    "lots of mistakes",
    "error prone",
    "error-prone",
)

# Common internal backlog/spec language. Not hard excludes: still useful in DB.
GITHUB_BACKLOG_MARKERS = (
    "acceptance criteria",
    "what needs to be done",
    "## scope",
    "## goal",
    "## requirements",
    "## delivery",
    "## outcome",
    "implementation plan",
    "engineering audit",
    "study guide",
    "roadmap",
    "parent: #",
    "depends on #",
    "done when",
    "this issue tracks",
    "feature request",
    "target product architecture",
    "future roadmap",
)

GITHUB_BACKLOG_TITLE_PREFIXES = (
    "spec:",
    "epic:",
    "[epic]",
    "[task]",
    "[feature]",
    "feat(",
    "add ",
    "implement ",
    "support ",
    "deliver ",
    "harden ",
    "verify ",
    "set up ",
    "enforce ",
    "refresh ",
    "build ",
    "choose ",
    "expand ",
)

GITHUB_COMPLETED_MARKERS = (
    "completed claimant",
    "claimant receipt",
    "bounty claimed",
    "already claimed",
    "work completed",
    "paid out",
    "winner selected",
    "submission accepted",
    "completed work",
)

USER_INCIDENT_TERMS = (
    "what happened",
    "what did you do",
    "what did you expect",
    "what did you see",
    "steps to reproduce",
    "to reproduce",
    "i'm trying",
    "i am trying",
    "i can't",
    "i cannot",
    "we can't",
    "we cannot",
    "our webhook",
    "our api",
    "our integration",
    "stopped working",
    "fails with",
    "keeps failing",
    "started right after",
)

HN_JOB_MARKERS = (
    "full-time",
    "full time",
    "part-time",
    "part time",
    "apply here",
    "apply at",
    "apply:",
    "salary",
    "compensation",
    "staff engineer",
    "senior engineer",
    "machine learning engineer",
    "software engineer",
    "hiring",
    "job opening",
    "position",
)


_CURRENCY_BUYER_PATTERN = re.compile(
    r"(?:budget|pay|paying|offering|bounty|reward).{0,40}(?:\$|€|£)\s?\d+",
    re.IGNORECASE,
)


def _term_pattern(term: str) -> re.Pattern[str]:
    escaped = re.escape(term)
    left = r"(?<![\w])" if term and (term[0].isalnum() or term[0] == "_") else ""
    right = r"(?![\w])" if term and (term[-1].isalnum() or term[-1] == "_") else ""
    return re.compile(left + escaped + right)


def _contains_term(text: str, term: str) -> bool:
    return bool(_term_pattern(term).search(text))


def _contains_any(text: str, terms: tuple[str, ...] | set[str]) -> bool:
    return any(_contains_term(text, term) for term in terms)


def _has_strong_buyer_commercial_intent(text: str) -> bool:
    return _contains_any(text, STRONG_BUYER_COMMERCIAL_TERMS) or bool(_CURRENCY_BUYER_PATTERN.search(text))


def _looks_like_seller_post(opportunity: Opportunity, text: str) -> bool:
    if _contains_any(text, SELLER_INTENT_TERMS):
        return True

    # HN's "Who wants to be hired?" template is extremely distinctive.
    profile_hits = sum(1 for marker in SELLER_PROFILE_MARKERS if marker in text)
    if opportunity.source == "hackernews" and profile_hits >= 3:
        return True
    return False


def _looks_like_hn_job(opportunity: Opportunity, text: str) -> bool:
    if opportunity.metadata.get("type") == "job":
        return True
    # Who-is-hiring comments are stored as comments, so use conservative
    # boilerplate detection. Two markers are enough.
    hits = sum(1 for term in HN_JOB_MARKERS if _contains_term(text, term))
    return hits >= 2


def _has_owned_help_intent(text: str) -> bool:
    return _contains_any(text, OWNERSHIP_TERMS) and _contains_any(text, HELP_ACTION_TERMS)


def _has_strong_manual_buyer_signal(text: str, owned_business: bool) -> bool:
    if _contains_any(text, AUTOMATION_DESIRE_TERMS):
        return True
    if owned_business and _contains_any(text, STRONG_MANUAL_BURDEN_TERMS):
        return True
    # First-person repetitive/time-cost pain is useful even when the author
    # does not spell out a company name.
    return _contains_any(text, OWNERSHIP_TERMS) and _contains_any(text, STRONG_MANUAL_BURDEN_TERMS)


def alert_eligibility(
    opportunity: Opportunity,
    analysis: Analysis,
    breakdown: dict[str, int],
) -> tuple[bool, str]:
    """Buyer-oriented gate applied after technical scoring.

    v1.5 separates direction of intent. "Open to contract work" is a seller,
    while "looking for a contractor" is a buyer. Broad commercial words stay
    useful for recall/search but cannot open the Telegram gate by themselves.
    """
    text = opportunity.text.casefold()
    categories = {m.category for m in analysis.phrase_matches}
    technical_signal = bool(categories & PROBLEM_CATEGORIES)
    explicit_help_signal = "explicit_help_hire" in categories
    strong_commercial = _has_strong_buyer_commercial_intent(text)
    build_signal = "buyer_build_request" in categories
    question_signal = "buyer_help_question" in categories
    owned_problem_signal = bool(categories & {"buyer_owned_problem", "buyer_business_problem"})
    manual_pain_signal = "buyer_manual_pain" in categories
    owned_help = _has_owned_help_intent(text)
    owned_business = _contains_any(text, BUSINESS_TERMS)
    seller_signal = _looks_like_seller_post(opportunity, text)

    if seller_signal:
        return False, f"{opportunity.source}_seller_self_promo"

    if opportunity.source == "github":
        # GitHub is mainly backlog/OSS maintenance. Only a directional buyer
        # money/contract signal may interrupt the user. Generic bounty/paid/
        # contract mentions and completed bounty receipts remain database-only.
        if _contains_any(text, GITHUB_COMPLETED_MARKERS):
            return False, "github_completed_or_historical_work"
        if strong_commercial and technical_signal:
            return True, "github_commercial_intent"
        return False, "github_no_commercial_intent"

    if opportunity.source == "hackernews":
        if _looks_like_hn_job(opportunity, text):
            return False, "hn_job_post"
        if strong_commercial and technical_signal:
            return True, "hn_commercial_intent"
        if explicit_help_signal and technical_signal:
            # Explicit questions such as "can anyone help" are useful early
            # intent even before budget is stated. Reverse-intent posts were
            # already removed above.
            return True, "hn_explicit_owned_help" if (owned_help or owned_business) else "hn_explicit_help"
        if build_signal and technical_signal:
            return True, "hn_owned_build_request"
        if manual_pain_signal and _has_strong_manual_buyer_signal(text, owned_business):
            return True, "hn_manual_process_pain"
        if question_signal and technical_signal and (owned_business or owned_help):
            return True, "hn_owned_technical_question"
        if owned_problem_signal and technical_signal and owned_business:
            return True, "hn_owned_business_problem"
        return False, "hn_discussion_or_weak_intent"

    # Future community sources: preserve broad recall but still require a
    # directional buyer/help/owned signal.
    if strong_commercial and technical_signal:
        return True, "generic_commercial_intent"
    if explicit_help_signal and technical_signal:
        return True, "generic_explicit_help"
    if build_signal and technical_signal:
        return True, "generic_build_request"
    if manual_pain_signal and _has_strong_manual_buyer_signal(text, owned_business):
        return True, "generic_manual_process_pain"
    if owned_problem_signal and owned_business:
        return True, "generic_owned_business_problem"
    return False, "generic_weak_intent"


def score(opportunity: Opportunity, analysis: Analysis, now: datetime | None = None) -> tuple[int, dict[str, int]]:
    if analysis.excluded:
        return -100, {"hard_exclude": -100}

    now = now or datetime.now(timezone.utc)
    created = opportunity.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_hours = max(0.0, (now - created).total_seconds() / 3600)

    breakdown: dict[str, int] = {}
    if age_hours < 0.25:
        breakdown["freshness"] = 6
    elif age_hours < 1:
        breakdown["freshness"] = 5
    elif age_hours < 6:
        breakdown["freshness"] = 3
    elif age_hours < 24:
        breakdown["freshness"] = 1
    elif age_hours > 72:
        breakdown["freshness"] = -3

    exact_matches = [m for m in analysis.phrase_matches if m.match_type == "phrase"]
    rule_matches = [m for m in analysis.phrase_matches if m.match_type == "rule"]
    grammar_matches = [m for m in analysis.phrase_matches if m.match_type == "grammar"]
    matched_phrases = {m.phrase.casefold() for m in exact_matches}
    exact_categories = {m.category for m in exact_matches}
    rule_categories = {m.category for m in rule_matches}
    grammar_categories = {m.category for m in grammar_matches}
    categories = exact_categories | rule_categories | grammar_categories
    text = opportunity.text.casefold()
    title = opportunity.title.casefold()

    seller_like = _looks_like_seller_post(opportunity, text)
    strong_commercial = _has_strong_buyer_commercial_intent(text)
    explicit_buyer = False

    if seller_like:
        breakdown["seller_self_promo"] = -10
    elif strong_commercial or matched_phrases & PAY_TERMS:
        breakdown["explicit_pay_or_hire"] = 7
        explicit_buyer = True
    elif _contains_any(text, NEED_SOMEONE_TERMS):
        breakdown["need_someone"] = 6
        explicit_buyer = True
    elif "explicit_help_hire" in exact_categories:
        breakdown["explicit_help"] = 4
        explicit_buyer = True
    elif "explicit_help_hire" in grammar_categories:
        breakdown["explicit_help_grammar"] = 4
        explicit_buyer = True
    elif "explicit_help_hire" in rule_categories:
        breakdown["explicit_help_rule"] = 3
        explicit_buyer = True

    # A broad commercial phrase can still be recorded as weak evidence, but
    # never receives the +7 buyer boost by itself.
    if not seller_like and not strong_commercial and (
        "commercial_intent" in grammar_categories or _contains_any(text, COMMERCIAL_MENTION_TERMS)
    ):
        breakdown["commercial_mention_only"] = 1

    if "buyer_build_request" in grammar_categories:
        breakdown["owned_build_request"] = 4
    elif "buyer_help_question" in grammar_categories:
        breakdown["owned_help_question"] = 3

    if grammar_categories & {"buyer_owned_problem", "buyer_business_problem"}:
        breakdown["owned_problem"] = 3
    if "buyer_manual_pain" in grammar_categories:
        if _has_strong_manual_buyer_signal(text, _contains_any(text, BUSINESS_TERMS)):
            breakdown["manual_process_pain"] = 4
        else:
            breakdown["weak_manual_pain"] = 1

    if exact_categories & PROBLEM_CATEGORIES:
        breakdown["concrete_problem"] = 3
        breakdown["strong_technical_fit"] = 4
    elif categories & PROBLEM_CATEGORIES:
        if grammar_categories & PROBLEM_CATEGORIES:
            breakdown["concrete_problem_grammar"] = 2
            breakdown["technical_fit_grammar"] = 2
        else:
            breakdown["concrete_problem_rule"] = 2
            breakdown["technical_fit_rule"] = 2

    business_context = _contains_any(text, BUSINESS_TERMS)
    owned_help = _has_owned_help_intent(text)
    if business_context:
        breakdown["business_context"] = 3
    # Urgency only matters when attached to an owned/help/commercial context.
    if _contains_any(text, URGENT_TERMS) and (business_context or owned_help or explicit_buyer):
        breakdown["urgent"] = 3

    if opportunity.source == "github":
        backlog_like = _contains_any(text, GITHUB_BACKLOG_MARKERS) or title.startswith(GITHUB_BACKLOG_TITLE_PREFIXES)
        buyer_like = strong_commercial or (
            not seller_like
            and (
                explicit_buyer
                or _contains_any(text, BUYER_INTENT_TERMS)
            )
        )
        user_incident = _contains_any(text, USER_INCIDENT_TERMS)

        if backlog_like:
            breakdown["github_backlog_spec"] = -6
        if _contains_any(text, GITHUB_COMPLETED_MARKERS):
            breakdown["github_completed_or_historical"] = -8
        if user_incident:
            breakdown["user_incident"] = 2

        if buyer_like:
            breakdown["direct_contact_possible"] = 1
        else:
            breakdown["github_low_commercial_intent"] = -6
    elif opportunity.source == "hackernews":
        if (
            not seller_like
            and (
                explicit_buyer
                or owned_help
                or bool(grammar_categories & {"buyer_build_request", "buyer_help_question"})
                or ("buyer_manual_pain" in grammar_categories and _has_strong_manual_buyer_signal(text, business_context))
            )
        ):
            breakdown["direct_contact_possible"] = 1
        if _looks_like_hn_job(opportunity, text):
            breakdown["hn_job_post"] = -8

    soft = tuple(p.casefold() for p in analysis.soft_penalties)
    if any(p in SOLVED_TERMS for p in soft):
        breakdown["already_solved"] = -6
    elif soft:
        breakdown["soft_penalty"] = -3

    total = sum(breakdown.values())
    return total, breakdown
