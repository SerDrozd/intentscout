from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re
from typing import TypedDict

from .models import Analysis, Opportunity, PhraseMatch
from .text import normalize_text


class SignalRule(TypedDict, total=False):
    anchors: list[str]
    triggers: list[str]
    max_distance_chars: int


@dataclass
class Matcher:
    config: dict

    def __post_init__(self) -> None:
        matching = self.config["matching"]
        self.case_sensitive = bool(matching.get("case_sensitive", False))
        self.phrases: dict[str, list[str]] = matching["phrases"]
        self.signal_rules: dict[str, SignalRule] = self.config.get("signal_rules", {})
        self.rule_max_distance = int(self.config.get("rule_max_distance_chars", 180))
        self.hard_excludes: list[str] = self.config.get("hard_exclude_phrases", [])
        self.soft_penalties: list[str] = self.config.get("soft_penalty_phrases", [])
        self.positive_context: list[str] = self.config.get("positive_context_phrases", [])

        # v1.4: compositional intent grammar. Instead of maintaining thousands
        # of brittle full sentences, combine technical concepts with synonym
        # groups for help, pain, build intent, questions, ownership and money.
        grammar = self.config.get("intent_grammar", {})
        self.grammar_pair_distance = int(grammar.get("pair_distance_chars", 220))
        self.grammar_context_span = int(grammar.get("context_span_chars", 340))
        self.grammar_technical: dict[str, list[str]] = grammar.get("technical_concepts", {})
        self.grammar_help: list[str] = grammar.get("help_intents", [])
        self.grammar_failures: list[str] = grammar.get("failure_states", [])
        self.grammar_build: list[str] = grammar.get("build_requests", [])
        self.grammar_questions: list[str] = grammar.get("question_intents", [])
        self.grammar_commercial: list[str] = grammar.get("commercial_intents", [])
        self.grammar_ownership: list[str] = grammar.get("ownership_terms", [])
        self.grammar_business: list[str] = grammar.get("business_terms", [])
        self.grammar_manual: list[str] = grammar.get("manual_process_terms", [])
        self.grammar_manual_pain: list[str] = grammar.get("manual_pain_terms", [])

    def coverage_stats(self) -> dict[str, int]:
        exact = sum(len(v) for v in self.phrases.values())
        tech = sum(len(v) for v in self.grammar_technical.values())
        intent = sum(
            len(v)
            for v in (
                self.grammar_help,
                self.grammar_failures,
                self.grammar_build,
                self.grammar_questions,
                self.grammar_commercial,
                self.grammar_ownership,
                self.grammar_business,
                self.grammar_manual,
                self.grammar_manual_pain,
            )
        )
        pair_variants = tech * (
            len(self.grammar_help)
            + len(self.grammar_failures)
            + len(self.grammar_build)
            + len(self.grammar_questions)
            + len(self.grammar_commercial)
        )
        return {
            "exact_phrases": exact,
            "technical_aliases": tech,
            "intent_aliases": intent,
            "pair_variant_equivalents": pair_variants,
        }

    def _prep(self, value: str) -> str:
        value = normalize_text(value)
        return value if self.case_sensitive else value.casefold()

    def _find(self, text: str, phrases: list[str]) -> list[str]:
        found: list[str] = []
        for phrase in phrases:
            needle = self._prep(phrase)
            if needle and needle in text:
                found.append(phrase)
        return found

    @staticmethod
    @lru_cache(maxsize=4096)
    def _bounded_pattern(needle: str) -> re.Pattern[str]:
        escaped = re.escape(needle)
        left = r"(?<![\w])" if needle and (needle[0].isalnum() or needle[0] == "_") else ""
        right = r"(?![\w])" if needle and (needle[-1].isalnum() or needle[-1] == "_") else ""
        return re.compile(left + escaped + right)

    def _occurrences(self, text: str, phrases: list[str]) -> list[tuple[int, int, str]]:
        out: list[tuple[int, int, str]] = []
        for phrase in phrases:
            needle = self._prep(phrase)
            if not needle:
                continue
            for match in self._bounded_pattern(needle).finditer(text):
                out.append((match.start(), match.end(), phrase))
        return out

    @staticmethod
    def _gap(a: tuple[int, int, str], b: tuple[int, int, str]) -> int:
        return max(0, max(a[0], b[0]) - min(a[1], b[1]))

    @staticmethod
    def _span(items: list[tuple[int, int, str]]) -> int:
        return max(x[1] for x in items) - min(x[0] for x in items)

    def _best_pair(
        self,
        left: list[tuple[int, int, str]],
        right: list[tuple[int, int, str]],
        max_distance: int | None = None,
    ) -> tuple[str, str] | None:
        if not left or not right:
            return None
        limit = self.grammar_pair_distance if max_distance is None else max_distance
        best: tuple[int, str, str] | None = None
        for a in left:
            for b in right:
                gap = self._gap(a, b)
                if gap > limit:
                    continue
                if best is None or gap < best[0]:
                    best = (gap, a[2], b[2])
        return None if best is None else (best[1], best[2])

    def _best_triplet(
        self,
        first: list[tuple[int, int, str]],
        second: list[tuple[int, int, str]],
        third: list[tuple[int, int, str]],
    ) -> tuple[str, str, str] | None:
        if not first or not second or not third:
            return None
        best: tuple[int, str, str, str] | None = None
        for a in first:
            for b in second:
                # Cheap pair pruning before the third loop.
                if self._gap(a, b) > self.grammar_context_span:
                    continue
                for c in third:
                    span = self._span([a, b, c])
                    if span > self.grammar_context_span:
                        continue
                    if best is None or span < best[0]:
                        best = (span, a[2], b[2], c[2])
        return None if best is None else (best[1], best[2], best[3])

    def _rule_match(self, text: str, category: str, rule: SignalRule) -> PhraseMatch | None:
        anchors = self._occurrences(text, rule.get("anchors", []))
        triggers = self._occurrences(text, rule.get("triggers", []))
        if not anchors or not triggers:
            return None

        best: tuple[int, str, str] | None = None
        max_distance = int(rule.get("max_distance_chars", self.rule_max_distance))
        for a in anchors:
            for t in triggers:
                gap = self._gap(a, t)
                if gap > max_distance:
                    continue
                if best is None or gap < best[0]:
                    best = (gap, a[2], t[2])
        if best is None:
            return None

        _, anchor, trigger = best
        return PhraseMatch(category=category, phrase=f"{anchor} + {trigger}", match_type="rule")

    def _grammar_matches(self, text: str) -> list[PhraseMatch]:
        if not self.grammar_technical:
            return []

        help_occ = self._occurrences(text, self.grammar_help)
        fail_occ = self._occurrences(text, self.grammar_failures)
        build_occ = self._occurrences(text, self.grammar_build)
        question_occ = self._occurrences(text, self.grammar_questions)
        commercial_occ = self._occurrences(text, self.grammar_commercial)
        ownership_occ = self._occurrences(text, self.grammar_ownership)
        business_occ = self._occurrences(text, self.grammar_business)
        manual_occ = self._occurrences(text, self.grammar_manual)
        manual_pain_occ = self._occurrences(text, self.grammar_manual_pain)

        out: list[PhraseMatch] = []
        seen: set[tuple[str, str]] = set()

        def add(category: str, evidence: str) -> None:
            key = (category, evidence.casefold())
            if key not in seen:
                seen.add(key)
                out.append(PhraseMatch(category=category, phrase=evidence, match_type="grammar"))

        for category, aliases in self.grammar_technical.items():
            tech_occ = self._occurrences(text, aliases)
            if not tech_occ:
                continue

            pair = self._best_pair(tech_occ, commercial_occ)
            if pair:
                tech, intent = pair
                add("commercial_intent", f"{tech} ↔ {intent}")
                add(category, f"{tech} ↔ {intent}")

            pair = self._best_pair(tech_occ, help_occ)
            if pair:
                tech, intent = pair
                add("explicit_help_hire", f"{tech} ↔ {intent}")
                add(category, f"{tech} ↔ {intent}")

            triplet = self._best_triplet(ownership_occ, tech_occ, fail_occ)
            if triplet:
                owner, tech, state = triplet
                add("buyer_owned_problem", f"{owner} ↔ {tech} ↔ {state}")
                add(category, f"{tech} ↔ {state}")

            triplet = self._best_triplet(ownership_occ, tech_occ, build_occ)
            if triplet:
                owner, tech, action = triplet
                add("buyer_build_request", f"{owner} ↔ {tech} ↔ {action}")
                add(category, f"{tech} ↔ {action}")

            triplet = self._best_triplet(ownership_occ, tech_occ, question_occ)
            if triplet:
                owner, tech, question = triplet
                add("buyer_help_question", f"{owner} ↔ {tech} ↔ {question}")
                add(category, f"{tech} ↔ {question}")

            # A business-owned problem may use "our customers" instead of a
            # clean pronoun near the technical phrase.
            triplet = self._best_triplet(business_occ, tech_occ, fail_occ)
            if triplet:
                business, tech, state = triplet
                add("buyer_business_problem", f"{business} ↔ {tech} ↔ {state}")
                add(category, f"{tech} ↔ {state}")

        # Manual-process opportunity grammar. It intentionally does not need a
        # literal "automation" word: "we copy these CSVs by hand every Friday"
        # is exactly the pre-order signal we want to retain.
        if manual_occ:
            triplet = self._best_triplet(ownership_occ, manual_occ, manual_pain_occ)
            if triplet:
                owner, process, pain = triplet
                add("buyer_manual_pain", f"{owner} ↔ {process} ↔ {pain}")
                add("automation_workflows", f"{process} ↔ {pain}")

            for intent_name, intent_occ in (
                ("explicit_help_hire", help_occ),
                ("buyer_build_request", build_occ),
                ("buyer_help_question", question_occ),
                ("commercial_intent", commercial_occ),
            ):
                pair = self._best_pair(manual_occ, intent_occ)
                if pair:
                    process, intent = pair
                    add(intent_name, f"{process} ↔ {intent}")
                    add("automation_workflows", f"{process} ↔ {intent}")

        return out

    def analyze(self, opportunity: Opportunity) -> Analysis:
        text = self._prep(opportunity.text)
        hard = self._find(text, self.hard_excludes)
        soft = self._find(text, self.soft_penalties)
        positive = self._find(text, self.positive_context)

        matches: list[PhraseMatch] = []
        exact_categories: set[str] = set()
        for category, phrases in self.phrases.items():
            for phrase in self._find(text, phrases):
                matches.append(PhraseMatch(category=category, phrase=phrase, match_type="phrase"))
                exact_categories.add(category)

        # Legacy broad rules remain as a fallback, but anchors/triggers must be
        # close and word-bounded.
        for category, rule in self.signal_rules.items():
            if category in exact_categories:
                continue
            match = self._rule_match(text, category, rule)
            if match:
                matches.append(match)

        # v1.4 compositional matcher: word order can change and synonyms can be
        # substituted, as long as the evidence lives in one short context.
        matches.extend(self._grammar_matches(text))

        # Dedupe repeated evidence emitted by exact/rule/grammar layers.
        unique: list[PhraseMatch] = []
        seen: set[tuple[str, str, str]] = set()
        for match in matches:
            key = (match.category, match.phrase.casefold(), match.match_type)
            if key not in seen:
                seen.add(key)
                unique.append(match)

        return Analysis(
            phrase_matches=unique,
            hard_excludes=hard,
            soft_penalties=soft,
            positive_context=positive,
        )
