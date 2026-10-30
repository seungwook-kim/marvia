---
# 계약 C6. 채팅 실무 질문 검색(RAG)의 재료 (docs/specs/v2-rag.md 6절, SDD 3.6). 코드(services/rag/)가 이 프론트매터를 읽는다.
# 키 하나는 skills/*/rules.md 중 한 파일에만 둔다. backend/app/services/rules.py가 모두 합쳐 읽고, 같은 키가 두 파일에 있으면 실패한다.
# 이 폴더에는 SKILL.md를 두지 않는다. 어떤 LLM 호출도 이 폴더를 통째로 넣지 않는다 (llm._skills_text는 지정한 폴더만 읽는다).
# 값이 null인 키는 "아직 재지 않아 정하지 않음"이다. 값을 지어 쓰지 않는다. 코드는 null이면 해당 게이트를 끄고 경고를 남긴다 (services/rag/config.py).

# 코퍼스 A가 읽는 Skill 폴더. referrals는 넣지 않는다 (주소·전화는 코드가 붙인다). docs/rag/corpus.yaml과 같아야 한다 (test_rag_chunker가 대조)
rag_corpus_folders: [ais-reliability, customs-stages, cargo-verdict, import-basics, port-busan]

# 청킹 (services/rag/chunker.py). 시작값은 Spec 제안이고 평가셋 A/B(R11)로 확정한다
rag_chunk_target_chars: 500
rag_chunk_max_chars: 800
rag_chunk_min_chars: 80
# 임베딩·BM25 글에 "제목 경로 + 줄바꿈"을 붙일지 (A/B 대상)
rag_context_header: true

# 검색 (services/rag/search.py, R5). 시작값은 Spec 제안
rag_candidates: 10
rag_top_k: 4
rag_rrf_k: 60
# BM25 표준값 (Okapi BM25의 흔한 기본값, Spec "표준값 (제안)")
rag_bm25_k1: 1.5
rag_bm25_b: 0.75

# 질의 재료 (services/rag/query.py). 직전 사용자 말 몇 개를 같이 쓰는지, 질의 글자 수 상한
rag_query_turns: 2
rag_query_max_chars: 200

# 게이트 (Spec 6절). 모델 id별 밀집 1위 코사인 최소값. 모델마다 분포가 달라 같은 숫자를 못 쓴다.
# null = 개발 분할 측정(R9) 전이라 게이트 꺼짐 + 시작 때 경고. 모델 id는 index_manifest.json embedding.model, services/rag/embed.py MODEL_SPECS의 id와 같은 글자를 쓴다(양자화가 다르면 분포가 달라 HF 저장소 이름이 아니라 이 id다). 새 후보 모델은 MODEL_SPECS에 등록할 때 여기에 키를 더한다
rag_min_score:
  multilingual-e5-small-int8: null
# BM25 모드와 밀집 불가일 때: 1위 조각에 일치한 질의 명사 최소 수 (R9에서 정한다)
rag_min_terms: null
# 검색 한 번 상한(ms). R10 지연 측정 뒤에 정한다
rag_search_timeout_ms: null
# 조각 수가 이 값을 넘으면 ANN 인덱스를 추가한다 (공개 문서가 들어온 뒤)
rag_ann_min_chunks: null
---

# 채팅 RAG 검색 재료

코드(services/rag/)가 위 키를 읽는다. 검색 방식과 게이트 규칙은 docs/specs/v2-rag.md 5절, 6절이 원본이다. 이 파일은 숫자와 목록만 둔다.

| 키 | 뜻 | 쓰는 곳 |
|---|---|---|
| rag_corpus_folders | 코퍼스 A가 읽는 Skill 폴더 (SKILL.md만, rules.md 제외) | docs/rag/corpus.yaml, 색인 빌드 |
| rag_chunk_target_chars | 조각 목표 글자 수. 가까워지면 끊는다 | chunker |
| rag_chunk_max_chars | 조각 최대 글자 수. 문장 중간은 자르지 않는다 | chunker |
| rag_chunk_min_chars | 이보다 짧은 절은 앞 조각에 붙인다 | chunker |
| rag_context_header | 임베딩·BM25 글 앞에 제목 경로를 붙인다 | chunker |
| rag_candidates | 검색 방식마다 가져오는 후보 수 | search |
| rag_top_k | 모델에게 주는 조각 수 | search |
| rag_rrf_k | RRF 상수 | search |
| rag_bm25_k1, rag_bm25_b | BM25 매개변수 | bm25 |
| rag_query_turns | 질의에 같이 쓰는 직전 사용자 말 개수 | query |
| rag_query_max_chars | 질의 글자 수 상한 | query |
| rag_min_score | 모델 id별 밀집 1위 코사인 최소값. null이면 밀집 게이트 꺼짐 | config, search |
| rag_min_terms | 1위 조각에 일치한 질의 명사 최소 수. null이면 BM25 게이트 꺼짐 | config, search |
| rag_search_timeout_ms | 검색 한 번의 상한. null이면 상한 없음 | search |
| rag_ann_min_chunks | ANN 인덱스를 추가하는 조각 수 | 마이그레이션 |
