"""BM25 classifier over agent registry metadata.

Pure-Python BM25 (no extra dependency on rank_bm25 / sklearn). The corpus is
small (a handful of agents) so a Counter-based implementation is more than
fast enough -- scoring all docs is O(|query terms|) per request after the
one-shot O(N) index build at construction time.

Document construction per agent:

    tokens(name) * NAME_BOOST
  + tokens(description)
  + tokens(tag) * TAG_BOOST for each tag
  + tokens(example_prompt) for each example

CamelCase / hyphen splitting is on by design so 'AwsSageMakerTrainingSmartAgent'
splits into 'aws', 'sage', 'maker', 'training', 'smart', 'agent' -- otherwise
the agent name contributes almost nothing to retrieval. Stopwords are removed
to avoid 'the', 'a', etc. dominating idf.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable

from multicloud_ai_agent_gateway.core.backend import AgentSpec
from multicloud_ai_agent_gateway.core.routing import RouteDecision

# Conservative stopword list -- only the most common English glue words. We
# intentionally keep domain words like 'cost', 'training', 'job' which look
# like stopwords in general English but are highly discriminative here.
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "do", "for", "from",
        "have", "i", "in", "is", "it", "me", "my", "of", "on", "or", "our",
        "please", "show", "tell", "that", "the", "this", "to", "want", "we",
        "what", "with", "you", "your",
    }
)

# Split camelCase / PascalCase boundaries (e.g. "AwsSageMaker" -> "Aws Sage Maker").
_CAMEL_RE = re.compile(r"([a-z0-9])([A-Z])|([A-Z]+)([A-Z][a-z])")
# Anything that isn't a word character becomes a separator.
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str | None) -> list[str]:
    if not text:
        return []
    expanded = _CAMEL_RE.sub(r"\1\3 \2\4", text)
    return [tok for tok in _TOKEN_RE.findall(expanded.lower()) if tok not in _STOPWORDS and len(tok) > 1]


class BM25Classifier:
    """Rank agents by Okapi BM25 against their metadata documents.

    Parameters
    ----------
    specs:
        AgentSpecs from the registry. The classifier indexes them once at
        construction; agents added later won't be picked unless a new
        classifier is built.
    k1, b:
        Standard BM25 tuning knobs. Defaults (1.5, 0.75) work well across
        most short-document corpora; the agent registry qualifies.
    name_boost, tag_boost:
        Multipliers on tokens from ``spec.name`` / ``spec.tags`` when
        building the document. Higher boost = these tokens carry more
        weight in scoring. We keep example_prompts at boost 1 -- they're
        already typically the longest field, so multiplying them would
        drown out the description.
    """

    def __init__(
        self,
        specs: Iterable[AgentSpec],
        *,
        k1: float = 1.5,
        b: float = 0.75,
        name_boost: int = 2,
        tag_boost: int = 3,
    ) -> None:
        self._specs = list(specs)
        if not self._specs:
            raise ValueError("BM25Classifier needs at least one AgentSpec to index.")
        self._k1 = k1
        self._b = b
        self._name_boost = max(1, int(name_boost))
        self._tag_boost = max(1, int(tag_boost))

        self._docs: list[list[str]] = [self._build_doc_tokens(s) for s in self._specs]
        self._doc_lens: list[int] = [len(d) for d in self._docs]
        self._avgdl: float = sum(self._doc_lens) / max(len(self._docs), 1) or 1.0
        self._tfs: list[Counter[str]] = [Counter(d) for d in self._docs]

        df: Counter[str] = Counter()
        for tf in self._tfs:
            df.update(tf.keys())
        N = len(self._docs)
        # Okapi BM25 idf: log(1 + (N - df + 0.5) / (df + 0.5)) -- always positive.
        self._idf: dict[str, float] = {
            term: math.log(1 + (N - count + 0.5) / (count + 0.5))
            for term, count in df.items()
        }

    @property
    def specs(self) -> list[AgentSpec]:
        return list(self._specs)

    def _build_doc_tokens(self, spec: AgentSpec) -> list[str]:
        tokens: list[str] = []
        name_tokens = _tokenize(spec.name)
        for _ in range(self._name_boost):
            tokens.extend(name_tokens)
        tokens.extend(_tokenize(spec.description))
        for tag in spec.tags:
            tag_tokens = _tokenize(tag)
            for _ in range(self._tag_boost):
                tokens.extend(tag_tokens)
        for example in spec.example_prompts:
            tokens.extend(_tokenize(example))
        return tokens

    def _score_one(self, query_tokens: list[str], doc_idx: int) -> float:
        tf = self._tfs[doc_idx]
        dl = self._doc_lens[doc_idx]
        score = 0.0
        # Each query *term* contributes at most once -- repeated query tokens
        # don't add anything in classic BM25, so set() over the tokens.
        for term in set(query_tokens):
            f = tf.get(term, 0)
            if f == 0:
                continue
            idf = self._idf.get(term, 0.0)
            norm = 1 - self._b + self._b * dl / max(self._avgdl, 1e-9)
            score += idf * f * (self._k1 + 1) / (f + self._k1 * norm)
        return score

    def classify(self, prompt: str) -> RouteDecision:
        query_tokens = _tokenize(prompt)
        scored: list[tuple[str, float]] = [
            (spec.name, self._score_one(query_tokens, idx))
            for idx, spec in enumerate(self._specs)
        ]
        scored.sort(key=lambda entry: entry[1], reverse=True)

        top_name, top_score = scored[0]
        runner_up = scored[1][1] if len(scored) > 1 else 0.0
        margin = top_score - runner_up
        confidence = _confidence_from(top_score, margin)
        candidates = tuple(
            {"agent": name, "score": round(score, 4)} for name, score in scored[:3]
        )

        if top_score <= 0.0:
            reason = "BM25: no query token matched any agent metadata"
        else:
            reason = (
                f"BM25 top match {top_name!r} "
                f"(score={top_score:.3f}, margin={margin:.3f})"
            )

        return RouteDecision(
            selected_agent=top_name,
            reason=reason,
            scores={
                "top_score": round(top_score, 4),
                "margin": round(margin, 4),
                "runner_up_score": round(runner_up, 4),
            },
            method="bm25",
            confidence=round(confidence, 4),
            candidates=candidates,
        )


def _confidence_from(top_score: float, margin: float) -> float:
    """Heuristic confidence in [0,1] derived from score magnitude + margin.

    - If top_score is essentially zero, we have no signal at all -> 0.
    - Otherwise we use margin / top_score (relative gap to runner-up).
      A single-agent runaway wins gets close to 1; a near-tie sinks toward 0.
    """
    if top_score <= 1e-6:
        return 0.0
    return max(0.0, min(1.0, margin / top_score))


__all__: list[str] = ["BM25Classifier"]
