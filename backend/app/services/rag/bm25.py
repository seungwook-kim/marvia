"""한국어 명사 BM25 (docs/specs/v2-rag.md 5절 2번, 작업 R5). 프로세스 안 메모리 색인이다.

토큰은 Kiwi 명사(NNG, NNP, 2글자 이상), 어절 명사 줄기, 영문 약어(B/L, D/O, A/N 같은 슬래시 약어, CY, AIS 같은 대문자 약어)다.
조사·어미가 달라도 명사가 같으면 맞는다. Kiwi는 validate._kiwi()가 돌려주는 객체를 그대로 쓴다(두 번째 Kiwi를 만들지 않는다.
Kiwi를 만드는 데 1.4초쯤, 메모리 수백 MB가 든다). 이 파일은 kiwipiepy를 import하지 않는다.
사전에 없는 도메인 낱말(체화료, 묘박지)이 문맥마다 다르게 쪼개지는 문제는 어절 명사 줄기 토큰으로 막는다 (_stem_tokens).
k1, b는 skills/rag-search/rules.md rag_bm25_k1, rag_bm25_b에서 읽는다 (코드에 숫자를 적지 않는다).
순위 점수(BM25)는 코사인과 척도가 달라 점수끼리 더하지 않고 순위만 RRF에 넘긴다 (search.py, Spec 5절 4번).
"""
from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from backend.app.services import validate
from backend.app.services.rules import rule

# idf 식의 평활 상수 0.5 (Okapi BM25 정의의 일부이지 조정하는 임계값이 아니다)
IDF_SMOOTHING = 0.5
# 명사 품사 (Kiwi 태그: 일반명사, 고유명사), 토큰으로 쓰는 최소 글자 수. validate._nouns와 같은 규칙이다 (Spec 5절 2번 "2글자 이상")
NOUN_TAGS = ("NNG", "NNP")
NOUN_MIN_CHARS = 2
# 어절의 명사 줄기가 끝나는 지점: 조사(J..), 어미(E..), 기호(S..), 용언 만드는 접미사와 이다(XSV, XSA, VCP, VCN) 앞
STEM_STOP_PREFIXES = ("J", "E", "S", "XSV", "XSA", "VCP", "VCN")
# 슬래시 영문 약어: B/L, D/O, A/N, M/H 등 (글자 하나, 슬래시, 글자 하나)
_SLASH_ABBR = re.compile(r"(?<![A-Za-z])[A-Za-z]/[A-Za-z](?![A-Za-z])")
# 대문자 약어: 대문자만 2글자 이상 (CY, AIS, MMSI, ETA, UNI-PASS의 UNI와 PASS)
_UPPER_ABBR = re.compile(r"(?<![A-Za-z])[A-Z]{2,}(?![A-Za-z])")


def _stem_tokens(text: str, tokens: list) -> list[str]:
    """어절(공백으로 나눈 덩어리)마다 앞의 명사 줄기를 하나의 토큰으로 낸다. 어절이 명사로 시작할 때만, 첫 조사·어미·기호 앞까지.

    왜: Kiwi는 사전에 없는 낱말을 문맥에 따라 다르게 가른다 ("체화료가"는 체화료 하나인데 "체화료는"은 체, 화, 료로 쪼개고,
    "묘박지는"은 묘, 박, 지로 쪼개며 어떤 문맥에서는 지를 동사로 본다). 한 글자 명사는 버리므로 이런 도메인 낱말이 색인과 질의에서
    다르게 갈라져 서로 못 만난다. 어절의 조사 앞 글자를 그대로 한 토큰으로 내면 두 쪽이 같은 줄기("체화료", "묘박지")를 얻는다.
    """
    stems: list[str] = []
    by_start = {t.start: t for t in tokens}
    for m in re.finditer(r"\S+", text):
        first = by_start.get(m.start())
        if first is None or first.tag not in NOUN_TAGS:
            continue  # 명사로 시작하지 않는 어절(동사, 기호 등)은 줄기를 내지 않는다
        end = m.end()
        for t in tokens:
            if t.start >= m.end():
                break
            if t.start > m.start() and t.tag.startswith(STEM_STOP_PREFIXES):
                end = t.start
                break
        stem = text[m.start():end]
        if len(stem) >= NOUN_MIN_CHARS:
            stems.append(stem)
    return stems


def tokenize(text: str) -> list[str]:
    """글에서 BM25 토큰을 뽑는다: Kiwi 명사(NNG, NNP, 2글자 이상, validate._nouns와 같은 규칙), 어절 명사 줄기(_stem_tokens),
    영문 약어(소문자로 맞춤). Kiwi는 validate._kiwi()가 돌려주는 하나를 쓴다 (두 번째 인스턴스를 만들지 않는다).
    같은 토큰이 여러 번 나오면 여러 번 돌려준다 (문서 쪽 빈도 계산에 쓴다). 줄기가 명사와 같으면 한 번만 낸다."""
    text = text or ""
    morphs = list(validate._kiwi().tokenize(text))
    nouns = [t.form for t in morphs if t.tag in NOUN_TAGS and len(t.form) >= NOUN_MIN_CHARS]
    tokens = list(nouns)
    seen = set(nouns)
    for stem in _stem_tokens(text, morphs):
        if stem not in seen:
            tokens.append(stem)
            seen.add(stem)
    tokens.extend(m.group(0).lower() for m in _SLASH_ABBR.finditer(text))
    tokens.extend(m.group(0).lower() for m in _UPPER_ABBR.finditer(text))
    return tokens


@dataclass(frozen=True)
class Bm25Hit:
    """BM25 결과 하나. index는 색인을 만들 때 준 글 목록의 자리, matched는 질의 토큰 중 이 글에 있는 서로 다른 토큰 수다
    (게이트의 rag_min_terms가 쓴다)."""

    index: int
    score: float
    matched: int


class Bm25Index:
    """글 목록 하나에 대한 Okapi BM25 색인. 만든 뒤 바뀌지 않는다.

    idf = ln(1 + (N - n + 0.5) / (n + 0.5)): 흔한 낱말도 음수가 되지 않는 꼴이다. 질의 토큰은 서로 다른 것만 한 번씩 센다
    (질의에서 같은 낱말을 되풀이해도 점수가 부풀지 않게).
    """

    def __init__(self, docs: Sequence[str], *, k1: float | None = None, b: float | None = None):
        # k1, b를 안 주면 rules.md 값을 읽는다. 테스트가 값을 직접 줄 수 있게 열어 두었다
        self.k1 = float(rule("rag_bm25_k1") if k1 is None else k1)
        self.b = float(rule("rag_bm25_b") if b is None else b)
        self._tf: list[Counter] = [Counter(tokenize(doc)) for doc in docs]
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg_len = (sum(self._len) / len(self._len)) if self._len else 0.0
        df: Counter = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        n = len(self._tf)
        self._idf = {term: math.log(1 + (n - count + IDF_SMOOTHING) / (count + IDF_SMOOTHING)) for term, count in df.items()}

    def __len__(self) -> int:
        return len(self._tf)

    def query_terms(self, text: str) -> list[str]:
        """질의 글의 토큰 (서로 다른 것만, 처음 나온 순서). 색인과 같은 토큰화를 쓴다."""
        return list(dict.fromkeys(tokenize(text)))

    def matched(self, doc_index: int, terms: Sequence[str]) -> int:
        """질의 토큰 중 이 글에 있는 서로 다른 토큰 수."""
        tf = self._tf[doc_index]
        return sum(1 for term in terms if term in tf)

    def score(self, doc_index: int, terms: Sequence[str]) -> float:
        """글 하나의 BM25 점수. 일치한 토큰이 없으면 0."""
        tf = self._tf[doc_index]
        norm = 1 - self.b + self.b * (self._len[doc_index] / self._avg_len if self._avg_len else 0.0)
        total = 0.0
        for term in terms:
            freq = tf.get(term, 0)
            if freq:
                total += self._idf[term] * freq * (self.k1 + 1) / (freq + self.k1 * norm)
        return total

    def rank(self, terms: Sequence[str], limit: int) -> list[Bm25Hit]:
        """점수가 0보다 큰 글을 높은 순으로 최대 limit개. 점수가 같으면 글 목록의 앞쪽이 먼저라 결과가 결정적이다."""
        scored = []
        for i in range(len(self._tf)):
            s = self.score(i, terms)
            if s > 0:
                scored.append(Bm25Hit(i, s, self.matched(i, terms)))
        scored.sort(key=lambda h: (-h.score, h.index))
        return scored[: max(limit, 0)]
