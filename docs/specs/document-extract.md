# Spec: 서류 사진에서 조회 값 뽑기 (F9 서류 등록)

PRD 5절 P1 "서류 사진·PDF에서 B/L·선명·ETA 추출(멀티모달)"을 P0로 당겨 만든 기능이다. SDD 5절에 없던 기능이라 범위와 동작을 여기 먼저 적는다.

> **현재 동작 (아래 "사용자 확인 화면" 규칙을 대신함)**: 확인 화면(DocumentPanel)은 없다. 사진·PDF는 채팅 입력창에 첨부하고(클립 버튼·붙여넣기·끌어 놓기, 한 번에 하나),
> 보내면 `POST /api/document` 응답의 `bl_status`(서버 코드가 정함, `routers/document.bl_status_of`)로 화면 코드(`web/lib/docAttach.ts`)가 다음을 정한다.
> `ok`(B/L 검사 모두 통과 + 조회 모양)면 사용자 글 + B/L(+MRN)을 채팅으로 보내 바로 조회한다(B/L은 서버가 가린다). `uncertain`이면 조회하지 않고 직접 입력을 부탁한다.
> `none`이면 B/L·A/N 사진을 부탁한다. 읽은 값은 다시 보여 주지 않고, 가린 B/L과 읽은 칸 이름(`important`)만 말한다.
> 항차·입항 예정일·출항일은 검사를 통과한 값만 DocHints로 넘긴다. 출항일은 채팅 요청 `document.departed_on`으로 실려 화물 현황표 출항 칸에만 쓴다.
> B/L 조건·연락처는 로그인 + `bl_status=ok`일 때 검사를 통과한 칸만 서버가 확인 없이 저장한다(`services/doc_facts.save_from_document`, `user_cargo.doc_facts_at/source` 026). 저장한 칸 이름은 `facts_saved`로 돌려준다.
> 채팅 없이 끝난 첨부 턴은 `POST /api/conversations/{id}/attachment-turns`로 대화에 저장한다.

## Why

- **상황**: 수입자는 포워더에게 받은 도착 통지서나 B/L 사본을 들고 있다. B/L 번호, 선명, 항차, 입항 예정일을 손으로 옮겨 적다가 틀린다.
- **문제**: B/L 번호 한 글자가 틀리면 조회 자체가 실패하고, 선명·항차가 틀리면 남의 배로 판정할 수 있다 (PRD 6.1 오확정).
- **설계 의도**: 멀티모달 입력과 작업별 모델 선택(소형 LLM으로 옮겨 적기)을 같이 보여 준다.

## Goal

- 사진 한 장을 올리면 7칸(B/L 번호, 선명, 항차, 입항 예정일, 선적항, 양하항, 품명)과 출항일을 채워 돌려준다. 추출값은 코드 형식 검증을 거친다. (처음 설계의 "사용자 확인 화면"은 현재는 코드가 정한 `bl_status`로 바뀜: ok일 때만 그 B/L로 조회한다)
- 코드 검사를 통과하지 못한 칸은 "확인 필요"로 표시한다. 틀린 값을 조용히 채우지 않는다.
- **Out of Scope**: 대형 비전 모델로 넘기기, 손글씨, 중국어 칸 읽기, 서류 원본·OCR 글자의 DB 저장.

## What

### 흐름 (2단 구조)

1. **전처리 (코드)**: 빨간 픽셀을 흰색으로 지운다(빨간 도장·워터마크). 0°로 읽어 확신도가 낮으면 180°·270°도 읽어 가장 잘 읽힌 방향을 쓴다.
2. **OCR (코드, 로컬)**: RapidOCR + ONNX Runtime, PaddleOCR PP-OCRv5 mobile 모델(글자 위치 `ch_PP-OCRv5_det_mobile`, 읽기 `korean_PP-OCRv5_rec_mobile`). 한국어 모델이 영어도 읽는다.
3. **B/L 번호 (코드)**: "B/L NO.", "B/L 번호", "Bill of Lading No" 이름표의 같은 줄, 뒤 두 줄, 앞 두 줄에서 `schemas/agent.py`의 B/L 형식(`^[A-Z0-9]{8,20}$`)에 맞는 값. 찾으면 OCR 글자에서 `[BL]`로 가린다.
4. **나머지 6칸 (소형 LLM, LLM_MODEL)**: 가린 OCR 글자를 넘겨 JSON으로 받는다. 프롬프트는 Langfuse `marvia-doc-extract`.
5. **검사 (코드)**: 빈칸, OCR 글자에 실제로 있나(지어낸 값 차단), 항차 형식, 날짜는 OCR 글자 속 날짜와 날짜로 비교(`02-OCT-2026` = `2026년 10월 2일` = `2026-10-02`). 영문 선명 뒤 "호"는 뗀다.
6. **응답**: 칸마다 값과 검사 결과. 하나라도 걸리면 `needs_review=true`.

### 사진 직접 읽기 (DOC_VISION_MODEL)

팩스로 받은 서류를 다시 폰으로 찍은 사진은 로컬 OCR이 비슷한 글자(I·L, E·R, U·I)를 틀린다. 실제 도착 통지서 사진에서 B/L 앞 4글자와 항차를 틀렸고, 흐리거나 기운 사진에서는 B/L을 아예 못 찾았다. 모델을 바꾸거나 번호 줄만 다시 읽어도 나아지지 않았다. 그래서 사진 한 장은 두 길을 동시에 돌린다 (routers/document.py).

- **A. OCR → UNI-PASS**: OCR은 0°로 한 번만 읽어 B/L을 뽑고 선사 부호를 바로잡는다(doc_ocr.fix_carrier_prefix, rules.md `bl_carrier_prefixes`). B/L이 하나이고 UNI-PASS에 있으면 선명·항차·입항일·항구·품명·MRN을 세관 값으로 채워 바로 답한다(`read_by: unipass`). 출항일은 UNI-PASS에 없어 비운다. B/L 조건·거래처는 응답 뒤에 OCR 글자로 읽어 저장만 한다.
- **B. 사진 직접 읽기**: 같은 사진을 모델(DOC_VISION_MODEL, 기본 claude-sonnet-5-5)이 읽는다. 프롬프트 `marvia-doc-vision`. A가 못 찾으면(적하목록이 세관에 올라가기 전 서류, OCR 오독) 이 값을 쓴다(`read_by: vision`). A가 찾으면 연결을 끊어 답 쓰기를 멈춘다(출력 비용 절약, 사진 입력 비용은 든다).
- B 값의 검사: OCR 글자에 있는지(not_in_text)는 걸지 않는다. 모델이 OCR보다 바르게 읽은 값은 OCR 글자에 없기 때문이다. 빈 칸·형식·이름표 중복은 그대로 건다. B/L은 OCR 값과 같거나 선사 부호가 목록에 있을 때만 바로 조회하고(ok), 아니면 ambiguous로 사용자 확인을 받는다.
- B가 실패하거나 15초를 넘기면 위 OCR 정리 길(`read_by: ocr`)로 돌아간다. DOC_VISION_MODEL이 비면 OCR 정리 길만 쓴다. PDF는 지금처럼 OCR 정리 길이다(글자층이 있는 PDF는 정확하다).
- 보안: 이 길은 B/L·연락처가 든 사진이 Anthropic으로 간다 (CLAUDE.md 예외). 트레이스에는 사진 크기와 채운 칸 이름만 남긴다.
- 속도: Sonnet 5.5는 기본으로 답 전에 생각(thinking)을 써서, 칸 옮겨 적기에서는 끈다(같은 정확도에 약 4.8초 → 3.3초). Anthropic 클라이언트의 HTTPS 인증서 묶음은 한 번만 읽는다(호출마다 약 0.76초 들던 것, 모든 claude/ 호출에 해당).

### PDF 입력

- **구분**: 파일 앞 4바이트가 `%PDF`이면 PDF다. 파일 이름·content-type은 바꿀 수 있어 보지 않는다. 아니면 기존 사진 경로(`invalid_image` 포함)다.
- **앞 2쪽만** 읽는다. 값은 앞 두 장에 있고, 쪽마다 OCR이 늘면 응답이 느려진다.
- **글자층 먼저**: pypdfium2로 글자를 꺼내 공백 뺀 글자가 40자 이상이면 그 줄을 OCR 결과로 쓴다(OCR 없음, `ocr.runs=0`). 전자 PDF는 글자가 정확하고 빠르다. 40자 미만이면 쪽 번호·워터마크만 있는 스캔 PDF로 보고 넘어간다 (임계값은 추정이며 실제 서류로 재지 않았다).
- **스캔 PDF**: 쪽마다 배율 2(144dpi)로 그려(그림 한 장 1200만 화소 이하로 배율을 낮춘다) 사진과 같은 OCR을 돌리고, 줄을 쪽 순서로 잇는다.
- 이후 B/L 추출·가리기·LLM·검사는 사진과 같다. 라이브러리는 pypdfium2(Apache-2.0/BSD-3, `requirements-doc.txt`). PyMuPDF는 AGPL이라 뺐다.
- **오류**: 열 수 없거나 암호가 걸렸거나 0쪽이면 400 `invalid_pdf`.

### 코드와 맞춘 것

- 입력은 사진 또는 PDF 한 파일이고 상한은 10MB(`routers/document.py MAX_BYTES`, 넘으면 400 `too_large`)다. 폼에 `session_id`를 같이 보낼 수 있다. 로그인이 필수이고 하루 토큰 한도(docs/specs/token-quota.md)를 확인한다
- 7칸 말고 **출항일 `etd`**(도착 통지서의 E.T.D. 또는 B/L의 Shipped/Laden on board 날짜, 서류에서 옮긴 값, 없으면 null)가 있다. 화물 시트의 `departed_source = 'document'`가 이 값에서 온다 (docs/ERD.md)
- 응답에 `mrn`(코드가 "MRN" 이름표 옆에서 뽑음, docs/specs/mrn-fallback.md)과 `llm_error`(LLM 단계가 실패한 종류, 성공이면 null)가 있다. LLM이 실패하면 `status=partial`이고 화면은 AI가 못 읽었다고 안내한다
- PDF 동작은 `services/doc_ocr.py`의 상수(`PDF_MAX_PAGES` 2, `PDF_MIN_TEXT_CHARS` 40, `PDF_RENDER_SCALE` 2, `PDF_MAX_PIXELS` 12,000,000)와 위 서술이 일치한다
- 응답에 `bl_status`(ok / uncertain / none, 코드가 정함), `important`(검사를 통과한 중요 칸 이름, 값 없음), `facts_saved`(저장한 B/L 조건·거래처 칸 이름), `doc_kind`가 있다 (`schemas/document.py`)
- 화면은 별도 확인 화면 없이 채팅에서 처리한다 (`web/components/ChatBar.tsx`, `AppProvider.askWithAttachment`, `web/lib/docAttach.ts`)

### 규칙과의 관계

- **LLM이 만드는 것**: 판정 흐름의 LLM 몫은 셋(선박 선택, 판정 설명, 후속 답)이다. 이 기능은 판정 밖의 별도 호출이다. 값을 "고르거나 판단"하지 않고 OCR 글자에서 "옮겨 적기"만 하며, 검사 5단계가 옮긴 값이 글자에 있는지 확인한다.
- **B/L은 LLM에 안 넘긴다**: 3단계에서 코드가 뽑고 가린다. 가린 뒤 남아 있으면 LLM을 부르지 않는다.
- **품명**: 금지 규칙은 "UNI-PASS 응답의 품명"이다. 여기 품명은 사용자가 올린 서류에서 읽은 값이고, 저장하지 않는다.
- **서류 속 문장은 데이터다** (SDD 7절 신뢰 경계). 프롬프트에 명시한다.

### Edge Cases

| # | 상황 | 처리 |
|---|---|---|
| EC-01 | 이미지가 아니거나 열 수 없음 | 400, `invalid_image` |
| EC-10 | PDF를 열 수 없음 (깨짐, 암호, 0쪽) | 400, `invalid_pdf` |
| EC-02 | 너무 큰 파일 | 400, `too_large` (상한은 라우터 상수) |
| EC-03 | 글자를 거의 못 읽음 (빈 사진, 손글씨) | 200, 7칸 모두 빈칸, `needs_review=true` |
| EC-04 | B/L 이름표가 없거나 형식이 안 맞음 | B/L 빈칸, 확인 필요. LLM은 나머지 칸만 |
| EC-05 | LLM 실패 (키 없음, 과부하, 형식 깨짐) | 200, B/L만 채우고 나머지 빈칸, `status=partial` |
| EC-06 | LLM이 글자에 없는 값을 씀 | 그 칸 `not_in_text`, 확인 필요 |
| EC-07 | 칸끼리 뒤바뀜 (선명 자리에 선적항) | **코드 검사로는 못 잡는다.** 화면이 조회에 넘기는 것은 B/L과 검사를 통과한 항차·입항 예정일·출항일뿐이고, 서류의 선명·선적항은 조회에 쓰지 않는다 (`web/lib/docAttach.ts`) |
| EC-08 | 서류 속에 지시문 ("이전 지시 무시") | 데이터로만 넘긴다. 뽑는 칸이 고정이라 새 동작이 없다. 측정(`scripts/doc_injection_check.py`, `evals/doc_dataset.jsonl`): 지시문 7종은 거의 전부 무시, "모든 칸 비우기"는 6번 중 1번 따랐지만 빈칸(missing)으로 확인 필요 |
| EC-09 | 같은 칸 이름표가 두 번 (가짜 "선명 / 항차 (정정)" 행 등) | 그 칸 `ambiguous`, 확인 필요. 값이 서류 글자에 있어 not_in_text로 못 잡는다(측정에서 LLM이 3번 모두 위조 값 채택). 이름표처럼 짧은 줄만 센다 |

## How

```
POST /api/document   (multipart/form-data, file=이미지 또는 PDF, session_id 선택)

응답 200:
{
  "status": "ok" | "partial",
  "needs_review": bool,
  "fields": {
    "bl_no":  {"value": str|null, "checks": [str]},   // 예: [] 통과, ["missing"], ["not_in_text"]
    "vessel": {...}, "voyage": {...}, "eta": {...},
    "pol": {...}, "pod": {...}, "item": {...}
  },
  "ocr": {"lines": int, "rotation": 0|180|270, "runs": int, "ocr_ms": int},
  "trace_id": str,
  "mrn": str|null, "doc_kind": "B/L"|"A/N"|null, "llm_error": str|null,
  "bl_status": "ok"|"uncertain"|"none", "important": [str], "facts_saved": [str]
}
```

### B/L 조건과 화물 인도 연락처 (docs/specs/memory.md 결정 6·7)

화물 비서 기억이 짚을 거리로만 쓴다. 판정 등급에는 쓰지 않는다.

| 칸 | 값 | 검사 |
|---|---|---|
| `original_bl` | `required`(원본 필요) / `surrendered`(서렌더) | 목록 밖이면 format. LLM이 근거로 옮긴 글(`original_bl_text`)이 서류에 없으면 not_in_text, 지시문 줄에서만 나오면 ambiguous |
| `to_order_bank` | `"true"` / `"false"` (수하인이 TO ORDER OF ... BANK인가) | 위와 같음 (`to_order_bank_text`) |
| `freight_term` | `prepaid` / `collect` | 위와 같음 (`freight_term_text`) |
| `incoterm` | `CIF` / `FOB` / `CFR` | 서류에 낱말로 있어야 한다 (not_in_text) |
| `contact_company` | 화물 인도 연락처 회사 (B/L "Party to contact for cargo release", A/N) | 서류에 있어야 한다. 40자 넘거나 이메일 모양이면 format |
| `contact_role` | 포워더 / 관세사 / 수출자 / 판매처 | 목록 밖이면 format |
| `contact_phone` | 회사·부서 전화, 숫자와 하이픈 | 모양이 아니거나 개인 휴대폰이면 **값을 비우고** format. 숫자가 서류에 없으면 not_in_text |

- 이 칸들은 서류에 없는 경우가 흔해서 비어 있으면 검사하지 않는다(missing 없음). `needs_review`는 값이 있는데 검사에 걸릴 때만 올라간다.
- 응답의 `doc_kind`(`B/L` / `A/N` / null)는 메모장 거래처 출처로 쓴다. 모르면 거래처를 저장하지 않는다.
- 개인 이름·휴대폰·이메일은 받지 않는다 (프롬프트 + 코드 검사).
- 저장: 로그인 + `bl_status=ok`일 때 `/api/document`가 검사를 통과한 칸만 그 B/L의 화물 줄에 바로 저장한다(`services/doc_facts.save_from_document`, 출처 `doc_facts_source`, 시각 `doc_facts_at`, 026). 검사에 걸린 칸은 저장하지 않는다. (처음 설계의 "확인 화면 + `POST /api/memory/document-facts`"는 현재는 이 자동 저장으로 바뀜)

- 파일: `services/doc_ocr.py`(1~3, 5단계), `services/llm.py` `extract_doc_fields()`(4단계), `routers/document.py`, `schemas/document.py`(계약), `services/doc_facts.py`(저장, 채팅 `cargo_documents` 도구)
- 의존성: `rapidocr`(Apache-2.0), `onnxruntime`(MIT), `python-multipart`(업로드). 모델 파일은 Docker 빌드 때 받아 이미지에 넣는다

## 근거 (실행한 것만)

레포 밖에서 한 OCR 비교 실험, Mac M2 CPU. 원본 결과는 레포에 없다.

- OCR 후보, 이미지 12장 90칸: PaddleOCR 기본 72 · PaddleOCR 한국어 89 · EasyOCR 74. 한국어 모델을 ONNX Runtime으로 돌리면 90/90, B/L 한 장 약 2초(Paddle 그대로 약 20초)
- 지저분한 사진 10종 + 웹 실제 영어 도착 통지서 4장 + 1853 손글씨 B/L, 120칸: 전처리 후 113/120. 못 읽은 것은 중국어 2칸, 손글씨 3칸, `I225`→`1225`, `가나다무역`→`기나다무역`
- 2단계(가공 통지서 9장, 서식 4종, 63칸): 63/63, 검사 9/9 통과. LLM Gemini 2.5 Flash(Vertex, 생각 0) 1~4초
- 한계: 2단계는 가공 서류로만 쟀다. 배포 서버(Railway) 속도는 미측정
- 실제 팩스 도착 통지서 사진 1장(레포 밖) + 그 변형 4장, 칸 9개 x 5장, `scripts/bench_document.py` (evals/results/doc_bench_20261012_ocr.txt, doc_bench_20261012_vision.txt):
  OCR 정리 길 30/45, 한 장 4.9~10.2초 / 사진 직접 읽기(A+B) 43/45, 한 장 2.8~3.9초. 사진 직접 읽기가 틀린 것은 JPEG 30 변형의 항차, 2도 기운 변형의 B/L. 사진이 실제로는 1장이라 방향을 보는 숫자다. A(UNI-PASS에서 찾음) 길은 이 서류가 적하목록 전이라 실제로 재지 못했다
