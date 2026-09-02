# 음성 번역기

마이크로 말하면 문장 단위로 인식해 바로 번역해 주는 웹 앱입니다.
음성 인식은 브라우저(Web Speech API), 번역은 Antigravity CLI(`agy -p`, Google 구독 로그인)가 기본입니다.

## 실행

```bash
# 서버 시작 (의존성 설치 없음, 표준 라이브러리만 사용)
python3 voice-translator/server.py --lan
```

시작하면 이런 주소가 출력됩니다. 같은 Wi-Fi의 핸드폰에서 첫 번째 주소를 엽니다.

```
  phone / other devices:  https://192.168.0.228:8787/
  this machine:           https://127.0.0.1:8787/
```

인증서가 자체 서명이라 브라우저가 한 번 경고합니다. "고급 → 계속 진행"(Chrome) 또는 "세부사항 보기 → 이 웹사이트 방문"(iOS Safari)으로 넘어가면 됩니다.
핸드폰 브라우저는 https 에서만 마이크를 허용하기 때문에 `--lan` 은 항상 HTTPS 입니다. Mac에서만 쓸 때는 `--lan` 없이 `http://127.0.0.1:8787/` 로도 됩니다.

옵션:

| 플래그 | 기본값 | 설명 |
|---|---|---|
| `--backend agy\|muse\|ollama\|echo` | `agy` | 번역 백엔드. `agy`는 Antigravity CLI(구독 로그인), `muse`는 Meta Muse Code(결제 필요), `ollama`는 로컬 `gemma4:12B`, `echo`는 배관 테스트용 |
| `--lan` | 꺼짐 | 모든 인터페이스에 바인드 + 자체 서명 HTTPS (핸드폰용) |
| `--port` | `8787` | 포트 |

환경변수: `AGY_MODEL`(기본 `gemini-3.7-flash-low`, `agy models` 로 목록 확인), `OLLAMA_URL`, `OLLAMA_MODEL`, `META_API_KEY`.

## 사용

1. 위쪽에서 말하는 언어와 번역할 언어를 고릅니다 (기본 한국어 → Tagalog, ⇄ 버튼으로 뒤집기).
2. 🎤 버튼을 누르고 말합니다. 문장이 끝나면 자동으로 번역 행이 추가됩니다. 다시 누르면 멈춥니다.
3. 마이크 대신 아래 입력창에 타이핑해서 Enter 를 눌러도 됩니다.
4. 🔊 로 번역문을 읽어줍니다. macOS에는 타갈로그 음성이 없어서 Tagalog 방향은 비활성화됩니다 (한국어 방향은 됩니다).

## muse가 "402 Payment Required" 를 내면

`muse login` 이 성공해도 Meta Model API 계정에 결제(pay-as-you-go) 설정이 없으면 모든 모델 호출이 HTTP 402로 거부됩니다.
muse는 이를 최대 10회 지수 백오프로 재시도하므로, 이 앱은 402/401/403을 보는 즉시 호출을 끊고 행에 오류와 결제 링크를 표시합니다.
결제 설정: https://accountscenter.meta.com/muse_code/?ep=no_payg (터미널 `muse` 실행 시 Ctrl+Enter 로 여는 것과 같은 페이지).
결제 전에 앱을 써 보려면 `--backend ollama` 를 쓰세요.

## 테스트

```bash
python3 -m unittest discover -s voice-translator -p 'test_*.py'
```

## 구조

- `server.py` — HTTP 서버. `GET /api/health`, `POST /api/translate` (NDJSON 스트림).
- `translator.py` — 프롬프트 구성, agy / muse / ollama 백엔드 (muse JSONL 파싱 포함).
- `index.html` — 단일 페이지 UI.
- `fixtures/echo_run.jsonl` — `muse exec --json --provider echo` 실제 출력 (파서 테스트 픽스처).
