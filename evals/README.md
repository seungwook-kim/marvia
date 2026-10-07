# 평가셋과 평가 실행기

개선 전후를 같은 셋으로 비교한다 (SDD 5.12, PRD 8절). 결과 숫자는 `evals/results/`의 파일과 루트 `EVAL_REPORT.md`에만 있다.

## 구성

| 파일 | 내용 |
|---|---|
| `dataset.jsonl` | 판정 평가셋 30건. 정상 8(fine 4·act 4) · 경계 판정형 9 · 경계 보류형 5 · 실패 8. 한 줄 모양은 `schema.py`(계약 C7) |
| `doc_dataset.jsonl` | 서류 사진 주입 확인 12건. `python -m scripts.doc_injection_check`가 쓴다 |
| `fixtures/` | 외부 API 가공 응답(ShipFinder, 해수부, UNI-PASS, PortWatch, aisstream)과 재생 모드 파일(`fixtures/replay/`), 데모 시나리오(`demo_live.jsonl`, `demo_papers.jsonl`) |
| `results/` | 실행 결과. 실행마다 새 파일 `<config>_<한국시각>.json`. 주입 측정은 `results/injection/` |

종류마다 왜 넣었나

| 종류 | 왜 필요한가 |
|---|---|
| 정상 케이스 | 기본 동작 확인 |
| 경계 케이스 | 신호가 엇갈리거나 정보가 모자란 입력에서 판정하는지, 되묻는지 |
| 실패 유도 | 없는 B/L, 지원 범위 밖, 주입 문장, 스키마를 깨는 입력 |

잘 되는 것만 모으면 평가가 되지 않으므로 실패 문항을 처음부터 넣었다. 입력과 정답(`expected_*`)은 사람이 모델 실행 전에 정하고 `labeler`·`label_reason`을 남긴다. LLM으로 만들거나 고치지 않는다. 모든 B/L·선박 값은 가공값이다.

## 실행

```
python -m evals.run_eval --config a     # 규칙만 (baseline_rules.py, LLM 없음)
python -m evals.run_eval --config b     # 규칙 + 서비스 모델 (.env LLM_MODEL)
python -m evals.run_eval --config c     # 규칙 + 비교 모델 (.env EVAL_COMPARE_MODEL)
```

- 문항마다 fixtures를 재생 파일로 풀고(`replay_fixtures.py`) 시계를 문항의 clock으로 고정한 채 실제 `run_agent`에 넣는다. 외부 API는 키 없이 재생하고 (b)(c)만 진짜 LLM을 부른다. 웹 검색 도구는 끈다
- (a)와 (b)는 같은 판정 표 코드(`services/verdict.py`)를 써서 "같은 규칙, LLM 유무"만 다르다
- 결과 파일에는 축별 점수, 게이트, 문항별 LLM 호출(모델·토큰·비용·지연·실패 종류), 실행한 커밋과 프롬프트 버전이 들어간다
- Langfuse 키가 있으면 실행 한 번을 Dataset `marvia-golden-30`의 실험으로 남긴다. 평가셋 사본 올리기는 `python -m evals.push_dataset`

## 판정 방식

- 판정 등급·상태·되묻기 여부: 정답과 정확 일치
- 출력 계약: 스키마 통과 여부. 여러 결과 파일을 합친 준수율은 `python -m evals.contract_rate evals/results/b_*.json`
- 통관 해설: LLM-as-judge(`judge.py`, `EVAL_GRADER_MODEL`, 프롬프트 `marvia-judge-eval`). 생성 모델과 다른 회사 모델로 채점한다. 같은 모델이면 점수가 후해지기 때문이다. 채점기 자체 점검은 `results/grader_check_20261001.md`

각 방식의 한계는 `EVAL_REPORT.md`에 적는다. 운영자 화면 `/metrics`는 `services/metrics.py`가 이 결과 파일을 읽어 보여 준다.
