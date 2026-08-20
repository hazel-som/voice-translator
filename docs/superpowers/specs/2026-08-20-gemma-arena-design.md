# Gemma4:12B vs 기존 파이프라인 — 해설 번역 비교 Arena 설계

- 작성일: 2026-08-20
- 상태: 사용자 설계 승인 완료 (2026-08-20), spec 리뷰 대기
- 위치: `/Users/cokoroad/translator-test` (신규 프로젝트)

## 1. 목표

seda-question-bank의 기존 해설(解說) 번역 파이프라인(Gemini 클라우드 기반)이 생성한 다국어 번역과, 로컬 Ollama의 **gemma4:12B**가 동일 환경에서 생성한 번역을 나란히 놓고, **논리적·재현 가능한 근거**로 어느 쪽이 나은지, 그리고 gemma4:12B를 실전 투입할 수 있는 언어가 어디까지인지 판정한다.

- 비교 언어 8개 (고정): `ne`(네팔어), `vi`(베트남어), `id`(인도네시아어), `bn`(뱅골어), `ur`(우르두어), `km`(크메르어), `th`(태국어), `tl`(타갈로그어)
  - 타밀어(ta)는 파이프라인에 존재하지 않아 사용자 확인 후 타갈로그어로 확정. `ru`는 요청 범위 외로 제외. DB의 언어 목록이 arena의 언어 목록을 결정하지 않는다.
- 비교 모델: **gemma4:12B 단일** (사용자 확정). 26b 등 다중 모델 스캐폴딩은 만들지 않는다. 모델 태그는 매니페스트 기록 항목일 뿐이다.
- 모든 결과·승패·판정 논리는 localhost 웹 UI에서 확인 가능해야 한다 (사용자 명시 요구).

## 2. 공정성 원칙 (설계의 뼈대)

1. **동일 환경 재현** — gemma는 기존 파이프라인과 동일하게:
   - 시스템 프롬프트: `local_agent/resources/prompts/explanation/TRANSLATE.md` (언어별 `{{TARGET_LANGUAGE}}`/`{{LANGUAGE_CODE}}` 치환) + 파이프라인의 JSON 출력 서픽스(`_TRANSLATE_JSON_SUFFIX`) 그대로.
   - 유저 프롬프트: `{"target_language": <원어 표기>, "language_code": <코드>, "text": <한글 마스킹된 컴포넌트>}` JSON.
   - 한글 마스킹: `korean_extractor.extract` → 번역 → `restore`, `missing_slots` 검사. 컴포넌트 단위 번역.
   - 샘플링: temperature **0.3** (파이프라인에 명시된 유일한 수치인 Vertex 폴백 값), `num_predict` 8000 (Vertex max_output_tokens 미러링).
   - `num_ctx` **16384 명시 설정** (Ollama 기본값의 조용한 절단 방지; gemma4:12B는 262k 지원). 값은 매니페스트에 기록.
   - 출력 형식: Ollama의 강제 JSON 모드 미사용. 파이프라인과 동일하게 프롬프트 지시 + 파싱 실패 시 재시도. (사용자 승인된 선택)
2. **생존자 편향 대칭** — DB의 기존 번역은 Gate B/C/D + 재시도를 통과한 생존자다. gemma에도 동일한 재시도 사다리를 적용한다: 컴포넌트당 최대 3회 시도(JSON 파싱 실패·플레이스홀더 유실 시), Gate C 실패 시 실패 피드백 주입 재번역 1회 (파이프라인 `retry_failed_gate_c` 미러링).
3. **양측 신규 채점** — 과거 Gate D 점수는 10% 샘플링이므로 절대 재사용하지 않는다. 기존 번역과 gemma 번역 **둘 다** 같은 세션에서 같은 judge(claude-sonnet-4-6, `translator_judge.md` 프롬프트)로 신규 채점한다. 신규 점수와 과거 점수를 섞지 않는다.
4. **기준선의 정직한 표기** — 기존 번역은 3개 경로(배치 gemini-3.5-flash-medium / 어드민 gemini-3.1-flash-lite / KFP)가 혼재하고 행별 출처가 없다. 기준선 라벨은 항상 **"DB에 저장된 기존 파이프라인 번역"** 으로 표기하며 특정 모델명을 기준선 이름으로 쓰지 않는다.
5. **블라인드 평가** — 비교 판정자는 어느 쪽이 gemma인지 모른다. A/B 배정은 (문제×언어)마다 무작위이며 배정 기록을 저장하고, UI에서는 판정 완료 후 unblind해서 표시한다.
6. **운영 편차의 문서화** — 타임아웃은 로컬 추론 속도에 맞춰 컴포넌트당 600초로 완화한다(모델 출력 품질에 영향을 주지 않는 운영 파라미터). 이 편차는 매니페스트에 기록한다.

## 3. 아키텍처

```
/Users/cokoroad/translator-test/
├── server/
│   ├── app.py        FastAPI 앱 + API 라우트 + 정적 UI 서빙 (127.0.0.1:8765)
│   ├── qbank.py      seda-question-bank sys.path 주입 후 local_agent 모듈 재사용:
│   │                 prompt_loader / korean_extractor / gates / db_ops / translator(서픽스·타임아웃 상수)
│   │                 3개 테이블(ept/ept_v3/generated) 자동 검색, 읽기 전용
│   ├── gemma.py      Ollama /api/chat 클라이언트: 마스킹→번역→복원→파싱 재시도→Gate B/C→Gate C 피드백 재시도
│   ├── judge.py      Gate D 채점(양측) + 블라인드 비교 평가 — Claude CLI 서브프로세스 (파이프라인 방식 미러링)
│   ├── runner.py     실행 오케스트레이션 (백그라운드 잡, 진행 상태 발행, 언어 단위 재실행)
│   └── store.py      runs/ 디렉토리 JSON 저장·조회
├── ui/               단일 페이지 UI (빌드 스텝 없는 HTML/CSS/JS, 마크다운 렌더링, ur RTL)
├── prompts/
│   └── compare_judge_<lang>.md 생성 로직 + 템플릿 — translator.md 방법론 각색 (아래 §6)
├── runs/<run_id>/    manifest.json, input.json, baseline/<lang>.json,
│                     gemma/<lang>.json, eval/<lang>.json, summary.json
└── docs/superpowers/specs/  본 문서
```

- 실행 환경: 시스템 python3(3.14, supabase 등 의존성 설치 확인됨). 서버 기동은 `direnv exec /Users/cokoroad/seda-question-bank`로 Supabase 환경변수를 주입한다. 비밀 값은 코드·로그·UI에 절대 노출하지 않는다.
- DB는 **읽기 전용**. arena 결과는 전부 로컬 `runs/` JSON에 저장한다.
- 평가 자동화: Anthropic API 키 불필요. 파이프라인의 Gate D와 동일하게 **Claude CLI 서브프로세스**를 사용한다. 동시 실행은 2개로 제한하고 호출당 타임아웃을 둔다.

## 4. 입력 형식 (UI 첫 화면에서 받는 것)

문제 번호는 세트 상대적이므로 단독 숫자로는 해석 불가. 지원 입력:

1. **bank question UUID** — 3개 테이블에서 자동 검색.
2. **세트 UUID + 문제 번호** — `ept_questions(question_set_id, question_number) → bank_id` 또는 `bank_generated_questions(question_number)` 매핑.
3. **세트 브라우즈** — 세트 목록 → 문제 목록에서 클릭 선택 (UUID를 모를 때).
4. **영어 해설 직접 붙여넣기** — 문제 ID가 함께 있으면 기존 번역과 비교, 없으면 gemma 번역만 생성(비교 없음, "참고용" 표시).

복수 문제 입력(콤마 구분/줄바꿈)을 지원하며 순차 처리한다.

## 5. 실행 플로우

1. 입력 해석 → EN 해설 + 8개 언어 기존 번역 로드 (`db_ops.fetch_explanations_all_langs`). 기존 번역이 없는 언어는 "기준선 없음"으로 표시하고 gemma 번역만 생성.
2. gemma 번역: 언어 × 텍스트 컴포넌트 단위 순차 실행 (Ollama 단일 인스턴스). 비텍스트 컴포넌트(image/audio)는 파이프라인과 동일하게 무변경 통과. 컴포넌트별 소요 시간·토큰 수·재시도 횟수 기록.
3. Gate B(구조 동일성)·Gate C(결정적 5종: korean_pollution, component_ids, english_headers, markers, bracket_balance)를 gemma 결과에 적용. Gate C 실패 → 피드백 주입 재번역 1회 → 재검사. 최종 실패해도 결과는 저장하고 실패 상태를 기록한다 (진단 가치).
4. 평가 (언어 단위, 개별 재실행 가능):
   a. **Gate D 채점**: 기존 번역, gemma 번역 각각 `translator_judge.md` 6기준(정확성·자연스러움·한국어 보존·마크다운 무결성·헤더 현지화·용어 일관성) 채점. judge = claude-sonnet-4-6 (파이프라인 동일).
   b. **블라인드 비교 판정**: §6의 각색 프롬프트로 A/B 승/무/패 + 축별 근거. judge = opus (translator 에이전트의 모델 미러링), Claude CLI `--model` 지정.
5. 집계·판정 (§7) → summary.json → UI 대시보드.

## 6. 블라인드 비교 평가 프롬프트 (translator 에이전트 방법론의 각색)

원본: `/Users/cokoroad/seda/product/.claude/agents/translator.md`. UI 카피용 전제를 제거하고 장문 해설 비교용으로 이식한다.

**유지하는 방법론:**
- 언어별 **3인 페르소나 피드백 라운드** — 페르소나 정의는 `/Users/cokoroad/seda/product/wiki/번역-페르소나-가이드.md`에서 읽기 전용 로드 (8개 대상 언어 모두 커버 확인됨). 실제 인물 발언 위조 인용 금지.
- **6축 비교표** (해설용 각색): ① 의미 정확성(EN 원문 대비 왜곡·누락) ② 자연스러움·레지스터(페르소나 합의) ③ 학습자 인지 부담(EPS-TOPIK 6개월차 초급 어휘·문법 상한) ④ 한국어 보존·형식 무결성(한글 원문 훼손, 마크다운, `[한국어] = [의미]` 이중 표기) ⑤ 용어 일관성(`번역-용어집.md` 대조, 읽기 전용) ⑥ 문화·종교 금기(이슬람권 ur·bn·id / 불교권 th·km / 기독교권 tl).
- **추상어 금지**: "자연스럽다" 단독 판정 금지, 축마다 구체 근거(원문 인용 + 레지스터·어휘 빈도·오역 지목) 강제.
- Steelman: 패자로 판정하는 쪽의 강점 1줄 명시.

**바꾸는 것:** Google Sheet SoT·네임스페이스·TSV/셀 좌표 출력·plan mode 등 UI 카피 운영 전제 전부 제거. 입력은 [EN 원문, 번역 A, 번역 B], 출력은 아래 JSON 스키마.

**출력 스키마 (JSON 강제):**
```json
{
  "winner": "A|B|tie",
  "axes": [{"axis": "의미 정확성", "winner": "A|B|tie", "evidence": "원문 인용 포함 구체 근거"}],
  "persona_round": [{"persona": "...", "pick": "A|B", "rationale": "..."}],
  "steelman_loser": "...",
  "critical_errors": {"A": ["오역/누락 목록"], "B": ["..."]},
  "confidence": "high|medium|low"
}
```

## 7. 승패 판정 논리 (UI에 그대로 노출되는 규칙)

사용자 요구: "어디가 이겼고 어떤 측정 논리로 그런 결과가 나왔는지"를 화면에서 볼 수 있어야 한다. 판정 규칙 자체를 데이터로 저장해 UI에 표시한다.

**(문제 × 언어) 단위 판정:**
1. gemma가 재시도 후에도 Gate B/C 실패 → **기존 파이프라인 승** (사유: "구조 게이트 실패 — 실전 투입 불가"). Gate D·블라인드 판정은 진단용으로 계속 수행하되 승패에는 반영하지 않는다.
2. 게이트 통과 시 → **블라인드 비교 판정의 winner가 1차 결론**. Gate D 점수 차(Δ = gemma − 기존)는 보조 지표. 블라인드 판정과 Gate D 우열이 상충하면 "판정 상충" 플래그를 표시하고 블라인드 판정을 따르되 상충 사실을 화면에 명시한다.

**언어 단위 집계:** 승/무/패 수, Gate D 평균 점수(양측)와 Δ, 게이트 실패율, 평균 소요 시간.

**"실전 투입 가능" 판정 (언어별, 대시보드 최종 결론):** 아래 3개를 모두 충족하면 `deployable`:
- 게이트 실패율 0%
- gemma Gate D 평균 ≥ 파이프라인 임계값 (km·bn·ur 8.0, 그 외 8.5 — 파이프라인과 동일)
- 블라인드 판정에서 승+무 ≥ 50%

각 조건의 충족/미충족과 실제 수치를 대시보드에 조건별로 표시한다.

## 8. localhost UI (127.0.0.1:8765)

빌드 스텝 없는 단일 페이지. 화면 4개:

1. **새 실행**: §4 입력 폼 + 세트 브라우즈 + 언어 선택(기본 8개 전체) + 실행 버튼.
2. **진행 화면**: 언어×컴포넌트 진행 표, 실시간 로그(폴링), 실패 시 해당 언어만 재실행 버튼.
3. **결과 상세 (문제 × 언어)** — 사용자 핵심 요구 반영:
   - 3열 나란히 보기: [EN 원문 | 기존 파이프라인 번역 | gemma4:12B 번역], 마크다운 렌더링, 우르두어 `dir="rtl"`.
   - 게이트 배지: Gate B/C 통과·실패 및 실패 상세(어떤 검사가 왜).
   - Gate D 점수 카드: 6기준 각각 양측 점수 + 총점 + judge의 issues/notes 원문.
   - 블라인드 판정 카드: 승자, 6축 비교표(축별 승자 + 근거 인용), 페르소나 3인 피드백, steelman, 치명 오류 목록, confidence, 블라인드 배정 공개(A=어느 쪽이었는지).
   - 판정 논리 박스: §7의 규칙 중 이 셀에 적용된 경로("게이트 실패로 자동 패배" / "블라인드 판정 채택" / "판정 상충 플래그")를 문장으로 표시.
   - 측정 메타: 소요 시간, 재시도 횟수, 컴포넌트 수.
4. **대시보드 (실행 단위)**: 언어별 집계표(승/무/패, Gate D Δ, 게이트 실패율, 속도), §7 판정 규칙 명시 박스, 언어별 `deployable` 여부와 조건별 충족 현황, 최종 결론 요약문. 과거 실행 목록 조회.

## 9. API

| 메서드 | 경로 | 역할 |
|---|---|---|
| POST | `/api/runs` | 실행 생성 (입력 + 언어 목록) → run_id, 백그라운드 처리 |
| GET | `/api/runs` / `/api/runs/{id}` | 실행 목록 / 상태·결과 전체 |
| POST | `/api/runs/{id}/languages/{lang}/retranslate` | 해당 언어 gemma 번역 재실행 |
| POST | `/api/runs/{id}/languages/{lang}/evaluate` | 해당 언어 평가(Gate D + 블라인드)만 재실행 |
| GET | `/api/sets` / `/api/sets/{id}/questions` | 세트 브라우즈 (3개 테이블) |
| GET | `/` | UI |

## 10. run manifest (재현성)

실행마다 기록: gemma 모델 태그+다이제스트(`ollama show`), ollama 버전, temperature/num_ctx/num_predict, `TRANSLATE.md`·`translator_judge.md`·비교 프롬프트의 SHA-256, JSON 서픽스 사용 여부, 재시도 정책, 타임아웃(편차 명시), judge 모델 2종, 블라인드 배정표, 데이터 소스 테이블, 타임스탬프.

## 11. 에러 처리

- Ollama 미기동/모델 부재 → 실행 전 헬스체크, UI에 명확한 안내.
- Supabase 조회 실패 → 실행 차단 + 원인 표시 (환경변수 미주입 포함).
- Claude CLI 실패/타임아웃 → 해당 언어 평가만 `failed` 상태, 개별 재실행 가능. 전체 실행은 계속.
- JSON 파싱·플레이스홀더 유실 → 재시도 사다리(§2-2), 최종 실패는 상태 기록.
- 판정 불능(비교 프롬프트 JSON 파싱 실패 2회) → "판정 불능" 상태로 저장, UI 표시.

## 12. 테스트

- 단위: 한글 마스킹 왕복(추출→복원 무손실), 게이트 재사용 호출, 블라인드 배정·unblind, 판정 로직(§7) 규칙별.
- 통합: Ollama·Claude CLI를 스텁으로 대체한 전체 플로우.
- 스모크: 실데이터 1문제 × 1언어(vi)로 end-to-end 확인 후 8개 언어 확장.

## 13. 범위 외 (명시)

- `ru`, 타밀어, gemma4:26b 및 다중 모델 비교 UI, DB 쓰기, 기존 파이프라인 코드 수정, 배포(로컬 전용).
