"""채팅 실무 질문용 검색(RAG) 패키지 (docs/specs/v2-rag.md).

코퍼스 A(skills/*/SKILL.md)를 조각으로 나누고(chunker), 가린 글로 질의를 만들고(query), 임계값을 rules.md에서 읽는다(config).
판정 길(judge)은 이 패키지를 쓰지 않는다 (v2-rag.md 확정 결정 1).
"""
