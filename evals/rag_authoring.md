# RAG 검색 평가셋 작성 안내 (빈 양식)

대상 파일: `evals/rag_dataset.jsonl` (아직 없음). 근거: `docs/specs/v2-rag.md` 평가 절, 정할 것 Q7. 이 문서는 칸과 규칙만 있고 문항은 하나도 없다. 문항은 전부 사용자가 직접 채운다.

## 1. 목적과 규칙

- 목적: 채팅 에이전트가 코퍼스 A(`skills/*/SKILL.md`)에서 맞는 조각을 찾는지 재는 검색 평가셋을 만든다. Recall@k, MRR, nDCG, 게이트 거짓 통과/거짓 거절, 실패 유형 분석이 이 파일로 나온다.
- 규모: 36문항 = 질문 종류 9개 x 4. 종류마다 dev 2 + test 2, 합계 dev 18 / test 18 (Q7, 2026-10-31 사용자 결정).
- 사람이 쓴다. 질문, 정답 anchor, 필수 낱말, 모범 답, 이유는 모두 사용자가 쓴다. LLM으로 만들거나 고치지 않는다. RAGAS `TestsetGenerator` 같은 합성 기능도 쓰지 않는다. AI 도구도 이 표를 채우지 않는다.
- 모델 실행 전에 확정한다. 돌려 본 결과를 보고 정답을 고치지 않는다. 고쳐야 하면 `label_reason`에 바꾼 이유를 남긴다.
- dev는 게이트 임계값(`rag_min_score`, `rag_min_terms`)을 정하는 데만 쓴다. test는 그 값이 정해진 뒤 재는 용도라 임계값을 맞추려고 들여다보지 않는다.
- 실제 B/L 번호, MRN, 컨테이너 번호, 전화번호, 이메일을 쓰지 않는다. 질문에 B/L이 필요하면 `[BL1]`, MRN은 `[MRN]`, 컨테이너는 `[CNTR1]`처럼 이미 가린 표시로만 쓴다. 질문은 "이미 가린 글" 모양이어야 한다.
- 정답은 항상 실제 SKILL.md 안의 절을 가리킨다(`answerable=false` 문항 제외). 문서에 없는 사실을 정답으로 쓰지 않는다.
- 파일 안에서 em dash(U+2014)를 쓰지 않는다.

## 2. 한 문항의 칸 (`RagEvalRecord`, 스펙 그대로)

`evals/schema.py`에 추가될 클래스다(아직 만들어지지 않았고 이 작업에서 건드리지 않았다). 한 줄이 JSON 한 객체가 된다.

| 칸 | 타입 | 허용값 / 뜻 | 누가 채우나 |
|---|---|---|---|
| `id` | str | `R01` ... `R36` (스펙 표기 `R01...`) | 3절 표에 미리 적어 둠 |
| `split` | `"dev"` 또는 `"test"` | 게이트 임계값은 dev로만 정한다 | 3절 표에 미리 적어 둠 |
| `question` | str | 사용자가 쓴 질문. 이미 가린 글 모양 | 사용자 |
| `qtype` | 아래 9개 중 하나 | `direct`, `paraphrase`, `abbrev`, `multi_section`, `table_row`, `followup`, `out_of_corpus`, `broker_topic`, `inject_passage` | 3절 표에 미리 적어 둠 |
| `answerable` | bool | `false`면 `gold`를 비운다(거절이 정답). `true`면 `gold` 1개 이상 | 사용자 |
| `gold` | `RagGoldItem` 목록 | 아래 표. `answerable=true`일 때 1개 이상 | 사용자 |
| `reference_answer` | str 또는 null | 사람이 쓴 모범 답. RAGAS `context_recall`과 정답 대조에 쓴다 | 사용자 (4절 표) |
| `labeler` | str | 쓴 사람 이름 | 사용자 |
| `label_reason` | str, 1글자 이상 | 왜 이 질문과 이 정답인지 | 사용자 (3절 비고 칸) |
| `labeled_at` | datetime 또는 null | 확정한 때. 비워도 된다 | 사용자 |

`gold`의 원소 `RagGoldItem`

| 칸 | 타입 | 뜻 |
|---|---|---|
| `source_path` | str | 정답이 있는 파일. 레포 기준 상대 경로, 슬래시. 5절 목록에서 복사 |
| `heading_path_prefix` | str 또는 null | 조각의 `heading_path`가 이 글로 시작하면 정답 조각. 5절 목록에서 복사 |
| `must_include` | str 목록 (기본 빈 목록) | 조각 글에 모두 들어 있어야 정답으로 치는 낱말(한글 낱말 등) |
| `match` | `"heading"`, `"words"`, `"either"`, `"both"` (기본 `either`) | 정답 판정 방식. 값 이름 외의 세부 뜻은 스펙에 적혀 있지 않다. 제목 접두어 규칙(`heading`), 낱말 규칙(`words`), 둘 중 하나(`either`), 둘 다(`both`)로 읽는다 |

주의
- 정답은 조각 번호나 `chunk_id`가 아니라 anchor(파일 + 제목 경로)와 낱말 규칙으로 적는다. 청킹 설정이 바뀌어도 정답이 살아남게 하려는 것이다. 조각 anchor는 `source_path#heading_path[#행]` 모양이다.
- 정답이 너무 많아 변별력이 없거나(예: 문서 제목만 적어 파일 전체가 정답), `answerable=true`인데 gold가 없는 문항은 검증 도구가 거부한다(R11 완료 기준). "너무 많다"의 기준은 R11 구현에서 정해진다.
- 표 한 행이 정답인 `table_row` 문항은 `heading_path_prefix`로 절까지 좁히고, 그 행에만 있는 낱말을 `must_include`에 적어 행을 가린다. 표는 조각 하나 = 행 하나(머리 행 포함)로 잘린다.
- 필수 낱말은 해당 SKILL.md 본문에 실제로 있는 글자 그대로(띄어쓰기 포함) 적는다. 직접 파일을 열어 확인한다.

### 형식 예시 (자리표시자만, 실제 내용 아님)

```json
{"id": "R00", "split": "dev", "question": "<질문을 여기에>", "qtype": "<qtype>", "answerable": true, "gold": [{"source_path": "<source_path>", "heading_path_prefix": "<heading_path_prefix>", "must_include": ["<낱말1>", "<낱말2>"], "match": "either"}], "reference_answer": "<모범 답을 여기에>", "labeler": "<이름>", "label_reason": "<이유를 여기에>", "labeled_at": null}
```

문서에 없는 질문은 `"answerable": false, "gold": []`로 쓴다.

## 3. 질문 종류 9개와 작성표

종류마다 4문항(dev 2, test 2). 정의는 스펙 평가 절의 종류 목록에서 옮겼다.

| qtype | 한 줄 정의 |
|---|---|
| `direct` | 직접 묻기. 문서에 있는 낱말로 그대로 묻는다 |
| `paraphrase` | 우회 표현. 문서의 낱말과 키워드가 겹치지 않게 돌려서 묻는다 |
| `abbrev` | 약어. B/L, D/O, A/N 같은 약어로 묻는다 |
| `multi_section` | 여러 절에 걸침. 정답이 두 절 이상에 나뉘어 있다 (`gold` 2개 이상) |
| `table_row` | 표 한 행. 답이 표의 특정 행 하나에 있다 |
| `followup` | 후속 질문. 직전 사용자 말이 있어야 주제를 알 수 있다 (아래 열린 문제 참고) |
| `out_of_corpus` | 문서에 없음. 수입 실무 질문인데 코퍼스 A에 답이 없다. 날씨 같은 쉬운 무관 질문은 앞단 고정 답이 이미 걸러서 이 종류의 본질이 아니다. `answerable=false` |
| `broker_topic` | 세액·관세사 주제. 세금 계산이나 관세사가 판단할 영역을 묻는다. 답 가능 여부는 사용자가 정한다 |
| `inject_passage` | 조각 주입 문장이 든 질문. 지시처럼 보이는 문장이 섞여 있어도 따르지 않는지 본다 (EC-07) |

칸 쓰는 법 (한 셀에 값이 여러 개면 `;`로 나눈다)

- 답 가능: `O` 또는 `X`(`answerable`). `X`면 정답 anchor와 필수 낱말은 비운다.
- 정답 anchor: 5절 목록의 `파일경로 | 제목 경로`를 그대로 복사한다. 정답이 여러 개면 `;`로 잇는다 (`gold` 원소가 하나씩 생긴다).
- 필수 낱말: anchor마다 `anchor 번호: 낱말a, 낱말b` 식으로 적는다. 없으면 비운다.
- 비고: `label_reason`(이유, 필수)을 쓴다. 기본값(`either`)이 아닌 `match`를 쓰고 싶으면 `match=both`처럼 덧붙인다.

| id | split | qtype | 질문 | 답 가능(O/X) | 정답 anchor | 필수 낱말 | 비고 (이유, match) |
|---|---|---|---|---|---|---|---|
| R01 | dev | direct |  |  |  |  |  |
| R02 | dev | direct |  |  |  |  |  |
| R03 | test | direct |  |  |  |  |  |
| R04 | test | direct |  |  |  |  |  |
| R05 | dev | paraphrase |  |  |  |  |  |
| R06 | dev | paraphrase |  |  |  |  |  |
| R07 | test | paraphrase |  |  |  |  |  |
| R08 | test | paraphrase |  |  |  |  |  |
| R09 | dev | abbrev |  |  |  |  |  |
| R10 | dev | abbrev |  |  |  |  |  |
| R11 | test | abbrev |  |  |  |  |  |
| R12 | test | abbrev |  |  |  |  |  |
| R13 | dev | multi_section |  |  |  |  |  |
| R14 | dev | multi_section |  |  |  |  |  |
| R15 | test | multi_section |  |  |  |  |  |
| R16 | test | multi_section |  |  |  |  |  |
| R17 | dev | table_row |  |  |  |  |  |
| R18 | dev | table_row |  |  |  |  |  |
| R19 | test | table_row |  |  |  |  |  |
| R20 | test | table_row |  |  |  |  |  |
| R21 | dev | followup |  |  |  |  |  |
| R22 | dev | followup |  |  |  |  |  |
| R23 | test | followup |  |  |  |  |  |
| R24 | test | followup |  |  |  |  |  |
| R25 | dev | out_of_corpus |  |  |  |  |  |
| R26 | dev | out_of_corpus |  |  |  |  |  |
| R27 | test | out_of_corpus |  |  |  |  |  |
| R28 | test | out_of_corpus |  |  |  |  |  |
| R29 | dev | broker_topic |  |  |  |  |  |
| R30 | dev | broker_topic |  |  |  |  |  |
| R31 | test | broker_topic |  |  |  |  |  |
| R32 | test | broker_topic |  |  |  |  |  |
| R33 | dev | inject_passage |  |  |  |  |  |
| R34 | dev | inject_passage |  |  |  |  |  |
| R35 | test | inject_passage |  |  |  |  |  |
| R36 | test | inject_passage |  |  |  |  |  |

### 열린 문제 (사용자가 정해 주세요)

- `followup`: 스펙의 `RagEvalRecord`에는 `question` 하나뿐이고 "직전 말"을 담는 칸이 없다. 질의는 직전 사용자 말과 이번 말을 합쳐 만든다(EC-13, `rag_query_turns`). 직전 말을 어디에 쓸지(예: 질문 칸에 두 문장을 이어 쓰기, 또는 칸 추가) 정해야 한다. 칸을 추가하면 계약 변경이라 `schema.py`와 SDD 3.7을 같이 고친다. 결정 전에는 이 종류 4문항(R21~R24)을 비워 둔다.
- `broker_topic`: 코퍼스 A에 답이 있으면 `O`, 없고 관세사 안내로 끝나야 하면 `X`다. 문항마다 사용자가 정한다.

## 4. 모범 답 (선택, `reference_answer`)

RAGAS `context_recall`에 쓴다. 문서에 없는 문항(`X`)은 비워 둔다. 해당 SKILL.md 내용만으로 사람이 쓴다.

| id | 모범 답 |
|---|---|
| R01 |  |
| R02 |  |
| R03 |  |
| R04 |  |
| R05 |  |
| R06 |  |
| R07 |  |
| R08 |  |
| R09 |  |
| R10 |  |
| R11 |  |
| R12 |  |
| R13 |  |
| R14 |  |
| R15 |  |
| R16 |  |
| R17 |  |
| R18 |  |
| R19 |  |
| R20 |  |
| R21 |  |
| R22 |  |
| R23 |  |
| R24 |  |
| R25 |  |
| R26 |  |
| R27 |  |
| R28 |  |
| R29 |  |
| R30 |  |
| R31 |  |
| R32 |  |
| R33 |  |
| R34 |  |
| R35 |  |
| R36 |  |

## 5. 쓸 수 있는 anchor 목록

코퍼스 A = 아래 SKILL.md 5개(스펙 기준 합계 11,045자, `##` 절 25개; 실제 파일 제목 25개와 일치 확인). 청킹 때 맨 앞 프론트매터와 첫 `>` 인용 머리말은 조각에서 빠지고, `출처:` 줄은 조각 글에서 빠져 메타로 간다. 그래서 필수 낱말을 그런 곳에서 고르지 않는다. `heading_path`는 `# 문서 제목 > ## 절` 모양이다. 복사해서 쓴다.

### skills/ais-reliability/SKILL.md
- `# AIS 신뢰도 Skill > ## 이 도메인은 무엇을 다루나` (표 있음)
- `# AIS 신뢰도 Skill > ## 신호 7종`
- `# AIS 신뢰도 Skill > ## 출처 우선순위`
- `# AIS 신뢰도 Skill > ## 위치가 안 바뀔 때, '정박'일 때 (하정운 함정 AIS 판독 경험, 10-06)`

### skills/cargo-verdict/SKILL.md
- `# 화물 등급 설명 Skill > ## 등급의 뜻` (표 있음)
- `# 화물 등급 설명 Skill > ## 표 등급을 설명할 때`
- `# 화물 등급 설명 Skill > ## 회색 지대에서 등급을 올렸을 때`
- `# 화물 등급 설명 Skill > ## 판매 예정일이 없을 때`
- `# 화물 등급 설명 Skill > ## 날짜 말하기`
- `# 화물 등급 설명 Skill > ## 목적지가 어긋날 때 (dest_changed)`
- `# 화물 등급 설명 Skill > ## 코드가 낸 되묻기·보류·실패 결과` (표 있음)

### skills/customs-stages/SKILL.md
- `# 통관 단계 Skill > ## 단계 목록 읽는 법`
- `# 통관 단계 Skill > ## 통관 단계를 말할 때 (plain_explanation, answer, actions 모두)`
- `# 통관 단계 Skill > ## 관세사 확인이 필요할 때`
- `# 통관 단계 Skill > ## 하지 말아야 할 것`

### skills/import-basics/SKILL.md
- `# 수입 실무 기초 Skill > ## 답하는 원칙`
- `# 수입 실무 기초 Skill > ## 1. B/L 번호 찾기`
- `# 수입 실무 기초 Skill > ## 2. A/N(도착통지서)`
- `# 수입 실무 기초 Skill > ## 3. 통관 끝난 뒤 반출`
- `# 수입 실무 기초 Skill > ## 4. 체화료·보관료`
- `# 수입 실무 기초 Skill > ## 5. 혼자 수입신고, 검사`
- `# 수입 실무 기초 Skill > ## 하지 말아야 할 것`

### skills/port-busan/SKILL.md
- `# 부산항 부두 Skill > ## 부두·정박지 개요` (표 있음)
- `# 부산항 부두 Skill > ## 하역 장소 문장 규칙`
- `# 부산항 부두 Skill > ## 하지 말아야 할 것`

anchor를 쓰기 전에 해당 파일을 직접 열어 제목 글자가 위와 같은지 확인한다. 제목이 바뀌면 정답이 어긋난다. 표는 위에 "(표 있음)"으로 적은 네 절에만 있다.

### 코퍼스에서 제외된 파일 (정답으로 쓰지 않는다)

| 파일 | 제외 이유 (스펙 corpus.yaml) |
|---|---|
| `skills/referrals/SKILL.md` (referrals 전체) | 주소·전화는 코드가 붙인다. LLM에 넣지 않는다 |
| `skills/ais-reliability/rules.md`, `skills/cargo-verdict/rules.md`, `skills/customs-stages/rules.md`, `skills/port-busan/rules.md`, `skills/referrals/rules.md` | 프론트매터는 코드가 읽는 재료(C6). 본문 표는 키 설명이라 제외 (Q2, 제안 상태, 아직 확정 아님) |
| `skills/port-busan/cargo-vessels.md` | 프론트매터만 있는 코드용 파일 |
| 공개 관세 문서, 사용자 서류(코퍼스 B) | 아직 아무것도 색인하지 않는다 |

`out_of_corpus` 문항은 제외된 파일에만 답이 있는 질문도 "문서에 없음"이 된다. 정말 코퍼스 A에 답이 없는지 위 5개 파일을 직접 확인한 뒤 `X`로 적는다.

## 6. 채운 뒤 jsonl로 바뀌는 과정

1. 사용자가 3절과 4절 표를 모두 채운다. 36문항, 종류별 dev 2 + test 2를 지킨다.
2. 표를 `evals/rag_dataset.jsonl`로 바꾸는 일은 작업 R11(`RagEvalRecord` + `run_rag_eval.py`)이 맡는 것으로 본다. 스펙에는 표를 jsonl로 바꾸는 스크립트가 따로 적혀 있지 않다. R11 안에 변환기를 둘지 별도 소작업으로 뗄지는 R11 착수 때 사용자가 정한다. 변환기는 기계적으로 옮겨 적기만 한다: 칸 매핑, `O/X`를 `answerable`로, `;`로 나눈 anchor를 `gold` 원소로, 필수 낱말 분배, `labeler`와 `labeled_at` 채움. 질문과 정답은 한 글자도 만들거나 고치지 않는다.
3. 스키마 검증(R11 완료 기준): 모든 문항에 `labeler`와 `label_reason`이 있고, `answerable=true`인데 gold가 없거나 너무 많으면 거부한다. 이 안내의 id 중복 없음과 종류별 dev/test 수(2+2)도 같이 확인한다.
4. 변환된 `evals/rag_dataset.jsonl`을 사용자가 열어 보고 최종 확정한다. 확정 전에는 `run_rag_eval.py`로 결과를 내지 않는다. 확정 뒤에는 정답을 바꾸지 않는다(바꾸면 이유를 `label_reason`에 남긴다).
5. 확정 뒤 `evals/README.md` 구성 표에 `rag_dataset.jsonl` 행을 더한다(R11의 문서 갱신).
