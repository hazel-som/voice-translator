# 음성 번역기 (muse 기반)

마이크로 말하면 문장 단위로 인식해 바로 번역해 주는 localhost 웹 앱입니다.
음성 인식은 브라우저(Web Speech API), 번역은 Meta의 Muse Code(`muse exec`)가 합니다.

## 실행

```bash
# 최초 1회: muse 로그인 (브라우저에서 코드 승인)
muse login

# 서버 시작 (의존성 설치 없음, 표준 라이브러리만 사용)
python3 voice-translator/server.py
# → http://127.0.0.1:8787/ 를 Chrome 또는 Safari로 엽니다.
```

옵션:

| 플래그 | 기본값 | 설명 |
|---|---|---|
| `--backend muse\|ollama\|echo` | `muse` | 번역 백엔드. `ollama`는 로컬 `gemma4:12B`, `echo`는 자격증명 없는 배관 테스트용 |
| `--port` | `8787` | 포트 |

환경변수: `OLLAMA_URL`, `OLLAMA_MODEL`(ollama 백엔드용), `META_API_KEY`(muse 로그인 대신).

## 사용

1. 위쪽에서 말하는 언어와 번역할 언어를 고릅니다 (기본 한국어 → Tagalog, ⇄ 버튼으로 뒤집기).
2. 🎤 버튼을 누르고 말합니다. 문장이 끝나면 자동으로 번역 행이 추가됩니다. 다시 누르면 멈춥니다.
3. 마이크 대신 아래 입력창에 타이핑해서 Enter 를 눌러도 됩니다.
4. 🔊 로 번역문을 읽어줍니다. macOS에는 타갈로그 음성이 없어서 Tagalog 방향은 비활성화됩니다 (한국어 방향은 됩니다).

## 테스트

```bash
python3 -m unittest voice-translator/test_translator.py
```

## 구조

- `server.py` — HTTP 서버. `GET /api/health`, `POST /api/translate` (NDJSON 스트림).
- `translator.py` — 프롬프트 구성, muse JSONL 파싱, muse / ollama 백엔드.
- `index.html` — 단일 페이지 UI.
- `fixtures/echo_run.jsonl` — `muse exec --json --provider echo` 실제 출력 (파서 테스트 픽스처).
