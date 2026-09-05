# 음성 번역기

마이크로 말하면 문장 단위로 인식해 바로 번역해 주는 웹 앱입니다.
음성 인식은 브라우저(Web Speech API), 번역은 Antigravity CLI(`agy -p`, Google 구독 로그인)가 기본입니다.

## 실행

```bash
# 서버 시작 (의존성 설치 없음, 표준 라이브러리만 사용)
python3 server.py --lan
```

시작하면 이런 주소가 출력됩니다. 같은 Wi-Fi의 핸드폰에서 첫 번째 주소를 엽니다.

```
  phone / other devices:  https://192.168.0.228:8787/
  this machine:           https://127.0.0.1:8787/
```

인증서가 자체 서명이라 브라우저가 한 번 경고합니다. "고급 → 계속 진행"(Chrome) 또는 "세부사항 보기 → 이 웹사이트 방문"(iOS Safari)으로 넘어가면 됩니다.
핸드폰 브라우저는 https 에서만 마이크를 허용하기 때문에 `--lan` 은 항상 HTTPS 입니다. Mac에서만 쓸 때는 `--lan` 없이 `http://127.0.0.1:8787/` 로도 됩니다.

### 외부(어디서나)에서 쓰기

```bash
brew install cloudflared          # 최초 1회
python3 server.py --lan --public
```

Cloudflare 임시 터널이 열리고 이런 줄이 출력됩니다. 이 주소를 핸드폰에 보내 열면 됩니다 (정식 인증서라 경고 없음).

```
  anywhere (share this): https://xxxx-xxxx.trycloudflare.com/?key=AbC...
```

- 누구나 접근 가능한 주소이므로 `--public` 은 항상 **접속 키**를 요구합니다. 키는 처음 연 뒤 브라우저가 기억하고, 주소창에서는 지워집니다. 키 없이 열면 "접속 키 필요"라고 뜹니다.
- 주소는 서버를 켤 때마다 바뀝니다 (계정 없는 임시 터널). 고정 주소가 필요하면 Cloudflare 계정 + 도메인으로 named tunnel 을 만들거나 ngrok 고정 도메인을 쓰면 되고, `--key` 로 키를 고정하면 됩니다.
- 번역(Antigravity 로그인)과 음성이 이 Mac에서 돌기 때문에 **Mac이 켜져 있고 서버가 실행 중이어야** 합니다.

옵션:

| 플래그 | 기본값 | 설명 |
|---|---|---|
| `--backend agy\|agy-oneshot\|muse\|ollama\|echo` | `agy` | 번역 백엔드. `agy`는 Antigravity CLI를 한 세션으로 열어 두고 문장을 흘려 넣는 방식(문장당 약 1.5초), `agy-oneshot`은 문장마다 CLI를 새로 띄우는 방식(약 7초), `muse`는 Meta Muse Code(결제 필요), `ollama`는 로컬 `gemma4:12B`, `echo`는 배관 테스트용 |
| `--lan` | 꺼짐 | 모든 인터페이스에 바인드 + 자체 서명 HTTPS (핸드폰용) |
| `--public` | 꺼짐 | Cloudflare 임시 터널로 공개 https 주소 발급 (`cloudflared` 필요), 접속 키 자동 생성 |
| `--key` | 없음 (`VT_ACCESS_KEY`) | `/api/*` 접속 키를 직접 지정. `--public` 없이도 쓸 수 있음 |
| `--port` | `8787` | 포트 |

환경변수: `AGY_MODEL`(기본 `gemini-3.7-flash-low`, `agy models` 로 목록 확인), `OLLAMA_URL`, `OLLAMA_MODEL`, `META_API_KEY`.

## 사용

1. 열면 항상 **한국어 → Tagalog** 로 시작하고 **번역 자동 읽어주기가 켜져** 있습니다. 상대가 말할 차례면 ⇄ 로 뒤집거나 드롭다운에서 언어를 고릅니다 (세션 안에서만 유지).
2. 🎤 버튼을 누르고 말합니다. 문장이 끝나면 번역 행이 추가되고 바로 소리로 읽어줍니다. 다시 누르면 멈춥니다.
3. 마이크 대신 아래 입력창에 타이핑해서 Enter 를 눌러도 됩니다.
4. 🔊 로 아무 행이나 다시 읽을 수 있습니다.

읽어주기 음성은 서버가 만듭니다(`GET /api/tts`, `uvx edge-tts` 로 Microsoft Edge 신경망 음성, 타갈로그 `fil-PH-BlessicaNeural`). Mac·iPhone에는 타갈로그 음성이 없기 때문입니다. 첫 호출 때 `uv` 가 edge-tts 패키지를 내려받습니다. 서버 TTS가 실패하면 브라우저 내장 음성으로 대체합니다. 읽어주는 동안에는 마이크를 잠시 멈춰 스피커 소리를 다시 받아 적지 않게 합니다.

## 실시간에 가깝게: 세션 방식

기본 `agy` 백엔드는 서버가 뜰 때 `agy --input-format stream-json` 프로세스를 하나 띄워 통역 지시를 넣어 두고, 문장마다 `[Korean -> Tagalog]\n문장` 한 줄만 보냅니다. CLI 기동 5초가 사라져 문장 끝에서 번역까지 1.5~2초, 음성까지 약 3초입니다. 프로세스가 죽거나 200문장을 넘기면 자동으로 다시 띄웁니다. 한 번에 한 문장씩 처리하므로 여러 문장이 겹치면 순서대로 줄을 섭니다.

## muse가 "402 Payment Required" 를 내면

`muse login` 이 성공해도 Meta Model API 계정에 결제(pay-as-you-go) 설정이 없으면 모든 모델 호출이 HTTP 402로 거부됩니다.
muse는 이를 최대 10회 지수 백오프로 재시도하므로, 이 앱은 402/401/403을 보는 즉시 호출을 끊고 행에 오류와 결제 링크를 표시합니다.
결제 설정: https://accountscenter.meta.com/muse_code/?ep=no_payg (터미널 `muse` 실행 시 Ctrl+Enter 로 여는 것과 같은 페이지).
결제 전에 앱을 써 보려면 `--backend ollama` 를 쓰세요.

## 테스트

```bash
python3 -m unittest discover -p 'test_*.py'
```

## 구조

- `server.py` — HTTP 서버. `GET /api/health`, `POST /api/translate` (NDJSON 스트림), `GET /api/tts` (mp3).
- `translator.py` — 프롬프트 구성, agy / muse / ollama 백엔드 (muse JSONL 파싱 포함).
- `index.html` — 단일 페이지 UI.
- `fixtures/echo_run.jsonl` — `muse exec --json --provider echo` 실제 출력 (파서 테스트 픽스처).
