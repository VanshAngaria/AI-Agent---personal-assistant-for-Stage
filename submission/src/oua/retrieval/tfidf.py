"""Tiny TF-IDF over already-filtered catalogue text. Not an eligibility signal."""

from __future__ import annotations

import math
import re
from collections import Counter

_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


class TfidfIndex:
    def __init__(self, documents: list[str]) -> None:
        self.docs = [tokenize(doc) for doc in documents]
        df: Counter[str] = Counter()
        for tokens in self.docs:
            df.update(set(tokens))
        n = max(len(self.docs), 1)
        self.idf = {term: math.log((n + 1) / (count + 1)) + 1.0 for term, count in df.items()}

    def score(self, query: str, doc_index: int) -> float:
        q = tokenize(query)
        if not q or doc_index >= len(self.docs):
            return 0.0
        tf = Counter(self.docs[doc_index])
        length = max(sum(tf.values()), 1)
        q_tf = Counter(q)
        score = 0.0
        for term, qn in q_tf.items():
            score += (qn) * (tf.get(term, 0) / length) * self.idf.get(term, 0.0)
        return score
