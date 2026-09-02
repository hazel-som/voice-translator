# 음성 → 번역 웹 서버 (muse 기반) 설계

- 작성일: 2026-09-02
- 상태: 사용자 채팅 승인(STT=브라우저 Web Speech API, 언어=한국어 ↔ 타갈로그어) 후 구현
- 위치: `voice-translator/` (본 워크트리)

## 1. 목표

마이크로 말하면 문장 단위로 인식해 즉시 번역해 보여주는 localhost 웹 앱. 번역 LLM은 사용자가 설치한 **Muse Code** (`muse`, Meta의 터미널 코딩 에이전트) 의 헤드리스 모드 `muse exec` 를 사용한다.

- 기본 방향: 한국어 → 타갈로그어. 버튼 한 번으로 타갈로그어 → 한국어로 전환 (양방향 대화용).
- 음성 인식(STT)은 브라우저 Web Speech API (`ko-KR`, `fil-PH`). 서버는 텍스트만 받는다. 인식기는 언어를 미리 알아야 하므로 "자동 감지"는 하지 않는다.
- 번역문 읽어주기(TTS)는 브라우저 `speechSynthesis`. macOS에는 타갈로그 음성이 없으므로 해당 언어 음성이 없으면 버튼을 비활성화한다.

## 2. 확인된 사실 (2026-09-02)

- `muse` 1.0.1. `muse exec --json` 은 JSONL 이벤트를 stdout으로 낸다. 출력 텍스트는
  `payload_type == "run.output.delta"` 의 `payload.text` (스트리밍 조각) 와
  `payload_type == "run.terminal.completed"` 의 `payload.text` (최종 전문, `payload.terminal == "completed"`) 에 있다.
  `--provider echo` 로 자격증명 없이 동일 스키마를 재현할 수 있다 (테스트 픽스처 출처).
- 프로세스 기동 오버헤드 약 0.6초 (echo provider 기준). 모델 응답 시간은 로그인 후 측정.
- `--reasoning-effort none` 은 meta provider에서 거부됨 → `minimal` 사용. `--max-model-steps 1`, `--user-input-auto-resolve` 로 도구 호출·질문 없이 한 스텝만 돌린다.
- 최상위 옵션(`--no-session-log` 등)은 `exec` 앞에 둘 수 없다. 세션 로그는 `~/.local/share/muse` 에 남는다.
- 로그인은 기기 코드 방식(`muse login`)으로 사용자만 할 수 있다. 미로그인 시 `missing meta credentials` 로 즉시 실패한다.
- 로그인 후에도 Meta Model API 계정에 결제 설정이 없으면 모델 호출이 **HTTP 402** 로 거부된다 (2026-09-02 실측). muse는 `task.lifecycle.status` 이벤트(`phase: retry_scheduled`, facet `http_status`)를 내며 최대 10회 지수 백오프 재시도한다. 앱은 401/402/403을 보면 즉시 프로세스를 죽이고 결제 안내(`https://accountscenter.meta.com/muse_code/?ep=no_payg`)를 오류로 보낸다. 그 외 상태는 `{"status": …}` 로 UI에 흘린다.

## 3. 아키텍처

```
voice-translator/
├── server.py        표준 라이브러리만 사용. ThreadingHTTPServer, 127.0.0.1:8787
│                    GET  /              → index.html
│                    GET  /api/health    → {backend, ready, detail}
│                    POST /api/translate → NDJSON 스트림 {"delta"} … {"done", "text", "ms"}
├── translator.py    build_prompt(), MuseBackend, OllamaBackend, extract_muse_text()
├── test_translator.py  unittest (프롬프트 구성, JSONL 파싱, 백엔드 선택)
├── index.html       빌드 없는 단일 페이지: 방향 전환, 마이크, 타이핑 입력, 대화 로그, 읽어주기
└── README.md
```

- **백엔드 분리**: `--backend muse|ollama|echo`. 기본 muse. ollama(gemma4:12B, 이미 설치)는 muse가 느리거나 미로그인일 때의 대체. echo는 자격증명 없는 배관 테스트용.
- **muse 호출**: 요청마다 빈 임시 작업 디렉터리에서 `muse exec --json --reasoning-effort minimal --max-model-steps 1 --user-input-auto-resolve --prompt-file <tmp>` 실행. 프롬프트 파일을 쓰므로 인용 문제와 `ps` 노출이 없다. stdout을 줄 단위로 읽어 delta를 즉시 클라이언트로 흘린다. 타임아웃 90초.
- **프롬프트**: 번역기 역할 지시 + 원문을 `<text>` 태그로 감싸 데이터임을 명시 + "번역문만 출력". 코딩 에이전트가 원문을 명령으로 해석하지 않도록 한다.
- **동시성**: 발화가 겹치면 각 발화는 독립 요청으로 처리하고 UI는 도착 순서와 무관하게 자기 행에 결과를 채운다. 서버는 세마포어로 동시 muse 프로세스를 2개로 제한한다. 이전 발화를 취소하지 않는다(대화 기록이 목적).
- **오류 처리**: muse 비정상 종료·타임아웃·미로그인은 `{"error": …}` 로 스트림에 실어 보내고 해당 행에 표시. 서버는 죽지 않는다.

## 4. 테스트

- 단위: `python3 -m unittest voice-translator/test_translator.py` — echo provider에서 캡처한 실제 JSONL 픽스처로 파싱 검증.
- 통합: `--backend echo` 로 서버 띄우고 curl → NDJSON 스트림 확인. `--backend ollama` 로 실제 ko→tl 번역 확인.
- muse 실경로: 사용자 `muse login` 후 지연 측정 및 결과 확인.

## 5. 변경 이력

- 2026-09-02 (같은 날, 사용자 결정): Meta 결제를 하지 않기로 함 → 번역 백엔드 기본값을 **Antigravity CLI `agy -p --output-format json`** (Google 구독 로그인, `~/.gemini/antigravity-cli/`) 로 변경. 모델 기본 `gemini-3.7-flash-low`. 문장당 약 6초(모델 1초 + CLI 기동 5초). 프롬프트는 argv로만 전달 가능(stdin 모드 없음). 출력은 `{"status":"SUCCESS","response":…}` 한 줄.
- 로컬 ollama gemma4:12B는 `think:false` 로 1~2초까지 빨라졌으나 타갈로그 품질이 낮아("안녕하세요, 안녕하세요" → "halo, kumusta po kayo") 사용자 요청으로 사용 중단. 백엔드는 남겨 둠.
- 핸드폰 지원: `--lan` 플래그 = 0.0.0.0 바인드 + openssl 자체 서명 인증서(SAN에 LAN IP 포함, `certs/` gitignore) HTTPS. 브라우저가 마이크를 https/localhost에서만 허용하기 때문.
- 사용자 요구(2026-09-02 저녁): ①자동 읽어주기 항상 켜짐 ②열 때 항상 한국어→타갈로그어 ③언어 선택 UI 유지. 타갈로그 음성이 Mac·iOS에 없어 **서버 TTS** 추가: `GET /api/tts?text&lang` → `uvx edge-tts --voice fil-PH-BlessicaNeural` (Microsoft Edge 무료 신경망 음성, 문장당 약 0.8초, mp3 메모리 캐시 200개). 브라우저는 `<audio>` 하나를 마이크 첫 탭에서 unlock 해 두고 재사용(iOS 자동재생 제한 우회), 재생 중 인식 일시정지, 실패 시 speechSynthesis 폴백. 방향·자동읽기 설정은 더 이상 localStorage에 저장하지 않는다.
