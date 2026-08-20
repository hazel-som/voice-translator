# Gemma Arena Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** gemma4:12B(Ollama)와 seda-question-bank 기존 파이프라인의 해설 번역을 동일 환경에서 생성·게이트·채점·블라인드 판정하고 localhost UI로 결과와 판정 논리를 보여주는 arena.

**Architecture:** FastAPI 서버가 seda-question-bank의 `local_agent` 모듈(프롬프트 로더·한글 마스킹·Gate B/C·Gate D)을 sys.path로 직접 재사용한다. 번역은 Ollama HTTP API, 평가는 Claude CLI 서브프로세스(파이프라인의 Gate D 방식 그대로). 결과는 `runs/` 디렉토리 JSON, UI는 빌드 스텝 없는 단일 페이지.

**Tech Stack:** Python 3.14 (uv venv), FastAPI + uvicorn, httpx, pytest, supabase-py(읽기 전용), Ollama `/api/chat`, `claude` CLI 2.x, vanilla HTML/JS/CSS.

**Spec:** `docs/superpowers/specs/2026-08-20-gemma-arena-design.md`

## Global Constraints

- 비교 언어 8개 고정: `["ne", "vi", "id", "bn", "ur", "km", "th", "tl"]`. `ru`·타밀어 금지. DB 언어 목록이 arena 목록을 결정하지 않는다.
- 번역 모델: `gemma4:12B` 단일. 다중 모델 스캐폴딩 금지 (모델 태그는 매니페스트 기록용 필드일 뿐).
- Ollama 옵션 고정: `temperature 0.3`, `num_ctx 16384`, `num_predict 8000`. 매 호출 명시.
- 시스템 프롬프트 = `render_translate_prompt(언어명, 코드)` + `_TRANSLATE_JSON_SUFFIX` (파이프라인과 바이트 동일). Ollama의 강제 JSON 모드(`format` 파라미터) 사용 금지.
- 재시도 사다리: 컴포넌트당 최대 3회(JSON 파싱 실패·placeholder 유실 시), Gate C 실패 시 피드백 주입 재번역 1회.
- Gate D judge = `claude-sonnet-4-6`, 블라인드 비교 judge = `opus`. 둘 다 Claude CLI 경유. 과거 Gate D 점수 재사용 금지 — 기존 번역도 신규 채점.
- 기준선 라벨은 항상 "DB에 저장된 기존 파이프라인 번역". 특정 Gemini 모델명을 기준선 이름으로 쓰지 않는다.
- Supabase는 **읽기 전용**. `upsert`·`insert`·`update`·`delete` 호출 금지.
- 블라인드: A/B 배정은 (문제×언어)별 무작위(run 시드 기반), 배정 기록 저장, 판정 프롬프트에 출처 단서 금지.
- 경로 상수: `QBANK_ROOT=/Users/cokoroad/seda-question-bank`, `SEDA_ROOT=/Users/cokoroad/seda`, 서버 `127.0.0.1:8765`.
- 모든 커밋 메시지 끝에 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` 추가.
- 테스트 실행: `.venv/bin/python -m pytest tests/ -v` (venv는 Task 1에서 생성).

---

### Task 1: 스캐폴드 + venv + qbank 브리지

**Files:**
- Create: `.gitignore`, `server/__init__.py`, `server/qbank.py`, `tests/__init__.py`, `tests/test_qbank.py`

**Interfaces:**
- Produces: `qbank.ARENA_LANGS: list[str]`, `qbank.bootstrap()`, `qbank.ResolvedQuestion(question_id, source)`, `qbank.tables_for(source) -> TableSet`, `qbank.resolve_by_uuid(question_id)`, `qbank.resolve_by_set(set_id, number)`, `qbank.fetch_bundle(rq) -> {"en": list, "baseline": dict}`, `qbank.list_sets()`, `qbank.list_set_questions(set_id)`, `qbank.sample_questions(limit)`, `qbank.load_persona_guide()`, `qbank.load_glossary()`

- [ ] **Step 1: venv 생성 + 의존성 설치**

```bash
cd /Users/cokoroad/translator-test
uv venv .venv --python 3.14
uv pip install --python .venv/bin/python \
  fastapi uvicorn httpx pytest \
  "supabase>=2.10.0" "rich>=13.7.0" "python-dotenv>=1.0.0" \
  "questionary>=2.0.0" "textual>=0.80"
```

supabase·rich·questionary·textual은 `local_agent` import 체인(`db_ops → ...ui → console/interactive`)이 요구한다. import 시 `ModuleNotFoundError`가 더 나오면 그 패키지(`PyJWT`, `Pillow`, `matplotlib` 후보)를 같은 방식으로 추가 설치하고 이 스텝에 기록.

- [ ] **Step 2: .gitignore 작성**

```
.venv/
runs/
__pycache__/
*.pyc
.pytest_cache/
```

- [ ] **Step 3: 실패하는 테스트 작성** — `tests/test_qbank.py`

```python
"""qbank 브리지 테스트. 네트워크 호출 없음 — db_ops 함수는 monkeypatch."""
import pytest

from server import qbank


def test_arena_langs_fixed():
    assert qbank.ARENA_LANGS == ["ne", "vi", "id", "bn", "ur", "km", "th", "tl"]
    assert "ru" not in qbank.ARENA_LANGS


def test_bootstrap_imports_local_agent_modules():
    qbank.bootstrap()
    from local_agent.generators.explanation_pipeline import (
        db_ops, gates, korean_extractor, prompt_loader, translator,
    )
    # 동일 환경 재현의 핵심 상수·함수가 존재하는지
    assert callable(prompt_loader.render_translate_prompt)
    assert "OUTPUT FORMAT REQUIREMENT" in translator._TRANSLATE_JSON_SUFFIX
    assert translator.get_language_name("ne") == "नेपाली"
    masked, slots = korean_extractor.extract("정답은 택배입니다")
    assert "⟦K0⟧" in masked and korean_extractor.restore(masked, slots) == "정답은 택배입니다"


def test_resolve_by_uuid_tries_tables_in_order(monkeypatch):
    qbank.bootstrap()
    from local_agent.generators.explanation_pipeline import db_ops

    def fake_fetch(question_id, tables=db_ops.EPT_TABLES):
        if tables.questions == "bank_ept_questions_v3":
            return {"question_id": question_id}
        raise db_ops.QuestionNotFound(question_id)

    monkeypatch.setattr(qbank.db_ops, "fetch_question", fake_fetch)
    rq = qbank.resolve_by_uuid("00000000-0000-0000-0000-000000000001")
    assert rq.source == "ept_v3"


def test_resolve_by_uuid_not_found(monkeypatch):
    qbank.bootstrap()
    from local_agent.generators.explanation_pipeline import db_ops

    def fake_fetch(question_id, tables=db_ops.EPT_TABLES):
        raise db_ops.QuestionNotFound(question_id)

    monkeypatch.setattr(qbank.db_ops, "fetch_question", fake_fetch)
    with pytest.raises(qbank.ArenaQuestionNotFound):
        qbank.resolve_by_uuid("00000000-0000-0000-0000-000000000002")


def test_fetch_bundle_filters_to_arena_langs(monkeypatch):
    qbank.bootstrap()
    fake = {
        "en": [{"id": "c1", "type": "text", "order": 1, "config": {}, "content": "x"}],
        "vi": [{"id": "c1", "type": "text", "order": 1, "config": {}, "content": "y"}],
        "ru": [{"id": "c1", "type": "text", "order": 1, "config": {}, "content": "z"}],
    }
    monkeypatch.setattr(
        qbank.db_ops, "fetch_explanations_all_langs", lambda qid, tables: fake
    )
    bundle = qbank.fetch_bundle(qbank.ResolvedQuestion("q1", "ept"))
    assert bundle["en"][0]["content"] == "x"
    assert set(bundle["baseline"].keys()) == {"vi"}  # ru 제외, en 분리
```

- [ ] **Step 4: 테스트 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_qbank.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server'` 또는 attribute 부재

- [ ] **Step 5: 구현** — `server/__init__.py`는 빈 파일. `server/qbank.py`:

```python
"""seda-question-bank 읽기 전용 브리지.

local_agent 모듈을 sys.path 주입으로 직접 재사용한다 (환경 동일성의 근거).
이 모듈은 절대 DB에 쓰지 않는다.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

QBANK_ROOT = Path("/Users/cokoroad/seda-question-bank")
SEDA_ROOT = Path("/Users/cokoroad/seda")
PERSONA_GUIDE_PATH = SEDA_ROOT / "product" / "wiki" / "번역-페르소나-가이드.md"
GLOSSARY_PATH = (
    SEDA_ROOT / "product" / "sources" / "pm-document" / "04_가이드-메뉴얼" / "번역-용어집.md"
)

ARENA_LANGS = ["ne", "vi", "id", "bn", "ur", "km", "th", "tl"]

_bootstrapped = False


def bootstrap() -> None:
    global _bootstrapped
    if _bootstrapped:
        return
    if str(QBANK_ROOT) not in sys.path:
        sys.path.insert(0, str(QBANK_ROOT))
    _bootstrapped = True


bootstrap()
from local_agent.generators.explanation_pipeline import db_ops  # noqa: E402


class ArenaQuestionNotFound(Exception):
    pass


@dataclass(frozen=True)
class ResolvedQuestion:
    question_id: str
    source: str  # "ept" | "ept_v3" | "generated"


_SOURCES: list[tuple[str, "db_ops.TableSet"]] = [
    ("ept", db_ops.EPT_TABLES),
    ("ept_v3", db_ops.EPT_V3_TABLES),
    ("generated", db_ops.GENERATED_TABLES),
]


def tables_for(source: str) -> "db_ops.TableSet":
    for name, tables in _SOURCES:
        if name == source:
            return tables
    raise ValueError(f"unknown source: {source}")


def resolve_by_uuid(question_id: str) -> ResolvedQuestion:
    """3개 테이블 자동 검색 (ept → ept_v3 → generated 순)."""
    for source, tables in _SOURCES:
        try:
            db_ops.fetch_question(question_id, tables=tables)
            return ResolvedQuestion(question_id, source)
        except db_ops.QuestionNotFound:
            continue
    raise ArenaQuestionNotFound(question_id)


def resolve_by_set(set_id: str, question_number: int) -> ResolvedQuestion:
    """세트 UUID + 문제 번호 → bank question. ept 배포분 → generated 순."""
    supabase = db_ops.get_supabase_client()
    resp = (
        supabase.table("ept_questions")
        .select("bank_id")
        .eq("question_set_id", set_id)
        .eq("question_number", question_number)
        .limit(1)
        .execute()
    )
    if resp.data:
        return ResolvedQuestion(resp.data[0]["bank_id"], "ept")
    resp = (
        supabase.table("bank_generated_questions")
        .select("question_id")
        .eq("question_set_id", set_id)
        .eq("question_number", question_number)
        .limit(1)
        .execute()
    )
    if resp.data:
        return ResolvedQuestion(resp.data[0]["question_id"], "generated")
    raise ArenaQuestionNotFound(f"{set_id}#{question_number}")


def fetch_bundle(rq: ResolvedQuestion) -> dict:
    """EN 해설 + 8개 언어 기존 번역. 기준선 라벨: 'DB에 저장된 기존 파이프라인 번역'."""
    all_langs = db_ops.fetch_explanations_all_langs(rq.question_id, tables=tables_for(rq.source))
    en = all_langs.get("en")
    baseline = {lang: comps for lang, comps in all_langs.items() if lang in ARENA_LANGS}
    return {"en": en, "baseline": baseline}


def list_sets(limit: int = 30) -> list[dict]:
    supabase = db_ops.get_supabase_client()
    out: list[dict] = []
    for table, source in (("ept_question_sets", "ept"), ("bank_generated_sets", "generated")):
        resp = (
            supabase.table(table)
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        for row in resp.data or []:
            out.append(
                {
                    "set_id": row.get("question_set_id"),
                    "source": source,
                    "set_type": row.get("set_type") or row.get("name") or "",
                    "created_at": row.get("created_at"),
                }
            )
    return out


def list_set_questions(set_id: str) -> list[dict]:
    supabase = db_ops.get_supabase_client()
    resp = (
        supabase.table("ept_questions")
        .select("question_number, bank_id")
        .eq("question_set_id", set_id)
        .order("question_number")
        .execute()
    )
    if resp.data:
        return [
            {"question_number": r["question_number"], "question_id": r["bank_id"], "source": "ept"}
            for r in resp.data
        ]
    resp = (
        supabase.table("bank_generated_questions")
        .select("question_number, question_id")
        .eq("question_set_id", set_id)
        .order("question_number")
        .execute()
    )
    return [
        {"question_number": r["question_number"], "question_id": r["question_id"], "source": "generated"}
        for r in (resp.data or [])
    ]


def sample_questions(limit: int = 10) -> list[dict]:
    """EN 해설이 존재하는 최근 문제 (UI 빠른 선택용)."""
    supabase = db_ops.get_supabase_client()
    out: list[dict] = []
    for source, tables in _SOURCES:
        resp = (
            supabase.table(tables.explanations)
            .select("question_id, updated_at")
            .eq("language_short_code", "en")
            .order("updated_at", desc=True)
            .limit(limit)
            .execute()
        )
        for row in resp.data or []:
            out.append(
                {"question_id": row["question_id"], "source": source, "updated_at": row.get("updated_at")}
            )
    return out


def load_persona_guide() -> str:
    return PERSONA_GUIDE_PATH.read_text(encoding="utf-8")


def load_glossary(max_chars: int = 12000) -> str:
    text = GLOSSARY_PATH.read_text(encoding="utf-8")
    if len(text) > max_chars:
        return text[:max_chars] + "\n\n(용어집 일부 — 길이 제한으로 절단됨)"
    return text
```

- [ ] **Step 6: 테스트 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_qbank.py -v`
Expected: PASS 5개. import 에러가 나면 Step 1의 후보 패키지를 추가 설치 후 재실행.

- [ ] **Step 7: Commit**

```bash
git add .gitignore server/ tests/
git commit -m "feat: 프로젝트 스캐폴드 + seda-question-bank 읽기 전용 브리지"
```

---

### Task 2: store.py — 실행 결과 저장소

**Files:**
- Create: `server/store.py`, `tests/test_store.py`

**Interfaces:**
- Produces: `RunStore(root: Path)` 클래스 — `.create_run(payload: dict, langs: list[str]) -> str`, `.path(run_id) -> Path`, `.write_json(run_id, relpath: str, obj)`, `.read_json(run_id, relpath, default=None)`, `.update_status(run_id, **fields)`, `.append_log(run_id, message: str)`, `.list_runs() -> list[dict]`, `.run_detail(run_id) -> dict`
- 디렉토리 구조 (spec §3): `runs/<run_id>/{manifest.json, input.json, status.json, baseline/<lang>.json, gemma/<lang>.json, eval/<lang>.json, summary.json}`

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_store.py`

```python
from pathlib import Path

from server.store import RunStore


def _mk(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / "runs")


def test_create_run_writes_input_and_status(tmp_path):
    store = _mk(tmp_path)
    run_id = store.create_run({"question_id": "q1"}, ["vi", "ne"])
    assert (store.path(run_id) / "input.json").exists()
    status = store.read_json(run_id, "status.json")
    assert status["state"] == "created"
    assert status["languages"] == ["vi", "ne"]


def test_write_read_json_nested(tmp_path):
    store = _mk(tmp_path)
    run_id = store.create_run({}, ["vi"])
    store.write_json(run_id, "gemma/vi.json", {"components": [1]})
    assert store.read_json(run_id, "gemma/vi.json")["components"] == [1]
    assert store.read_json(run_id, "gemma/none.json", default={}) == {}


def test_update_status_merges_and_logs(tmp_path):
    store = _mk(tmp_path)
    run_id = store.create_run({}, ["vi"])
    store.update_status(run_id, state="translating")
    store.append_log(run_id, "vi 번역 시작")
    status = store.read_json(run_id, "status.json")
    assert status["state"] == "translating"
    assert status["languages"] == ["vi"]  # 기존 필드 보존
    assert "vi 번역 시작" in status["log"][-1]


def test_list_runs_newest_first(tmp_path):
    store = _mk(tmp_path)
    a = store.create_run({}, ["vi"])
    b = store.create_run({}, ["vi"])
    runs = store.list_runs()
    assert [r["run_id"] for r in runs][:2] == [b, a]


def test_run_detail_aggregates(tmp_path):
    store = _mk(tmp_path)
    run_id = store.create_run({"question_id": "q1"}, ["vi"])
    store.write_json(run_id, "baseline/vi.json", {"components": []})
    store.write_json(run_id, "eval/vi.json", {"verdict": {"winner": "tie"}})
    store.write_json(run_id, "summary.json", {"by_language": {}})
    detail = store.run_detail(run_id)
    assert detail["run_id"] == run_id
    assert detail["input"]["question_id"] == "q1"
    assert detail["baseline"]["vi"] == {"components": []}
    assert detail["eval"]["vi"]["verdict"]["winner"] == "tie"
    assert detail["summary"] == {"by_language": {}}
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_store.py -v`
Expected: FAIL — `No module named 'server.store'`

- [ ] **Step 3: 구현** — `server/store.py`

```python
"""runs/ 디렉토리 JSON 저장소. 원자적 쓰기(tmp+rename)."""
from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import datetime
from pathlib import Path

_SUBDIRS = ("baseline", "gemma", "eval")


class RunStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def create_run(self, payload: dict, langs: list[str]) -> str:
        run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
        run_dir = self.root / run_id
        for sub in _SUBDIRS:
            (run_dir / sub).mkdir(parents=True, exist_ok=True)
        self.write_json(run_id, "input.json", payload)
        self.write_json(
            run_id,
            "status.json",
            {
                "state": "created",
                "languages": langs,
                "created_at": datetime.now().isoformat(),
                "log": [],
            },
        )
        return run_id

    def path(self, run_id: str) -> Path:
        return self.root / run_id

    def write_json(self, run_id: str, relpath: str, obj) -> None:
        target = self.path(run_id) / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)

    def read_json(self, run_id: str, relpath: str, default=None):
        target = self.path(run_id) / relpath
        if not target.exists():
            return default
        return json.loads(target.read_text(encoding="utf-8"))

    def update_status(self, run_id: str, **fields) -> dict:
        with self._lock:
            status = self.read_json(run_id, "status.json", default={}) or {}
            status.update(fields)
            status["updated_at"] = datetime.now().isoformat()
            self.write_json(run_id, "status.json", status)
            return status

    def append_log(self, run_id: str, message: str) -> None:
        with self._lock:
            status = self.read_json(run_id, "status.json", default={}) or {}
            log = status.setdefault("log", [])
            log.append(f"{datetime.now().strftime('%H:%M:%S')} {message}")
            status["updated_at"] = datetime.now().isoformat()
            self.write_json(run_id, "status.json", status)

    def list_runs(self) -> list[dict]:
        out: list[dict] = []
        for run_dir in sorted(self.root.iterdir(), reverse=True):
            if not run_dir.is_dir():
                continue
            run_id = run_dir.name
            status = self.read_json(run_id, "status.json", default={}) or {}
            payload = self.read_json(run_id, "input.json", default={}) or {}
            out.append(
                {
                    "run_id": run_id,
                    "state": status.get("state"),
                    "languages": status.get("languages", []),
                    "created_at": status.get("created_at"),
                    "input": payload,
                }
            )
        return out

    def _read_dir(self, run_id: str, sub: str) -> dict:
        result: dict = {}
        d = self.path(run_id) / sub
        if d.is_dir():
            for f in sorted(d.glob("*.json")):
                result[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        return result

    def run_detail(self, run_id: str) -> dict:
        return {
            "run_id": run_id,
            "input": self.read_json(run_id, "input.json", default={}),
            "status": self.read_json(run_id, "status.json", default={}),
            "manifest": self.read_json(run_id, "manifest.json", default={}),
            "en": self.read_json(run_id, "en.json", default=None),
            "baseline": self._read_dir(run_id, "baseline"),
            "gemma": self._read_dir(run_id, "gemma"),
            "eval": self._read_dir(run_id, "eval"),
            "summary": self.read_json(run_id, "summary.json", default=None),
        }
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_store.py -v`
Expected: PASS 5개

- [ ] **Step 5: Commit**

```bash
git add server/store.py tests/test_store.py
git commit -m "feat: RunStore — 실행 결과 JSON 저장소"
```

---

### Task 3: verdict.py — 승패 판정 논리 (spec §7)

**Files:**
- Create: `server/verdict.py`, `tests/test_verdict.py`

**Interfaces:**
- Consumes: 없음 (순수 함수)
- Produces:
  - `cell_verdict(gates_result: dict, gate_d_baseline: dict|None, gate_d_gemma: dict|None, blind: dict|None) -> dict` — 반환 키: `winner`("baseline"|"gemma"|"tie"|"unknown"), `path`("gate_fail"|"blind"|"blind_conflict"|"no_verdict"), `logic`(한국어 설명문), `gate_d_delta`(float|None)
  - `language_summary(cells: list[dict]) -> dict` — `wins`/`ties`/`losses`/`unknown`, `gate_fail_count`, `n`, `avg_gate_d_baseline`, `avg_gate_d_gemma`, `avg_delta`, `avg_duration_sec`
  - `deployable_verdict(lang: str, summary: dict) -> dict` — `deployable: bool`, `conditions: list[{name, passed, actual, required}]`
  - `GATE_D_THRESHOLDS = {"km": 8.0, "bn": 8.0, "ur": 8.0}`, `DEFAULT_GATE_D_THRESHOLD = 8.5` (파이프라인 gate_d.py와 동일 값)
- gates_result 형식은 Task 5의 `run_gates` 반환값, gate_d_* 형식은 Task 6의 `gate_d_score` 반환값(`{"score": float, "passed": bool, ...}`), blind 형식은 Task 6의 `blind_compare` 반환값(`{"winner_side": "baseline"|"gemma"|"tie"|"unknown", ...}`). cells는 eval/<lang>.json의 `{"verdict": ..., "duration_sec": ...}` 병합 dict.

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_verdict.py`

```python
from server import verdict


def _gates(passed=True, failed=None):
    return {"passed": passed, "gate_c": {"failed_checks": failed or []}}


def _gd(score):
    return {"score": score, "passed": score >= 8.5}


def _blind(winner_side):
    return {"winner_side": winner_side}


def test_gate_fail_is_automatic_baseline_win():
    v = verdict.cell_verdict(_gates(False, ["korean_pollution"]), _gd(9.0), _gd(9.5), _blind("gemma"))
    assert v["winner"] == "baseline"
    assert v["path"] == "gate_fail"
    assert "구조 게이트" in v["logic"]


def test_blind_winner_adopted_when_gates_pass():
    v = verdict.cell_verdict(_gates(True), _gd(8.0), _gd(9.0), _blind("gemma"))
    assert v["winner"] == "gemma"
    assert v["path"] == "blind"
    assert v["gate_d_delta"] == 1.0


def test_conflict_flag_when_gate_d_contradicts_blind():
    # 블라인드는 gemma 승인데 Gate D는 gemma가 1.0점 이상 낮음 → 상충 플래그
    v = verdict.cell_verdict(_gates(True), _gd(9.5), _gd(8.0), _blind("gemma"))
    assert v["winner"] == "gemma"  # 블라인드 판정을 따르되
    assert v["path"] == "blind_conflict"
    assert "상충" in v["logic"]


def test_small_delta_is_not_conflict():
    v = verdict.cell_verdict(_gates(True), _gd(8.6), _gd(8.4), _blind("gemma"))
    assert v["path"] == "blind"


def test_tie_and_unknown():
    assert verdict.cell_verdict(_gates(True), _gd(8), _gd(8), _blind("tie"))["winner"] == "tie"
    v = verdict.cell_verdict(_gates(True), _gd(8), _gd(8), None)
    assert v["winner"] == "unknown" and v["path"] == "no_verdict"


def test_language_summary_counts():
    cells = [
        {"verdict": {"winner": "gemma", "path": "blind", "gate_d_delta": 1.0},
         "gate_d": {"baseline": _gd(8.0), "gemma": _gd(9.0)},
         "gates": _gates(True), "duration_sec": 10.0},
        {"verdict": {"winner": "baseline", "path": "gate_fail", "gate_d_delta": None},
         "gate_d": {"baseline": _gd(9.0), "gemma": _gd(5.0)},
         "gates": _gates(False, ["markers"]), "duration_sec": 20.0},
    ]
    s = verdict.language_summary(cells)
    assert s["wins"] == 1 and s["losses"] == 1 and s["n"] == 2
    assert s["gate_fail_count"] == 1
    assert s["avg_gate_d_gemma"] == 7.0
    assert s["avg_duration_sec"] == 15.0


def test_deployable_all_conditions():
    good = {"wins": 2, "ties": 1, "losses": 1, "unknown": 0, "n": 4,
            "gate_fail_count": 0, "avg_gate_d_gemma": 8.7}
    d = verdict.deployable_verdict("vi", good)
    assert d["deployable"] is True
    assert len(d["conditions"]) == 3

    bad_gate = dict(good, gate_fail_count=1)
    assert verdict.deployable_verdict("vi", bad_gate)["deployable"] is False

    # km은 임계 8.0 — 8.1이면 통과
    km = dict(good, avg_gate_d_gemma=8.1)
    assert verdict.deployable_verdict("km", km)["deployable"] is True
    assert verdict.deployable_verdict("vi", km)["deployable"] is False

    bad_blind = dict(good, wins=0, ties=1, losses=3)
    assert verdict.deployable_verdict("vi", bad_blind)["deployable"] is False
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_verdict.py -v`
Expected: FAIL — `No module named 'server.verdict'`

- [ ] **Step 3: 구현** — `server/verdict.py`

```python
"""승패 판정 논리 (spec §7). 순수 함수 — UI에 노출되는 규칙 그 자체.

규칙:
1. gemma가 재시도 후에도 Gate B/C 실패 → 기존 파이프라인 자동 승 (path=gate_fail)
2. 게이트 통과 → 블라인드 판정 winner 채택 (path=blind)
3. 블라인드 판정과 Gate D 점수 우열이 강하게 상충(|Δ| >= 1.0, 방향 반대) → path=blind_conflict,
   블라인드 판정을 따르되 상충 사실을 명시
"""
from __future__ import annotations

GATE_D_THRESHOLDS = {"km": 8.0, "bn": 8.0, "ur": 8.0}
DEFAULT_GATE_D_THRESHOLD = 8.5
CONFLICT_DELTA = 1.0


def gate_d_threshold(lang: str) -> float:
    return GATE_D_THRESHOLDS.get(lang, DEFAULT_GATE_D_THRESHOLD)


def cell_verdict(
    gates_result: dict,
    gate_d_baseline: dict | None,
    gate_d_gemma: dict | None,
    blind: dict | None,
) -> dict:
    delta = None
    if gate_d_baseline and gate_d_gemma:
        delta = round(gate_d_gemma["score"] - gate_d_baseline["score"], 2)

    if not gates_result.get("passed", False):
        failed = gates_result.get("gate_c", {}).get("failed_checks", [])
        return {
            "winner": "baseline",
            "path": "gate_fail",
            "gate_d_delta": delta,
            "logic": (
                "gemma 번역이 재시도 후에도 구조 게이트(Gate B/C)를 통과하지 못해 "
                f"실전 투입 불가로 판정, 기존 파이프라인 자동 승. 실패 검사: {failed}. "
                "Gate D·블라인드 결과는 진단용으로만 표시됩니다."
            ),
        }

    if not blind or blind.get("winner_side") not in ("baseline", "gemma", "tie"):
        return {
            "winner": "unknown",
            "path": "no_verdict",
            "gate_d_delta": delta,
            "logic": "블라인드 비교 판정을 얻지 못해 승패 미확정 (판정 재실행 가능).",
        }

    winner = blind["winner_side"]
    conflict = False
    if delta is not None and winner != "tie" and abs(delta) >= CONFLICT_DELTA:
        gate_d_favors = "gemma" if delta > 0 else "baseline"
        conflict = gate_d_favors != winner

    if conflict:
        return {
            "winner": winner,
            "path": "blind_conflict",
            "gate_d_delta": delta,
            "logic": (
                f"블라인드 비교 판정({winner} 승)을 채택하되, Gate D 점수 차(Δ={delta:+.2f})는 "
                "반대 방향 — 두 측정이 상충하므로 플래그 표시. 블라인드 판정이 우선하는 이유: "
                "양 번역을 직접 대조한 평가이기 때문."
            ),
        }
    return {
        "winner": winner,
        "path": "blind",
        "gate_d_delta": delta,
        "logic": (
            f"구조 게이트 통과 → 블라인드 비교 판정 채택: {winner}. "
            + (f"Gate D 점수 차 Δ={delta:+.2f} (보조 지표, 방향 일치)." if delta is not None else "")
        ),
    }


def language_summary(cells: list[dict]) -> dict:
    n = len(cells)
    wins = sum(1 for c in cells if c["verdict"]["winner"] == "gemma")
    losses = sum(1 for c in cells if c["verdict"]["winner"] == "baseline")
    ties = sum(1 for c in cells if c["verdict"]["winner"] == "tie")
    unknown = n - wins - losses - ties
    gate_fail = sum(1 for c in cells if not c.get("gates", {}).get("passed", True))

    def _avg(values: list[float]) -> float | None:
        vals = [v for v in values if v is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    return {
        "n": n,
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "unknown": unknown,
        "gate_fail_count": gate_fail,
        "avg_gate_d_baseline": _avg(
            [c.get("gate_d", {}).get("baseline", {}).get("score") for c in cells]
        ),
        "avg_gate_d_gemma": _avg(
            [c.get("gate_d", {}).get("gemma", {}).get("score") for c in cells]
        ),
        "avg_delta": _avg([c["verdict"].get("gate_d_delta") for c in cells]),
        "avg_duration_sec": _avg([c.get("duration_sec") for c in cells]),
    }


def deployable_verdict(lang: str, summary: dict) -> dict:
    threshold = gate_d_threshold(lang)
    decided = summary["wins"] + summary["ties"] + summary["losses"]
    blind_ratio = (summary["wins"] + summary["ties"]) / decided if decided else 0.0
    avg_gemma = summary.get("avg_gate_d_gemma")

    conditions = [
        {
            "name": "게이트 실패율 0%",
            "passed": summary["gate_fail_count"] == 0,
            "actual": f"{summary['gate_fail_count']}/{summary['n']} 실패",
            "required": "0건",
        },
        {
            "name": f"gemma Gate D 평균 ≥ {threshold} (파이프라인 임계값)",
            "passed": avg_gemma is not None and avg_gemma >= threshold,
            "actual": f"평균 {avg_gemma}",
            "required": f"≥ {threshold}",
        },
        {
            "name": "블라인드 승+무 비율 ≥ 50%",
            "passed": decided > 0 and blind_ratio >= 0.5,
            "actual": f"{blind_ratio:.0%} ({summary['wins']}승 {summary['ties']}무 {summary['losses']}패)",
            "required": "≥ 50%",
        },
    ]
    return {
        "lang": lang,
        "deployable": all(c["passed"] for c in conditions),
        "conditions": conditions,
    }
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_verdict.py -v`
Expected: PASS 7개

- [ ] **Step 5: Commit**

```bash
git add server/verdict.py tests/test_verdict.py
git commit -m "feat: 승패 판정 논리 — 게이트 자동 패배·블라인드 채택·상충 플래그·deployable 3조건"
```

---

### Task 4: gemma.py — Ollama 번역기 (동일 환경 재현)

**Files:**
- Create: `server/gemma.py`, `tests/test_gemma.py`

**Interfaces:**
- Consumes: `qbank.bootstrap()` 후 `local_agent...prompt_loader.render_translate_prompt`, `translator._TRANSLATE_JSON_SUFFIX`, `translator.get_language_name`, `korean_extractor.extract/restore/missing_slots`
- Produces:
  - `OLLAMA_BASE = "http://127.0.0.1:11434"`, `MODEL_TAG = "gemma4:12B"`, `OPTIONS = {"temperature": 0.3, "num_ctx": 16384, "num_predict": 8000}`, `MAX_ATTEMPTS = 3`, `COMPONENT_TIMEOUT_SEC = 600`
  - `build_system_prompt(lang_code: str, feedback_prefix: str = "") -> str`
  - `parse_translated_text(content: str) -> str | None` — ```json 펜스 제거, 첫 `{`~마지막 `}` JSON 파싱, `translated_text` 반환
  - `translate_component(comp: dict, lang_code: str, system_prompt: str) -> tuple[dict, dict]` — (번역 컴포넌트, meta `{component_id, attempts, duration_sec, prompt_tokens, output_tokens, lost_placeholders}`)
  - `translate_language(en_components: list[dict], lang_code: str, feedback_prefix: str = "") -> tuple[list[dict], list[dict]]`
  - `ollama_health() -> dict`, `model_info() -> dict` (`{tag, digest, ollama_version}`)
  - `_chat(system_prompt: str, user_prompt: str) -> dict` — 테스트에서 monkeypatch하는 유일한 네트워크 경계

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_gemma.py`

```python
import json

from server import gemma


EN_COMP = {
    "id": "correct_answer", "type": "text", "order": 1, "config": {},
    "content": "### ✅ CORRECT ANSWER\n\n**택배입니다.**\n\nThis is a delivery package.",
}


def test_options_match_spec():
    assert gemma.OPTIONS == {"temperature": 0.3, "num_ctx": 16384, "num_predict": 8000}
    assert gemma.MODEL_TAG == "gemma4:12B"


def test_system_prompt_is_pipeline_identical():
    sp = gemma.build_system_prompt("ne")
    assert "नेपाली" in sp                          # {{TARGET_LANGUAGE}} 치환
    assert "OUTPUT FORMAT REQUIREMENT" in sp        # _TRANSLATE_JSON_SUFFIX 포함
    assert "⟦K…⟧" in sp
    fb = gemma.build_system_prompt("ne", feedback_prefix="FIX THIS")
    assert fb.startswith("FIX THIS")


def test_parse_translated_text_variants():
    assert gemma.parse_translated_text('{"translated_text": "hola"}') == "hola"
    assert gemma.parse_translated_text('```json\n{"translated_text": "hola"}\n```') == "hola"
    assert gemma.parse_translated_text('prefix {"translated_text": "hola"} suffix') == "hola"
    assert gemma.parse_translated_text("no json here") is None


def test_translate_component_masks_and_restores(monkeypatch):
    captured = {}

    def fake_chat(system_prompt, user_prompt):
        captured["user"] = json.loads(user_prompt)
        masked = captured["user"]["text"]
        assert "택배" not in masked and "⟦K0⟧" in masked   # 한글은 마스킹되어 전달
        return {
            "message": {"content": json.dumps({"translated_text": masked.replace(
                "This is a delivery package.", "यो डेलिभरी प्याकेज हो।")})},
            "prompt_eval_count": 100, "eval_count": 50,
        }

    monkeypatch.setattr(gemma, "_chat", fake_chat)
    comp, meta = gemma.translate_component(EN_COMP, "ne", gemma.build_system_prompt("ne"))
    assert "택배입니다" in comp["content"]               # 복원됨
    assert "यो डेलिभरी" in comp["content"]
    assert comp["id"] == "correct_answer" and comp["order"] == 1
    assert meta["attempts"] == 1 and meta["output_tokens"] == 50
    assert captured["user"]["language_code"] == "ne"
    assert captured["user"]["target_language"] == "नेपाली"


def test_translate_component_retries_on_placeholder_loss(monkeypatch):
    calls = {"n": 0}

    def fake_chat(system_prompt, user_prompt):
        calls["n"] += 1
        text = json.loads(user_prompt)["text"]
        if calls["n"] == 1:
            return {"message": {"content": json.dumps(
                {"translated_text": text.replace("⟦K0⟧", "")})}}  # placeholder 유실
        return {"message": {"content": json.dumps({"translated_text": text})}}

    monkeypatch.setattr(gemma, "_chat", fake_chat)
    comp, meta = gemma.translate_component(EN_COMP, "ne", "sp")
    assert meta["attempts"] == 2
    assert "택배입니다" in comp["content"]


def test_translate_component_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr(gemma, "_chat", lambda s, u: {"message": {"content": "garbage"}})
    comp, meta = gemma.translate_component(EN_COMP, "ne", "sp")
    assert meta["attempts"] == gemma.MAX_ATTEMPTS
    assert meta["failed"] is True
    assert comp["content"] == EN_COMP["content"]  # 원문 유지 (파이프라인의 masked 폴백과 동등)


def test_non_text_component_passthrough(monkeypatch):
    monkeypatch.setattr(gemma, "_chat", lambda s, u: (_ for _ in ()).throw(AssertionError))
    img = {"id": "img1", "type": "image", "order": 2, "config": {}, "content": {"url": "x.png"}}
    comp, meta = gemma.translate_component(img, "ne", "sp")
    assert comp == img and meta["attempts"] == 0
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_gemma.py -v`
Expected: FAIL — `No module named 'server.gemma'`

- [ ] **Step 3: 구현** — `server/gemma.py`

```python
"""Ollama gemma4:12B 번역기 — 파이프라인 환경 재현.

동일: TRANSLATE.md + _TRANSLATE_JSON_SUFFIX, 유저 프롬프트 JSON, 한글 마스킹,
      컴포넌트 단위, 3회 재시도.
편차(매니페스트 기록): 컴포넌트 타임아웃 600s(로컬 추론 속도 대응),
      temperature 0.3/num_predict 8000은 Vertex 폴백 파라미터 미러링.
"""
from __future__ import annotations

import json
import re
import time

import httpx

from . import qbank

qbank.bootstrap()
from local_agent.generators.explanation_pipeline import korean_extractor  # noqa: E402
from local_agent.generators.explanation_pipeline import prompt_loader  # noqa: E402
from local_agent.generators.explanation_pipeline import translator as qb_translator  # noqa: E402

OLLAMA_BASE = "http://127.0.0.1:11434"
MODEL_TAG = "gemma4:12B"
OPTIONS = {"temperature": 0.3, "num_ctx": 16384, "num_predict": 8000}
MAX_ATTEMPTS = 3
COMPONENT_TIMEOUT_SEC = 600
KEEP_ALIVE = "30m"

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def build_system_prompt(lang_code: str, feedback_prefix: str = "") -> str:
    lang_name = qb_translator.get_language_name(lang_code)
    base = (
        prompt_loader.render_translate_prompt(lang_name, lang_code)
        + qb_translator._TRANSLATE_JSON_SUFFIX
    )
    if feedback_prefix:
        return feedback_prefix + "\n\n" + base
    return base


def _chat(system_prompt: str, user_prompt: str) -> dict:
    """Ollama /api/chat 호출. 테스트에서 monkeypatch하는 네트워크 경계."""
    resp = httpx.post(
        f"{OLLAMA_BASE}/api/chat",
        json={
            "model": MODEL_TAG,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "options": OPTIONS,
            "keep_alive": KEEP_ALIVE,
        },
        timeout=httpx.Timeout(COMPONENT_TIMEOUT_SEC, connect=10),
    )
    resp.raise_for_status()
    return resp.json()


def parse_translated_text(content: str) -> str | None:
    m = _FENCE_RE.search(content)
    if m:
        content = m.group(1)
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return None
    text = obj.get("translated_text")
    return text if isinstance(text, str) and text else None


def translate_component(comp: dict, lang_code: str, system_prompt: str) -> tuple[dict, dict]:
    if comp.get("type") != "text" or not comp.get("content"):
        return dict(comp), {"component_id": comp.get("id"), "attempts": 0, "duration_sec": 0.0}

    masked, slots = korean_extractor.extract(comp["content"])
    user_prompt = json.dumps(
        {
            "target_language": qb_translator.get_language_name(lang_code),
            "language_code": lang_code,
            "text": masked,
        },
        ensure_ascii=False,
    )

    started = time.monotonic()
    meta: dict = {"component_id": comp.get("id"), "prompt_tokens": None, "output_tokens": None}
    translated: str | None = None
    lost: list[str] = []
    attempts = 0
    for attempts in range(1, MAX_ATTEMPTS + 1):
        raw = _chat(system_prompt, user_prompt)
        meta["prompt_tokens"] = raw.get("prompt_eval_count")
        meta["output_tokens"] = raw.get("eval_count")
        candidate = parse_translated_text(raw.get("message", {}).get("content", ""))
        if candidate is None:
            continue
        lost = korean_extractor.missing_slots(candidate, slots)
        if lost:
            continue
        translated = candidate
        break

    meta["attempts"] = attempts
    meta["duration_sec"] = round(time.monotonic() - started, 1)
    meta["lost_placeholders"] = lost
    if translated is None:
        meta["failed"] = True
        content = comp["content"]  # 파이프라인의 masked-원문 폴백과 동등하게 원문 유지
    else:
        meta["failed"] = False
        content = korean_extractor.restore(translated, slots)

    return (
        {
            "id": comp.get("id", ""),
            "type": "text",
            "order": comp.get("order", 1),
            "config": comp.get("config", {}),
            "content": content,
        },
        meta,
    )


def translate_language(
    en_components: list[dict], lang_code: str, feedback_prefix: str = ""
) -> tuple[list[dict], list[dict]]:
    system_prompt = build_system_prompt(lang_code, feedback_prefix)
    components: list[dict] = []
    metas: list[dict] = []
    for comp in en_components:
        translated, meta = translate_component(comp, lang_code, system_prompt)
        components.append(translated)
        metas.append(meta)
    return components, metas


def ollama_health() -> dict:
    try:
        resp = httpx.get(f"{OLLAMA_BASE}/api/tags", timeout=5)
        resp.raise_for_status()
        models = [m.get("name") for m in resp.json().get("models", [])]
        return {"ok": MODEL_TAG in models, "models": models}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def model_info() -> dict:
    info: dict = {"tag": MODEL_TAG, "digest": None, "ollama_version": None}
    try:
        tags = httpx.get(f"{OLLAMA_BASE}/api/tags", timeout=5).json()
        for m in tags.get("models", []):
            if m.get("name") == MODEL_TAG:
                info["digest"] = m.get("digest")
        info["ollama_version"] = httpx.get(f"{OLLAMA_BASE}/api/version", timeout=5).json().get("version")
    except Exception as exc:  # noqa: BLE001
        info["error"] = str(exc)
    return info
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_gemma.py -v`
Expected: PASS 7개

- [ ] **Step 5: Commit**

```bash
git add server/gemma.py tests/test_gemma.py
git commit -m "feat: Ollama gemma 번역기 — 파이프라인 프롬프트·마스킹·재시도 동일 재현"
```

---

### Task 5: arena_gates.py — Gate B/C 적용 + 피드백 빌더

**Files:**
- Create: `server/arena_gates.py`, `tests/test_arena_gates.py`

**Interfaces:**
- Consumes: `local_agent...gates.gate_b_structure`, `gates.gate_c_for_language`
- Produces:
  - `run_gates(en_components, lang_components, lang) -> dict` — `{"passed": bool, "gate_b": {"passed": bool, "detail": dict}, "gate_c": {"passed": bool, "failed_checks": list[str], "results": list[{"name","passed","detail"}]}}`
  - `build_gate_c_feedback(failed_checks: list[str]) -> str` — 파이프라인 `retry_failed_gate_c`의 피드백 문구와 동일

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_arena_gates.py`

```python
from server import arena_gates

EN = [{"id": "c1", "type": "text", "order": 1, "config": {},
       "content": "### ✅ CORRECT\n\n**택배입니다.** ✅ ok ❌ no"}]
GOOD = [{"id": "c1", "type": "text", "order": 1, "config": {},
         "content": "### ✅ सही उत्तर\n\n**택배입니다.** ✅ ठीक ❌ होइन"}]
POLLUTED = [{"id": "c1", "type": "text", "order": 1, "config": {},
             "content": "### ✅ सही उत्तर\n\n**소포** ✅ ठीक ❌ होइन"}]  # EN에 없는 한글
BAD_STRUCT = [{"id": "cX", "type": "text", "order": 1, "config": {}, "content": "x ✅ ❌"}]


def test_all_pass():
    r = arena_gates.run_gates(EN, GOOD, "ne")
    assert r["passed"] is True
    assert r["gate_b"]["passed"] is True
    assert r["gate_c"]["failed_checks"] == []
    assert len(r["gate_c"]["results"]) == 5


def test_korean_pollution_fails():
    r = arena_gates.run_gates(EN, POLLUTED, "ne")
    assert r["passed"] is False
    assert "korean_pollution" in r["gate_c"]["failed_checks"]


def test_gate_b_structure_mismatch_fails():
    r = arena_gates.run_gates(EN, BAD_STRUCT, "ne")
    assert r["gate_b"]["passed"] is False
    assert r["passed"] is False


def test_feedback_text_mirrors_pipeline():
    fb = arena_gates.build_gate_c_feedback(["korean_pollution", "markers"])
    assert fb.startswith("PREVIOUS TRANSLATION FAILED THESE QUALITY CHECKS:")
    assert "(1) Korean text was modified or extra Korean inserted." in fb
    assert "(4) ✅/❌ marker counts differ from source. Preserve EXACT marker count." in fb
    assert fb.endswith("Re-translate carefully, fixing all of the above.")
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_arena_gates.py -v`
Expected: FAIL — `No module named 'server.arena_gates'`

- [ ] **Step 3: 구현** — `server/arena_gates.py`

```python
"""파이프라인 Gate B/C를 gemma 산출물에 동일 적용."""
from __future__ import annotations

from . import qbank

qbank.bootstrap()
from local_agent.generators.explanation_pipeline import gates  # noqa: E402


def run_gates(en_components: list[dict], lang_components: list[dict], lang: str) -> dict:
    b = gates.gate_b_structure(en_components, {lang: lang_components})
    b_entry = b.by_language[lang]
    c_results = gates.gate_c_for_language(en_components, lang_components, lang)
    failed = [r.name for r in c_results if not r.passed]
    return {
        "passed": b.all_match and not failed,
        "gate_b": {"passed": b.all_match, "detail": b_entry},
        "gate_c": {
            "passed": not failed,
            "failed_checks": failed,
            "results": [
                {"name": r.name, "passed": r.passed, "detail": r.detail} for r in c_results
            ],
        },
    }


# 파이프라인 retry_failed_gate_c와 동일 문구 (translator.py:524-553)
_FEEDBACK_DETAILS = {
    "korean_pollution": "(1) Korean text was modified or extra Korean inserted.",
    "component_ids": "(2) Some component IDs were missing or extra. Keep IDs identical to source.",
    "english_headers": "(3) English section headers (### CORRECT/WRONG/etc) were left untranslated.",
    "markers": "(4) ✅/❌ marker counts differ from source. Preserve EXACT marker count.",
    "bracket_balance": "(5) Bracket pairs were unbalanced. Match every opening with a closing.",
}


def build_gate_c_feedback(failed_checks: list[str]) -> str:
    parts = [
        f"PREVIOUS TRANSLATION FAILED THESE QUALITY CHECKS: {failed_checks}.",
        "Specifically:",
    ]
    for check, detail in _FEEDBACK_DETAILS.items():
        if check in failed_checks:
            parts.append(detail)
    parts.append("Re-translate carefully, fixing all of the above.")
    return " ".join(parts)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_arena_gates.py -v`
Expected: PASS 4개

- [ ] **Step 5: Commit**

```bash
git add server/arena_gates.py tests/test_arena_gates.py
git commit -m "feat: Gate B/C 재사용 래퍼 + 파이프라인 동일 피드백 빌더"
```

---

### Task 6: judge.py + 블라인드 비교 프롬프트

**Files:**
- Create: `server/judge.py`, `prompts/compare_judge.md`, `tests/test_judge.py`

**Interfaces:**
- Consumes: `qbank.load_persona_guide/load_glossary`, `local_agent...gate_d.run_gate_d/load_gate_d_prompt`, `local_agent.core.claude_cli.call_claude`
- Produces:
  - `GATE_D_MODEL = "claude-sonnet-4-6"`, `BLIND_MODEL = "opus"`
  - `gate_d_score(en_components, lang_components, lang) -> dict` — `{"score": float, "passed": bool, "threshold": float, "issues": list, "notes": str}`
  - `blind_compare(en_components, baseline_comps, gemma_comps, lang, rng: random.Random) -> dict` — `{"assignment": {"A": side, "B": side}, "winner_side": "baseline"|"gemma"|"tie"|"unknown", "raw": dict|None, "error": str|None}`
  - `build_compare_system_prompt(lang: str) -> str`, `components_text(comps) -> str`

- [ ] **Step 1: 블라인드 비교 프롬프트 작성** — `prompts/compare_judge.md` (전문)

````markdown
# 해설 번역 블라인드 비교 심사

당신은 EPS-TOPIK(한국 고용허가제 한국어능력시험) 학습 앱의 다국어 로컬라이제이션 심사위원입니다.
동일한 영어 해설(EN)을 {{LANGUAGE_NAME}}({{LANGUAGE_CODE}})로 번역한 두 결과물 A와 B를 비교 평가합니다.
A와 B가 각각 어떤 시스템의 산출물인지 당신은 알 수 없으며, 추측해서도 안 됩니다. 오직 품질만 평가하십시오.

## 평가 대상 독자
- EPS-TOPIK 응시생 (20대 초~30대 초 이주노동자·준비생)
- 한국어 학습 6개월차 초급 수준 — 모국어 해설도 쉬운 어휘·문법이어야 합니다.

## 페르소나 라운드
아래 페르소나 가이드에서 {{LANGUAGE_NAME}}의 3명 페르소나를 찾아, 각 페르소나의 보이스 특성 관점에서 A/B 중 어느 쪽이 나은지 피드백하십시오.
- 페르소나 피드백은 가이드의 보이스 특성 시뮬레이션입니다. 실제 인물의 발언으로 위조 인용하지 마십시오.
- 3인의 의견이 갈리면 다수결(2:1)로 수렴하고 모순을 명시하십시오.

<persona_guide>
{{PERSONA_SECTION}}
</persona_guide>

## 용어집 (참고)
SEDA 앱 용어집입니다. 해설에 같은 개념이 등장하면 용어 일관성 판단에 참고하십시오.
<glossary>
{{GLOSSARY_SECTION}}
</glossary>

## 6축 비교 평가 — 각 축마다 A/B/tie 판정 + 구체 근거 필수
1. **의미 정확성** — EN 원문 대비 오역·왜곡·누락·임의 추가
2. **자연스러움·레지스터** — 페르소나 합의 기반. 직역투·어색한 어순·저빈도 어휘
3. **학습자 인지 부담** — 6개월차 초급 학습자 기준 어휘·문법 난이도
4. **한국어 보존·형식 무결성** — 한글 원문 훼손 여부, 마크다운 구조, [한국어] = [뜻] 이중 표기 유지
5. **용어 일관성** — 시험·문법 용어의 문서 내 일관성 및 용어집 정합
6. **문화·종교적 금기** — 해당 언어권 금기 어휘·은유 회피

## 판정 규칙
- "자연스럽다" 같은 추상어 단독 판정 금지. 모든 근거에 실제 번역문 인용을 포함하십시오.
- 최종 승자 판정 전, 패자로 판정하는 쪽의 강점을 1줄로 먼저 옹호(steelman)하십시오.
- 치명 오류(의미 반전, 정답/오답 뒤바뀜, 한글 훼손, 대량 누락)는 반드시 critical_errors에 기록하십시오.
- 우열을 가릴 수 없으면 tie로 판정하십시오.

## 출력 (JSON만, 다른 텍스트 절대 금지)
```json
{
  "winner": "A|B|tie",
  "axes": [
    {"axis": "의미 정확성", "winner": "A|B|tie", "evidence": "번역문 인용 포함 구체 근거"},
    {"axis": "자연스러움·레지스터", "winner": "A|B|tie", "evidence": "..."},
    {"axis": "학습자 인지 부담", "winner": "A|B|tie", "evidence": "..."},
    {"axis": "한국어 보존·형식 무결성", "winner": "A|B|tie", "evidence": "..."},
    {"axis": "용어 일관성", "winner": "A|B|tie", "evidence": "..."},
    {"axis": "문화·종교적 금기", "winner": "A|B|tie", "evidence": "..."}
  ],
  "persona_round": [
    {"persona": "이름", "pick": "A|B|tie", "rationale": "보이스 특성 기반 근거"},
    {"persona": "이름", "pick": "A|B|tie", "rationale": "..."},
    {"persona": "이름", "pick": "A|B|tie", "rationale": "..."}
  ],
  "steelman_loser": "패자 쪽의 강점 1줄",
  "critical_errors": {"A": ["..."], "B": ["..."]},
  "confidence": "high|medium|low"
}
```
````

- [ ] **Step 2: 실패하는 테스트 작성** — `tests/test_judge.py`

```python
import json
import random

from server import judge

EN = [{"id": "c1", "type": "text", "order": 1, "config": {}, "content": "### ✅ CORRECT\n\nx"}]
BASE = [{"id": "c1", "type": "text", "order": 1, "config": {}, "content": "base-translation"}]
GEM = [{"id": "c1", "type": "text", "order": 1, "config": {}, "content": "gemma-translation"}]


def test_gate_d_score_uses_pipeline_judge(monkeypatch):
    from local_agent.generators.explanation_pipeline import gate_d

    monkeypatch.setattr(gate_d, "load_gate_d_prompt", lambda: "JUDGE PROMPT")
    monkeypatch.setattr(
        gate_d, "call_llm",
        lambda sp, up, timeout, model: {"score": 9.0, "issues": [], "notes": "good"},
    )
    r = judge.gate_d_score(EN, BASE, "km")
    assert r["score"] == 9.0
    assert r["threshold"] == 8.0          # km 임계 — 파이프라인과 동일
    assert r["passed"] is True


def test_blind_prompt_has_no_source_clues():
    sp = judge.build_compare_system_prompt("vi")
    assert "Tiếng Việt" in sp and "{{" not in sp
    up = judge._build_user_prompt(EN, "AAA", "BBB", "vi")
    payload = json.loads(up)
    assert set(payload.keys()) == {"language", "en_explanation", "translation_A", "translation_B"}
    for banned in ("gemma", "baseline", "기존", "파이프라인", "ollama"):
        assert banned not in up.lower()


def test_blind_compare_maps_winner_by_assignment(monkeypatch):
    fake = {
        "winner": "A",
        "axes": [], "persona_round": [], "steelman_loser": "",
        "critical_errors": {"A": [], "B": []}, "confidence": "high",
    }
    monkeypatch.setattr(judge, "_persona_text", lambda: "personas")
    monkeypatch.setattr(judge, "_glossary_text", lambda: "glossary")
    monkeypatch.setattr(judge.claude_cli, "call_claude", lambda *a, **k: dict(fake))

    seen = set()
    for seed in range(8):
        r = judge.blind_compare(EN, BASE, GEM, "vi", random.Random(seed))
        assert r["assignment"]["A"] in ("baseline", "gemma")
        assert r["winner_side"] == r["assignment"]["A"]  # judge가 A 승 → A에 배정된 쪽
        seen.add(r["assignment"]["A"])
    assert seen == {"baseline", "gemma"}  # 배정이 실제로 무작위로 뒤집힘


def test_blind_compare_handles_judge_failure(monkeypatch):
    monkeypatch.setattr(judge, "_persona_text", lambda: "p")
    monkeypatch.setattr(judge, "_glossary_text", lambda: "g")

    def boom(*a, **k):
        raise RuntimeError("cli dead")

    monkeypatch.setattr(judge.claude_cli, "call_claude", boom)
    r = judge.blind_compare(EN, BASE, GEM, "vi", random.Random(1))
    assert r["winner_side"] == "unknown"
    assert "cli dead" in r["error"]
```

- [ ] **Step 3: 테스트 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_judge.py -v`
Expected: FAIL — `No module named 'server.judge'`

- [ ] **Step 4: 구현** — `server/judge.py`

```python
"""평가 — Gate D 채점(파이프라인 그대로) + 블라인드 비교(translator 방법론 각색).

Gate D: claude-sonnet-4-6, translator_judge.md, 양측 모두 신규 채점.
블라인드: opus, prompts/compare_judge.md, A/B 무작위 배정 후 unblind 매핑.
"""
from __future__ import annotations

import functools
import json
import random
from dataclasses import asdict
from pathlib import Path

from . import qbank

qbank.bootstrap()
from local_agent.core import claude_cli  # noqa: E402
from local_agent.generators.explanation_pipeline import gate_d  # noqa: E402

GATE_D_MODEL = gate_d._SONNET_MODEL  # "claude-sonnet-4-6"
BLIND_MODEL = "opus"
BLIND_TIMEOUT = 300
COMPARE_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "compare_judge.md"


def gate_d_score(en_components: list[dict], lang_components: list[dict], lang: str) -> dict:
    summary = gate_d.run_gate_d(
        en_components,
        {lang: lang_components},
        sampling=False,
        system_prompt=gate_d.load_gate_d_prompt(),
    )
    result = summary.by_language[lang]
    return asdict(result)


def components_text(comps: list[dict]) -> str:
    parts = []
    for c in comps:
        content = c.get("content")
        if isinstance(content, str):
            parts.append(f"[{c.get('id')}]\n{content}")
    return "\n\n---\n\n".join(parts)


@functools.lru_cache(maxsize=1)
def _persona_text() -> str:
    return qbank.load_persona_guide()


@functools.lru_cache(maxsize=1)
def _glossary_text() -> str:
    return qbank.load_glossary()


def build_compare_system_prompt(lang: str) -> str:
    from local_agent.generators.explanation_pipeline import translator as qb_translator

    template = COMPARE_PROMPT_PATH.read_text(encoding="utf-8")
    return (
        template.replace("{{LANGUAGE_NAME}}", qb_translator.get_language_name(lang))
        .replace("{{LANGUAGE_CODE}}", lang)
        .replace("{{PERSONA_SECTION}}", _persona_text())
        .replace("{{GLOSSARY_SECTION}}", _glossary_text())
    )


def _build_user_prompt(en_components: list[dict], text_a: str, text_b: str, lang: str) -> str:
    return json.dumps(
        {
            "language": lang,
            "en_explanation": components_text(en_components),
            "translation_A": text_a,
            "translation_B": text_b,
        },
        ensure_ascii=False,
    )


def blind_compare(
    en_components: list[dict],
    baseline_comps: list[dict],
    gemma_comps: list[dict],
    lang: str,
    rng: random.Random,
) -> dict:
    if rng.random() < 0.5:
        assignment = {"A": "baseline", "B": "gemma"}
        text_a, text_b = components_text(baseline_comps), components_text(gemma_comps)
    else:
        assignment = {"A": "gemma", "B": "baseline"}
        text_a, text_b = components_text(gemma_comps), components_text(baseline_comps)

    system_prompt = build_compare_system_prompt(lang)
    user_prompt = _build_user_prompt(en_components, text_a, text_b, lang)

    try:
        raw = claude_cli.call_claude(
            system_prompt,
            user_prompt,
            max_retries=2,
            timeout=BLIND_TIMEOUT,
            model=BLIND_MODEL,
        )
    except Exception as exc:  # noqa: BLE001
        return {"assignment": assignment, "winner_side": "unknown", "raw": None, "error": str(exc)}

    winner_label = raw.get("winner") if isinstance(raw, dict) else None
    if winner_label == "tie":
        winner_side = "tie"
    elif winner_label in ("A", "B"):
        winner_side = assignment[winner_label]
    else:
        return {
            "assignment": assignment,
            "winner_side": "unknown",
            "raw": raw if isinstance(raw, dict) else None,
            "error": f"판정 파싱 불능: winner={winner_label!r}",
        }
    return {"assignment": assignment, "winner_side": winner_side, "raw": raw, "error": None}
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_judge.py -v`
Expected: PASS 4개

- [ ] **Step 6: Commit**

```bash
git add server/judge.py prompts/ tests/test_judge.py
git commit -m "feat: Gate D 신규 채점 + 블라인드 6축·페르소나 비교 judge"
```

---

### Task 7: runner.py — 실행 오케스트레이션 + 매니페스트

**Files:**
- Create: `server/runner.py`, `tests/test_runner.py`

**Interfaces:**
- Consumes: Task 1–6의 모든 인터페이스
- Produces: `ArenaRunner(store: RunStore)` —
  - `.start_run(payload: dict) -> str` — payload: `{"question_id": str|None, "set_id": str|None, "question_number": int|None, "en_text": str|None, "languages": list[str]|None}`. 백그라운드 스레드 실행.
  - `.retranslate_language(run_id, lang)`, `.evaluate_language(run_id, lang)` — 개별 재실행 (스레드)
  - status.json `state` 전이: `created → loading → translating → evaluating → completed | failed`
  - eval/<lang>.json 형식: `{"gates": ..., "gate_d": {"baseline": ...|None, "gemma": ...}, "blind": ...|None, "verdict": ..., "duration_sec": float, "translate_meta": list}`
  - manifest.json: 모델 태그·다이제스트·ollama 버전·OPTIONS·프롬프트 SHA-256 3종·재시도 정책·타임아웃 편차·judge 모델·시드·기준선 라벨

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_runner.py`

```python
"""러너 통합 테스트 — 외부 경계(qbank/gemma/judge) 전부 stub."""
import time

from server import runner as runner_mod
from server.runner import ArenaRunner
from server.store import RunStore
from server.qbank import ResolvedQuestion

EN = [{"id": "c1", "type": "text", "order": 1, "config": {}, "content": "### ✅ C\n\nx ✅ ❌"}]
TRANS = [{"id": "c1", "type": "text", "order": 1, "config": {}, "content": "y ✅ ❌"}]


def _stub_all(monkeypatch):
    monkeypatch.setattr(runner_mod.qbank, "resolve_by_uuid", lambda qid: ResolvedQuestion(qid, "ept"))
    monkeypatch.setattr(
        runner_mod.qbank, "fetch_bundle",
        lambda rq: {"en": EN, "baseline": {"vi": TRANS, "ne": TRANS}},
    )
    monkeypatch.setattr(
        runner_mod.gemma, "translate_language",
        lambda en, lang, feedback_prefix="": ([dict(c) for c in TRANS], [{"attempts": 1, "duration_sec": 1.0}]),
    )
    monkeypatch.setattr(runner_mod.gemma, "model_info", lambda: {"tag": "gemma4:12B", "digest": "abc"})
    monkeypatch.setattr(
        runner_mod.judge, "gate_d_score",
        lambda en, comps, lang: {"score": 9.0, "passed": True, "threshold": 8.5, "issues": [], "notes": ""},
    )
    monkeypatch.setattr(
        runner_mod.judge, "blind_compare",
        lambda en, base, gem, lang, rng: {
            "assignment": {"A": "baseline", "B": "gemma"},
            "winner_side": "gemma", "raw": {"winner": "B"}, "error": None,
        },
    )


def _wait_state(store, run_id, state, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if store.read_json(run_id, "status.json")["state"] == state:
            return True
        time.sleep(0.05)
    return False


def test_full_run_produces_summary(tmp_path, monkeypatch):
    _stub_all(monkeypatch)
    store = RunStore(tmp_path / "runs")
    r = ArenaRunner(store)
    run_id = r.start_run({"question_id": "q1", "languages": ["vi", "ne"]})
    assert _wait_state(store, run_id, "completed")

    detail = store.run_detail(run_id)
    assert detail["manifest"]["model"]["tag"] == "gemma4:12B"
    assert detail["manifest"]["baseline_label"] == "DB에 저장된 기존 파이프라인 번역"
    assert set(detail["gemma"].keys()) == {"vi", "ne"}
    assert detail["eval"]["vi"]["verdict"]["winner"] == "gemma"
    assert detail["summary"]["by_language"]["vi"]["wins"] == 1
    assert detail["summary"]["deployable"]["vi"]["deployable"] in (True, False)
    assert "판정 규칙" in detail["summary"]["rules_text"]


def test_run_without_baseline_skips_eval(tmp_path, monkeypatch):
    _stub_all(monkeypatch)
    monkeypatch.setattr(
        runner_mod.qbank, "fetch_bundle", lambda rq: {"en": EN, "baseline": {}}
    )
    store = RunStore(tmp_path / "runs")
    r = ArenaRunner(store)
    run_id = r.start_run({"question_id": "q1", "languages": ["vi"]})
    assert _wait_state(store, run_id, "completed")
    ev = store.read_json(run_id, "eval/vi.json")
    assert ev["blind"] is None                    # 기준선 없음 → 비교 없음
    assert ev["verdict"]["winner"] == "unknown"


def test_failed_load_marks_run_failed(tmp_path, monkeypatch):
    _stub_all(monkeypatch)

    def boom(qid):
        raise RuntimeError("db down")

    monkeypatch.setattr(runner_mod.qbank, "resolve_by_uuid", boom)
    store = RunStore(tmp_path / "runs")
    r = ArenaRunner(store)
    run_id = r.start_run({"question_id": "q1", "languages": ["vi"]})
    assert _wait_state(store, run_id, "failed")
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_runner.py -v`
Expected: FAIL — `No module named 'server.runner'`

- [ ] **Step 3: 구현** — `server/runner.py`

```python
"""실행 오케스트레이션.

플로우: 입력 해석 → EN·기준선 로드 → gemma 번역(+Gate C 피드백 재시도)
      → 평가(Gate D 양측 + 블라인드, 언어당 병렬 2) → 판정 → 집계.
"""
from __future__ import annotations

import hashlib
import random
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import arena_gates, gemma, judge, qbank, verdict
from .store import RunStore

RULES_TEXT = (
    "판정 규칙: ① gemma가 재시도 후에도 Gate B/C(구조·결정적 검사) 실패 → 기존 파이프라인 자동 승. "
    "② 게이트 통과 시 블라인드 비교 판정(6축·페르소나 라운드, 출처 은닉)의 승자를 채택. "
    "③ Gate D 점수(양측 모두 신규 채점, claude-sonnet-4-6)는 보조 지표 — 블라인드 판정과 "
    "강하게 상충(|Δ|≥1.0)하면 상충 플래그 표시. "
    "언어별 '실전 투입 가능'은 게이트 실패율 0% + gemma Gate D 평균 ≥ 임계값(km·bn·ur 8.0, 그 외 8.5) "
    "+ 블라인드 승+무 ≥ 50% 를 모두 충족해야 한다."
)

_EVAL_WORKERS = 2


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


class ArenaRunner:
    def __init__(self, store: RunStore) -> None:
        self.store = store

    # ---------------- public ----------------

    def start_run(self, payload: dict) -> str:
        langs = payload.get("languages") or list(qbank.ARENA_LANGS)
        langs = [l for l in langs if l in qbank.ARENA_LANGS]
        run_id = self.store.create_run(payload, langs)
        threading.Thread(target=self._execute, args=(run_id, payload, langs), daemon=True).start()
        return run_id

    def retranslate_language(self, run_id: str, lang: str) -> None:
        threading.Thread(target=self._retranslate, args=(run_id, lang), daemon=True).start()

    def evaluate_language(self, run_id: str, lang: str) -> None:
        threading.Thread(target=self._evaluate_and_save, args=(run_id, lang), daemon=True).start()

    # ---------------- internals ----------------

    def _write_manifest(self, run_id: str) -> None:
        qb = qbank.QBANK_ROOT / "local_agent" / "resources" / "prompts" / "explanation"
        compare_prompt = judge.COMPARE_PROMPT_PATH
        self.store.write_json(
            run_id,
            "manifest.json",
            {
                "model": gemma.model_info(),
                "options": gemma.OPTIONS,
                "max_attempts_per_component": gemma.MAX_ATTEMPTS,
                "gate_c_feedback_retry": 1,
                "component_timeout_sec": gemma.COMPONENT_TIMEOUT_SEC,
                "deviations": [
                    "타임아웃 600s (파이프라인 150~240s) — 로컬 추론 속도 대응, 출력 품질 무관",
                    "temperature 0.3 / num_predict 8000 — 파이프라인 Vertex 폴백 파라미터 미러링",
                ],
                "prompts_sha256": {
                    "TRANSLATE.md": _sha256_file(qb / "TRANSLATE.md"),
                    "translator_judge.md": _sha256_file(qb / "translator_judge.md"),
                    "compare_judge.md": _sha256_file(compare_prompt),
                },
                "judge_models": {"gate_d": judge.GATE_D_MODEL, "blind": judge.BLIND_MODEL},
                "baseline_label": "DB에 저장된 기존 파이프라인 번역",
                "languages": None,  # status.json이 SoT
                "blind_seed": "sha256(run_id)[:8] + lang",
            },
        )

    def _rng_for(self, run_id: str, lang: str) -> random.Random:
        seed = hashlib.sha256(f"{run_id}:{lang}".encode()).hexdigest()
        return random.Random(int(seed[:8], 16))

    def _execute(self, run_id: str, payload: dict, langs: list[str]) -> None:
        try:
            self.store.update_status(run_id, state="loading")
            en, baseline = self._load(run_id, payload)
            if en is None:
                raise RuntimeError("EN 해설을 찾을 수 없습니다 (en_text 직접 입력 또는 다른 문제 선택)")
            self.store.write_json(run_id, "en.json", en)
            for lang, comps in baseline.items():
                self.store.write_json(run_id, f"baseline/{lang}.json", {"components": comps})
            self._write_manifest(run_id)

            self.store.update_status(run_id, state="translating")
            for lang in langs:
                self._translate_one(run_id, lang, en)

            self.store.update_status(run_id, state="evaluating")
            with ThreadPoolExecutor(max_workers=_EVAL_WORKERS) as pool:
                list(pool.map(lambda l: self._evaluate_and_save(run_id, l, finalize=False), langs))

            self._finalize_summary(run_id)
            self.store.update_status(run_id, state="completed")
        except Exception as exc:  # noqa: BLE001
            self.store.append_log(run_id, f"실행 실패: {exc}")
            self.store.update_status(run_id, state="failed", error=str(exc), trace=traceback.format_exc())

    def _load(self, run_id: str, payload: dict) -> tuple[list | None, dict]:
        if payload.get("en_text"):
            en = [{"id": "pasted", "type": "text", "order": 1, "config": {}, "content": payload["en_text"]}]
            baseline: dict = {}
            if payload.get("question_id"):
                rq = qbank.resolve_by_uuid(payload["question_id"])
                baseline = qbank.fetch_bundle(rq)["baseline"]
            return en, baseline
        if payload.get("question_id"):
            rq = qbank.resolve_by_uuid(payload["question_id"])
        elif payload.get("set_id") and payload.get("question_number") is not None:
            rq = qbank.resolve_by_set(payload["set_id"], int(payload["question_number"]))
        else:
            raise RuntimeError("question_id 또는 set_id+question_number 또는 en_text가 필요합니다")
        self.store.update_status(run_id, question_id=rq.question_id, source=rq.source)
        self.store.append_log(run_id, f"문제 해석: {rq.question_id} ({rq.source})")
        bundle = qbank.fetch_bundle(rq)
        return bundle["en"], bundle["baseline"]

    def _translate_one(self, run_id: str, lang: str, en: list[dict]) -> None:
        self.store.append_log(run_id, f"[{lang}] gemma 번역 시작")
        started = time.monotonic()
        comps, metas = gemma.translate_language(en, lang)
        gates_result = arena_gates.run_gates(en, comps, lang)
        retried = False
        if not gates_result["gate_c"]["passed"]:
            failed = gates_result["gate_c"]["failed_checks"]
            self.store.append_log(run_id, f"[{lang}] Gate C 실패 {failed} → 피드백 재번역")
            feedback = arena_gates.build_gate_c_feedback(failed)
            comps2, metas2 = gemma.translate_language(en, lang, feedback_prefix=feedback)
            gates2 = arena_gates.run_gates(en, comps2, lang)
            retried = True
            if gates2["passed"] or len(gates2["gate_c"]["failed_checks"]) < len(failed):
                comps, metas, gates_result = comps2, metas + metas2, gates2
        duration = round(time.monotonic() - started, 1)
        self.store.write_json(
            run_id,
            f"gemma/{lang}.json",
            {
                "components": comps,
                "meta": metas,
                "gates": gates_result,
                "gate_c_feedback_retried": retried,
                "duration_sec": duration,
            },
        )
        self.store.append_log(run_id, f"[{lang}] 번역 완료 ({duration}s, 게이트 {'통과' if gates_result['passed'] else '실패'})")

    def _retranslate(self, run_id: str, lang: str) -> None:
        try:
            en = self.store.read_json(run_id, "en.json")
            self._translate_one(run_id, lang, en)
            self._evaluate_and_save(run_id, lang)
        except Exception as exc:  # noqa: BLE001
            self.store.append_log(run_id, f"[{lang}] 재번역 실패: {exc}")

    def _evaluate_and_save(self, run_id: str, lang: str, finalize: bool = True) -> None:
        try:
            en = self.store.read_json(run_id, "en.json")
            gemma_doc = self.store.read_json(run_id, f"gemma/{lang}.json")
            baseline_doc = self.store.read_json(run_id, f"baseline/{lang}.json")
            if not en or not gemma_doc:
                raise RuntimeError("번역 결과가 없어 평가 불가")
            gemma_comps = gemma_doc["components"]
            gates_result = gemma_doc["gates"]

            self.store.append_log(run_id, f"[{lang}] Gate D 채점 (gemma)")
            gd_gemma = judge.gate_d_score(en, gemma_comps, lang)
            gd_baseline = None
            blind = None
            if baseline_doc:
                self.store.append_log(run_id, f"[{lang}] Gate D 채점 (기존 번역)")
                gd_baseline = judge.gate_d_score(en, baseline_doc["components"], lang)
                self.store.append_log(run_id, f"[{lang}] 블라인드 비교 판정")
                blind = judge.blind_compare(
                    en, baseline_doc["components"], gemma_comps, lang, self._rng_for(run_id, lang)
                )

            cell = verdict.cell_verdict(gates_result, gd_baseline, gd_gemma, blind)
            self.store.write_json(
                run_id,
                f"eval/{lang}.json",
                {
                    "gates": gates_result,
                    "gate_d": {"baseline": gd_baseline, "gemma": gd_gemma},
                    "blind": blind,
                    "verdict": cell,
                    "duration_sec": gemma_doc.get("duration_sec"),
                    "translate_meta": gemma_doc.get("meta", []),
                },
            )
            self.store.append_log(run_id, f"[{lang}] 판정: {cell['winner']} ({cell['path']})")
            if finalize:
                self._finalize_summary(run_id)
        except Exception as exc:  # noqa: BLE001
            self.store.append_log(run_id, f"[{lang}] 평가 실패: {exc}")
            self.store.write_json(
                run_id, f"eval/{lang}.json",
                {"error": str(exc), "verdict": {"winner": "unknown", "path": "no_verdict",
                                                "logic": f"평가 실패: {exc}", "gate_d_delta": None}},
            )

    def _finalize_summary(self, run_id: str) -> None:
        detail = self.store.run_detail(run_id)
        by_language: dict = {}
        deployable: dict = {}
        for lang, ev in detail["eval"].items():
            if "verdict" not in ev:
                continue
            cells = [ev]
            s = verdict.language_summary(cells)
            by_language[lang] = s
            deployable[lang] = verdict.deployable_verdict(lang, s)
        self.store.write_json(
            run_id,
            "summary.json",
            {"by_language": by_language, "deployable": deployable, "rules_text": RULES_TEXT},
        )
```

주의: 이 러너는 1 run = 1 문제 구조다 (복수 문제는 UI에서 run을 여러 개 생성). `language_summary`가 셀 1개로 동작하는 것은 의도된 것이며, 대시보드의 "여러 문제 집계"는 Task 8의 `/api/aggregate`가 여러 run의 eval 셀을 모아 `verdict.language_summary`로 계산한다.

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_runner.py -v`
Expected: PASS 3개

- [ ] **Step 5: Commit**

```bash
git add server/runner.py tests/test_runner.py
git commit -m "feat: 실행 오케스트레이션 — 번역·게이트 재시도·평가 병렬·매니페스트·집계"
```

---

### Task 8: app.py — FastAPI API + run.sh

**Files:**
- Create: `server/app.py`, `run.sh`, `tests/test_app.py`

**Interfaces:**
- Consumes: `RunStore`, `ArenaRunner`, `qbank`, `gemma.ollama_health`
- Produces (spec §9 + 집계·헬스):
  - `POST /api/runs` → `{"run_id"}` / `GET /api/runs` / `GET /api/runs/{id}`
  - `POST /api/runs/{id}/languages/{lang}/retranslate`, `POST /api/runs/{id}/languages/{lang}/evaluate`
  - `GET /api/sets`, `GET /api/sets/{id}/questions`, `GET /api/samples`
  - `GET /api/aggregate` — 전체 run의 eval 셀을 언어별로 모아 `language_summary`+`deployable_verdict` 재계산
  - `GET /api/health` — ollama·supabase env·claude CLI 존재 여부
  - `GET /` — `ui/index.html` 서빙, `/static` → `ui/`

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_app.py`

```python
from fastapi.testclient import TestClient

from server import app as app_mod


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "store", app_mod.RunStore(tmp_path / "runs"))
    monkeypatch.setattr(app_mod, "runner", app_mod.ArenaRunner(app_mod.store))
    return TestClient(app_mod.app)


def test_create_and_get_run(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(app_mod.runner, "start_run", lambda payload: "run-1")
    r = client.post("/api/runs", json={"question_id": "q1", "languages": ["vi"]})
    assert r.status_code == 200 and r.json()["run_id"] == "run-1"


def test_list_runs_empty(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.get("/api/runs").json() == []


def test_run_detail_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.get("/api/runs/nope").status_code == 404


def test_language_actions_dispatch(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    run_id = app_mod.store.create_run({}, ["vi"])
    calls = []
    monkeypatch.setattr(app_mod.runner, "retranslate_language", lambda r, l: calls.append(("t", l)))
    monkeypatch.setattr(app_mod.runner, "evaluate_language", lambda r, l: calls.append(("e", l)))
    client.post(f"/api/runs/{run_id}/languages/vi/retranslate")
    client.post(f"/api/runs/{run_id}/languages/vi/evaluate")
    assert calls == [("t", "vi"), ("e", "vi")]


def test_invalid_language_rejected(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    run_id = app_mod.store.create_run({}, ["vi"])
    assert client.post(f"/api/runs/{run_id}/languages/ru/evaluate").status_code == 400


def test_aggregate_merges_runs(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    for _ in range(2):
        rid = app_mod.store.create_run({}, ["vi"])
        app_mod.store.write_json(rid, "eval/vi.json", {
            "gates": {"passed": True, "gate_c": {"failed_checks": []}},
            "gate_d": {"baseline": {"score": 8.0}, "gemma": {"score": 9.0}},
            "verdict": {"winner": "gemma", "path": "blind", "gate_d_delta": 1.0},
            "duration_sec": 5.0,
        })
    agg = client.get("/api/aggregate").json()
    assert agg["by_language"]["vi"]["n"] == 2
    assert agg["by_language"]["vi"]["wins"] == 2
    assert agg["deployable"]["vi"]["deployable"] is False  # Gate D 9.0 통과·게이트 0·승률 100% → True인지 확인
```

주의: 마지막 assert는 구현 후 실제 기대값으로 맞춘다 — 위 데이터면 3조건 모두 충족이라 `True`가 옳다. 테스트를 `is True`로 고칠 것 (작성 시점 오류 방지용 메모).

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_app.py -v`
Expected: FAIL — `No module named 'server.app'`

- [ ] **Step 3: 구현** — `server/app.py`

```python
"""Gemma Arena FastAPI 앱."""
from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import gemma, qbank, verdict
from .runner import ArenaRunner
from .store import RunStore

ROOT = Path(__file__).resolve().parents[1]
UI_DIR = ROOT / "ui"

app = FastAPI(title="Gemma Arena")
store = RunStore(ROOT / "runs")
runner = ArenaRunner(store)


@app.post("/api/runs")
def create_run(payload: dict):
    if not (payload.get("question_id") or payload.get("en_text")
            or (payload.get("set_id") and payload.get("question_number") is not None)):
        raise HTTPException(400, "question_id / set_id+question_number / en_text 중 하나가 필요합니다")
    return {"run_id": runner.start_run(payload)}


@app.get("/api/runs")
def list_runs():
    return store.list_runs()


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    if not store.path(run_id).is_dir():
        raise HTTPException(404, "run 없음")
    return store.run_detail(run_id)


def _check_lang(run_id: str, lang: str) -> None:
    if not store.path(run_id).is_dir():
        raise HTTPException(404, "run 없음")
    if lang not in qbank.ARENA_LANGS:
        raise HTTPException(400, f"지원 언어 아님: {lang}")


@app.post("/api/runs/{run_id}/languages/{lang}/retranslate")
def retranslate(run_id: str, lang: str):
    _check_lang(run_id, lang)
    runner.retranslate_language(run_id, lang)
    return {"ok": True}


@app.post("/api/runs/{run_id}/languages/{lang}/evaluate")
def evaluate(run_id: str, lang: str):
    _check_lang(run_id, lang)
    runner.evaluate_language(run_id, lang)
    return {"ok": True}


@app.get("/api/sets")
def sets():
    return qbank.list_sets()


@app.get("/api/sets/{set_id}/questions")
def set_questions(set_id: str):
    return qbank.list_set_questions(set_id)


@app.get("/api/samples")
def samples():
    return qbank.sample_questions()


@app.get("/api/aggregate")
def aggregate():
    cells_by_lang: dict[str, list[dict]] = {}
    for run in store.list_runs():
        detail = store.run_detail(run["run_id"])
        for lang, ev in detail["eval"].items():
            if "verdict" in ev and ev["verdict"]["winner"] != "unknown" or "gate_d" in ev:
                cells_by_lang.setdefault(lang, []).append(ev)
    by_language = {lang: verdict.language_summary(cells) for lang, cells in cells_by_lang.items()}
    deployable = {lang: verdict.deployable_verdict(lang, s) for lang, s in by_language.items()}
    from .runner import RULES_TEXT
    return {"by_language": by_language, "deployable": deployable, "rules_text": RULES_TEXT}


@app.get("/api/health")
def health():
    import os

    return {
        "ollama": gemma.ollama_health(),
        "supabase_env": bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SECRET_KEY")),
        "claude_cli": shutil.which("claude") is not None,
        "hint": "supabase_env=false면 ./run.sh(direnv exec)로 서버를 띄우세요",
    }


@app.get("/")
def index():
    return FileResponse(UI_DIR / "index.html")


app.mount("/static", StaticFiles(directory=UI_DIR), name="static")
```

주의: `app.mount`는 모듈 하단에 두고, `UI_DIR`가 없으면 서버가 죽으므로 이 태스크에서 `ui/index.html`을 빈 껍데기(`<h1>Gemma Arena</h1>`)로 먼저 생성한다 (Task 9에서 교체).

- [ ] **Step 4: run.sh 작성 + 실행 권한**

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
exec direnv exec /Users/cokoroad/seda-question-bank \
  "$PWD/.venv/bin/python" -m uvicorn server.app:app --host 127.0.0.1 --port 8765
```

Run: `chmod +x run.sh`

- [ ] **Step 5: 테스트 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_app.py -v`
Expected: PASS 6개 (aggregate 테스트는 Step 1 주의사항대로 기대값 `is True` 확정)

- [ ] **Step 6: Commit**

```bash
git add server/app.py run.sh ui/ tests/test_app.py
git commit -m "feat: FastAPI API — run 생성·조회·언어별 재실행·집계·헬스·run.sh"
```

---

### Task 9: UI — 결과·판정 논리 가시화

**Files:**
- Create: `ui/index.html` (교체), `ui/app.js`, `ui/style.css`

**Interfaces:**
- Consumes: Task 8의 API 전부
- Produces: 화면 4개 — ① 새 실행 폼(+세트 브라우즈·샘플) ② 진행(status.json 폴링 2초) ③ 결과 상세(문제×언어) ④ 대시보드(집계). 해시 라우팅(`#/`, `#/run/<id>`, `#/run/<id>/<lang>`, `#/dashboard`).

핵심 요구 (spec §8, 사용자 명시): 결과 상세에 **[EN | 기존 번역 | gemma] 3열**(ur는 `dir="rtl"`), **게이트 배지**, **Gate D 6기준 점수 카드(양측)**, **블라인드 판정 카드**(승자·6축 표·페르소나 3인·steelman·치명 오류·confidence·배정 공개), **판정 논리 박스**(`verdict.logic` + `RULES_TEXT`), 측정 메타(소요 시간·재시도). 대시보드는 언어별 집계표 + deployable 조건별 충족 현황 + 판정 규칙 박스.

- [ ] **Step 1: `ui/style.css` 작성**

```css
:root { --bg:#f7f7f8; --card:#fff; --line:#ddd; --win:#0a7d33; --lose:#b3261e; --tie:#8a6d00; }
* { box-sizing: border-box; }
body { margin:0; font-family:-apple-system,"Apple SD Gothic Neo",sans-serif; background:var(--bg); color:#1c1c1e; }
header { background:#1c1c2e; color:#fff; padding:12px 20px; display:flex; gap:16px; align-items:center; }
header a { color:#cfcfe8; text-decoration:none; font-weight:600; }
main { max-width:1400px; margin:0 auto; padding:20px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; margin-bottom:16px; }
.grid3 { display:grid; grid-template-columns:1fr 1fr 1fr; gap:12px; }
.md { overflow-x:auto; line-height:1.6; font-size:14px; }
.md table { border-collapse:collapse; } .md td,.md th { border:1px solid var(--line); padding:4px 8px; }
.badge { display:inline-block; padding:2px 10px; border-radius:12px; font-size:12px; font-weight:700; color:#fff; margin-right:6px; }
.badge.pass { background:var(--win); } .badge.fail { background:var(--lose); } .badge.neutral { background:#666; }
.winner-gemma { color:var(--win); font-weight:800; } .winner-baseline { color:var(--lose); font-weight:800; }
.winner-tie { color:var(--tie); font-weight:800; }
table.compare { width:100%; border-collapse:collapse; font-size:14px; }
table.compare th, table.compare td { border:1px solid var(--line); padding:6px 10px; text-align:left; vertical-align:top; }
.logic-box { background:#f0f4ff; border-left:4px solid #3b5bdb; padding:12px 16px; border-radius:6px; white-space:pre-wrap; }
.rules-box { background:#fffbe6; border-left:4px solid #b8860b; padding:12px 16px; border-radius:6px; font-size:13px; }
.rtl { direction:rtl; text-align:right; }
button { cursor:pointer; border:1px solid #3b5bdb; background:#3b5bdb; color:#fff; border-radius:6px; padding:8px 14px; font-size:14px; }
button.secondary { background:#fff; color:#3b5bdb; }
input, textarea, select { width:100%; padding:8px; border:1px solid var(--line); border-radius:6px; font-size:14px; }
.form-row { margin-bottom:12px; }
.lang-checks label { margin-right:12px; font-size:14px; }
.log { font-family:ui-monospace,monospace; font-size:12px; background:#111; color:#9f9; padding:12px; border-radius:6px; max-height:280px; overflow-y:auto; white-space:pre-wrap; }
.score-pair { display:flex; gap:8px; align-items:baseline; }
.muted { color:#777; font-size:12px; }
h3 .small { font-size:13px; font-weight:400; color:#666; }
```

- [ ] **Step 2: `ui/index.html` 교체**

```html
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <title>Gemma Arena — 해설 번역 비교</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <header>
    <span style="font-size:18px">⚖️ Gemma Arena</span>
    <a href="#/">새 실행</a>
    <a href="#/runs">실행 목록</a>
    <a href="#/dashboard">대시보드</a>
    <span id="health" class="muted"></span>
  </header>
  <main id="app"></main>
  <script src="/static/app.js"></script>
</body>
</html>
```

- [ ] **Step 3: `ui/app.js` 작성** — 전체 SPA. 아래 코드를 그대로 사용한다 (약 380줄). 마크다운은 외부 라이브러리 없이 내장 미니 렌더러(헤더·볼드·이탤릭·표·리스트·코드스팬·줄바꿈)로 처리한다.

```javascript
/* Gemma Arena SPA — 해시 라우팅, fetch 기반 */
const $ = (sel) => document.querySelector(sel);
const app = $("#app");
const LANG_NAMES = { ne:"네팔어", vi:"베트남어", id:"인도네시아어", bn:"뱅골어",
                     ur:"우르두어", km:"크메르어", th:"태국어", tl:"타갈로그어" };
const SIDE_LABEL = { baseline:"기존 파이프라인", gemma:"gemma4:12B", tie:"무승부", unknown:"미확정" };

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

/* --- 미니 마크다운 렌더러 --- */
function md(text) {
  if (typeof text !== "string") return "<em>(비텍스트 컴포넌트)</em>";
  const lines = text.split("\n");
  let html = "", inTable = false, inList = false;
  const inline = (s) => esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
  for (const line of lines) {
    if (/^\s*\|/.test(line)) {
      const cells = line.replace(/^\s*\||\|\s*$/g, "").split("|");
      if (/^[\s|:-]+$/.test(line)) continue; // 구분선
      if (!inTable) { html += "<table>"; inTable = true; }
      html += "<tr>" + cells.map(c => `<td>${inline(c.trim())}</td>`).join("") + "</tr>";
      continue;
    } else if (inTable) { html += "</table>"; inTable = false; }
    const h = line.match(/^(#{2,6})\s+(.*)$/);
    if (h) { const lv = Math.min(h[1].length, 6); html += `<h${lv}>${inline(h[2])}</h${lv}>`; continue; }
    const li = line.match(/^\s*[-*]\s+(.*)$/);
    if (li) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${inline(li[1])}</li>`; continue;
    } else if (inList) { html += "</ul>"; inList = false; }
    if (line.trim() === "") { html += "<br>"; continue; }
    html += `<p>${inline(line)}</p>`;
  }
  if (inTable) html += "</table>";
  if (inList) html += "</ul>";
  return html;
}

const api = async (path, opts) => {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${path}: ${r.status} ${await r.text()}`);
  return r.json();
};
const post = (path, body) => api(path, { method:"POST", headers:{"Content-Type":"application/json"},
                                         body: body ? JSON.stringify(body) : null });

/* --- 헬스 표시 --- */
async function refreshHealth() {
  try {
    const h = await api("/api/health");
    $("#health").textContent =
      `ollama:${h.ollama.ok ? "✓" : "✗"} db:${h.supabase_env ? "✓" : "✗"} claude:${h.claude_cli ? "✓" : "✗"}`;
  } catch { $("#health").textContent = "서버 연결 실패"; }
}

/* --- 화면 1: 새 실행 --- */
async function viewNewRun() {
  const langChecks = Object.entries(LANG_NAMES)
    .map(([c, n]) => `<label><input type="checkbox" name="lang" value="${c}" checked> ${n}(${c})</label>`)
    .join("");
  app.innerHTML = `
    <div class="card"><h2>새 비교 실행</h2>
      <div class="form-row"><label>문제 UUID (3개 테이블 자동 검색)</label>
        <input id="qid" placeholder="bank question UUID"></div>
      <div class="form-row"><label>또는 세트 UUID + 문제 번호</label>
        <div style="display:flex;gap:8px">
          <input id="setid" placeholder="question_set_id UUID" style="flex:3">
          <input id="qnum" placeholder="번호" type="number" style="flex:1"></div></div>
      <div class="form-row"><label>또는 영어 해설 직접 붙여넣기 (문제 UUID 없으면 gemma 결과만 표시)</label>
        <textarea id="entext" rows="4" placeholder="### ✅ CORRECT ANSWER ..."></textarea></div>
      <div class="form-row lang-checks"><label>언어</label><br>${langChecks}</div>
      <button id="go">실행</button>
      <span id="err" style="color:#b3261e;margin-left:12px"></span>
    </div>
    <div class="card"><h3>빠른 선택 <span class="small">EN 해설이 있는 최근 문제</span></h3>
      <div id="samples" class="muted">불러오는 중…</div></div>`;
  $("#go").onclick = async () => {
    const languages = [...document.querySelectorAll('input[name="lang"]:checked')].map(x => x.value);
    const payload = { languages };
    if ($("#qid").value.trim()) payload.question_id = $("#qid").value.trim();
    if ($("#setid").value.trim()) { payload.set_id = $("#setid").value.trim();
      payload.question_number = Number($("#qnum").value); }
    if ($("#entext").value.trim()) payload.en_text = $("#entext").value.trim();
    try {
      const { run_id } = await post("/api/runs", payload);
      location.hash = `#/run/${run_id}`;
    } catch (e) { $("#err").textContent = e.message; }
  };
  try {
    const samples = await api("/api/samples");
    $("#samples").innerHTML = samples.map(s =>
      `<div><a href="#" data-qid="${s.question_id}">${s.question_id}</a>
       <span class="muted">${s.source} · ${s.updated_at ?? ""}</span></div>`).join("") || "없음";
    $("#samples").querySelectorAll("a").forEach(a => a.onclick = (ev) => {
      ev.preventDefault(); $("#qid").value = a.dataset.qid; window.scrollTo(0, 0);
    });
  } catch (e) { $("#samples").textContent = `샘플 조회 실패: ${e.message}`; }
}

/* --- 화면 2+3: run 상세 (진행 + 결과) --- */
let pollTimer = null;
async function viewRun(runId, lang) {
  clearInterval(pollTimer);
  const render = async () => {
    const d = await api(`/api/runs/${runId}`);
    const st = d.status || {};
    const running = !["completed", "failed"].includes(st.state);
    if (!running) clearInterval(pollTimer);
    if (lang && d.eval[lang] !== undefined || lang && d.gemma[lang] !== undefined) {
      renderLangDetail(d, runId, lang); return;
    }
    const langRows = (st.languages || []).map(l => {
      const ev = d.eval[l], gm = d.gemma[l];
      const v = ev?.verdict;
      const badge = gm ? (gm.gates?.passed
        ? `<span class="badge pass">게이트 통과</span>` : `<span class="badge fail">게이트 실패</span>`)
        : `<span class="badge neutral">대기</span>`;
      const winner = v ? `<span class="winner-${v.winner}">${SIDE_LABEL[v.winner] ?? v.winner}</span>` : "—";
      return `<tr><td>${LANG_NAMES[l]}(${l})</td><td>${badge}</td><td>${winner}</td>
        <td>${gm?.duration_sec ?? "—"}s</td>
        <td><a href="#/run/${runId}/${l}">상세 보기</a></td></tr>`;
    }).join("");
    app.innerHTML = `
      <div class="card"><h2>실행 ${runId} <span class="small">${st.state ?? ""}</span></h2>
        <div class="muted">문제: ${esc(st.question_id ?? d.input.question_id ?? "(직접 입력)")} · 소스: ${esc(st.source ?? "-")}</div>
        ${st.error ? `<p style="color:#b3261e">${esc(st.error)}</p>` : ""}
        <table class="compare"><tr><th>언어</th><th>게이트</th><th>승자</th><th>번역 시간</th><th></th></tr>${langRows}</table>
      </div>
      <div class="card"><h3>진행 로그</h3><div class="log">${(st.log || []).map(esc).join("\n")}</div></div>`;
  };
  await render();
  pollTimer = setInterval(render, 2000);
}

function renderLangDetail(d, runId, lang) {
  clearInterval(pollTimer);
  const ev = d.eval[lang] || {};
  const gm = d.gemma[lang] || {};
  const base = d.baseline[lang];
  const v = ev.verdict;
  const rtl = lang === "ur" ? "rtl" : "";
  const colText = (comps) => (comps || []).map(c => md(c.content)).join("<hr>");

  const gateBadges = gm.gates ? `
    <span class="badge ${gm.gates.gate_b.passed ? "pass" : "fail"}">Gate B ${gm.gates.gate_b.passed ? "통과" : "실패"}</span>
    <span class="badge ${gm.gates.gate_c.passed ? "pass" : "fail"}">Gate C ${gm.gates.gate_c.passed ? "통과" : "실패"}</span>
    ${gm.gate_c_feedback_retried ? '<span class="badge neutral">피드백 재번역 수행</span>' : ""}
    ${gm.gates.gate_c.results.filter(r => !r.passed).map(r =>
      `<div class="muted">✗ ${r.name}: ${esc(r.detail)}</div>`).join("")}` : "";

  const gdRow = (side, gd) => gd ? `<tr><td>${SIDE_LABEL[side]}</td>
      <td><b>${gd.score}</b> / 임계 ${gd.threshold} ${gd.passed ? "✅" : "❌"}</td>
      <td>${esc(gd.notes ?? "")} ${(gd.issues || []).map(i => `<div class="muted">• ${esc(JSON.stringify(i))}</div>`).join("")}</td></tr>` : "";

  const blind = ev.blind;
  const axesRows = (blind?.raw?.axes || []).map(a => {
    const side = a.winner === "tie" ? "tie" : blind.assignment[a.winner];
    return `<tr><td>${esc(a.axis)}</td><td class="winner-${side}">${SIDE_LABEL[side] ?? a.winner}</td>
            <td>${esc(a.evidence)}</td></tr>`;
  }).join("");
  const personaRows = (blind?.raw?.persona_round || []).map(p => {
    const side = p.pick === "tie" ? "tie" : blind.assignment[p.pick];
    return `<tr><td>${esc(p.persona)}</td><td class="winner-${side}">${SIDE_LABEL[side] ?? p.pick}</td>
            <td>${esc(p.rationale)}</td></tr>`;
  }).join("");
  const critical = blind?.raw?.critical_errors
    ? Object.entries(blind.raw.critical_errors).map(([k, errs]) =>
        errs.length ? `<div><b>${SIDE_LABEL[blind.assignment[k]] ?? k}</b>: ${errs.map(esc).join(" / ")}</div>` : "").join("")
    : "";

  app.innerHTML = `
    <div class="card">
      <a href="#/run/${runId}">← 실행 개요</a>
      <h2>${LANG_NAMES[lang]}(${lang}) 비교 결과</h2>
      ${v ? `<p>승자: <span class="winner-${v.winner}" style="font-size:20px">${SIDE_LABEL[v.winner]}</span></p>
      <div class="logic-box"><b>판정 논리</b> (경로: ${v.path})\n${esc(v.logic)}</div>` : "<p>평가 대기 중</p>"}
      <p style="margin-top:8px">
        <button class="secondary" onclick="actRetranslate('${runId}','${lang}')">번역 재실행</button>
        <button class="secondary" onclick="actEvaluate('${runId}','${lang}')">평가 재실행</button></p>
    </div>
    <div class="card"><h3>번역 3열 비교</h3>
      <div class="grid3">
        <div><h4>EN 원문</h4><div class="md">${colText(d.en)}</div></div>
        <div><h4>DB에 저장된 기존 파이프라인 번역</h4>
          <div class="md ${rtl}">${base ? colText(base.components) : "<em>기준선 없음</em>"}</div></div>
        <div><h4>gemma4:12B</h4><div class="md ${rtl}">${colText(gm.components)}</div></div>
      </div></div>
    <div class="card"><h3>게이트 (구조·결정적 검사)</h3>${gateBadges || "—"}</div>
    <div class="card"><h3>Gate D 채점 <span class="small">judge: claude-sonnet-4-6 · translator_judge.md · 양측 신규 채점</span></h3>
      <table class="compare"><tr><th>대상</th><th>점수</th><th>심사평·이슈</th></tr>
        ${gdRow("baseline", ev.gate_d?.baseline)}${gdRow("gemma", ev.gate_d?.gemma)}</table>
      ${v?.gate_d_delta != null ? `<p>Δ(gemma − 기존) = <b>${v.gate_d_delta}</b></p>` : ""}</div>
    <div class="card"><h3>블라인드 비교 판정 <span class="small">judge: opus · 6축 + 페르소나 3인 · 출처 은닉 후 무작위 A/B</span></h3>
      ${blind ? `
        <p>배정 공개: A=${SIDE_LABEL[blind.assignment.A]}, B=${SIDE_LABEL[blind.assignment.B]}
           · confidence: ${esc(blind.raw?.confidence ?? "-")}</p>
        ${blind.error ? `<p style="color:#b3261e">판정 오류: ${esc(blind.error)}</p>` : ""}
        <h4>6축 비교표</h4>
        <table class="compare"><tr><th>축</th><th>우세</th><th>근거</th></tr>${axesRows}</table>
        <h4>페르소나 라운드</h4>
        <table class="compare"><tr><th>페르소나</th><th>선택</th><th>근거</th></tr>${personaRows}</table>
        ${blind.raw?.steelman_loser ? `<p><b>Steelman(패자 옹호):</b> ${esc(blind.raw.steelman_loser)}</p>` : ""}
        ${critical ? `<h4>치명 오류</h4>${critical}` : ""}` : "<em>기준선이 없어 블라인드 비교 생략</em>"}</div>
    <div class="card"><h3>측정 메타</h3>
      <p>번역 시간 ${gm.duration_sec ?? "—"}s · 컴포넌트 ${gm.components?.length ?? 0}개 ·
         재시도 합계 ${(gm.meta || []).reduce((a, m) => a + (m.attempts || 0), 0)}회</p></div>`;
}

window.actRetranslate = async (runId, lang) => { await post(`/api/runs/${runId}/languages/${lang}/retranslate`); viewRun(runId, lang); };
window.actEvaluate = async (runId, lang) => { await post(`/api/runs/${runId}/languages/${lang}/evaluate`); viewRun(runId, lang); };

/* --- 실행 목록 --- */
async function viewRuns() {
  const runs = await api("/api/runs");
  app.innerHTML = `<div class="card"><h2>실행 목록</h2>
    <table class="compare"><tr><th>run</th><th>상태</th><th>문제</th><th>언어</th></tr>
    ${runs.map(r => `<tr><td><a href="#/run/${r.run_id}">${r.run_id}</a></td>
      <td>${r.state}</td><td>${esc(r.input.question_id ?? "(직접 입력)")}</td>
      <td>${(r.languages || []).join(", ")}</td></tr>`).join("")}</table></div>`;
}

/* --- 화면 4: 대시보드 --- */
async function viewDashboard() {
  const agg = await api("/api/aggregate");
  const langs = Object.keys(agg.by_language);
  const rows = langs.map(l => {
    const s = agg.by_language[l], dep = agg.deployable[l];
    return `<tr><td>${LANG_NAMES[l] ?? l}(${l})</td>
      <td>${s.wins}승 ${s.ties}무 ${s.losses}패${s.unknown ? ` (미확정 ${s.unknown})` : ""}</td>
      <td>${s.avg_gate_d_gemma ?? "—"} vs ${s.avg_gate_d_baseline ?? "—"} (Δ ${s.avg_delta ?? "—"})</td>
      <td>${s.gate_fail_count}/${s.n}</td><td>${s.avg_duration_sec ?? "—"}s</td>
      <td class="winner-${dep.deployable ? "gemma" : "baseline"}">${dep.deployable ? "가능" : "불가"}</td></tr>`;
  }).join("");
  const depDetails = langs.map(l => {
    const dep = agg.deployable[l];
    return `<div class="card"><h4>${LANG_NAMES[l] ?? l}(${l}) — 실전 투입 ${dep.deployable ? "✅ 가능" : "❌ 불가"}</h4>
      <table class="compare"><tr><th>조건</th><th>충족</th><th>실제</th><th>요구</th></tr>
      ${dep.conditions.map(c => `<tr><td>${esc(c.name)}</td><td>${c.passed ? "✅" : "❌"}</td>
        <td>${esc(c.actual)}</td><td>${esc(c.required)}</td></tr>`).join("")}</table></div>`;
  }).join("");
  app.innerHTML = `
    <div class="card"><h2>대시보드 <span class="small">모든 실행의 (문제×언어) 셀 집계</span></h2>
      <div class="rules-box">${esc(agg.rules_text)}</div><br>
      <table class="compare"><tr><th>언어</th><th>블라인드 전적(gemma 기준)</th>
        <th>Gate D 평균 (gemma vs 기존)</th><th>게이트 실패</th><th>평균 번역 시간</th><th>실전 투입</th></tr>
      ${rows || "<tr><td colspan=6>아직 평가된 실행이 없습니다</td></tr>"}</table></div>
    ${depDetails}`;
}

/* --- 라우터 --- */
function route() {
  clearInterval(pollTimer);
  const h = location.hash || "#/";
  const m = h.match(/^#\/run\/([^/]+)(?:\/([a-z]{2}))?$/);
  if (m) return viewRun(m[1], m[2]);
  if (h === "#/runs") return viewRuns();
  if (h === "#/dashboard") return viewDashboard();
  return viewNewRun();
}
window.addEventListener("hashchange", route);
refreshHealth(); setInterval(refreshHealth, 10000);
route();
```

- [ ] **Step 4: 수동 스모크 (외부 호출 없음)**

```bash
.venv/bin/python -m uvicorn server.app:app --port 8765 &
sleep 2
curl -s http://127.0.0.1:8765/ | grep -q "Gemma Arena" && echo UI_OK
curl -s http://127.0.0.1:8765/static/app.js | head -1
curl -s http://127.0.0.1:8765/api/health
kill %1
```

Expected: `UI_OK` 출력, app.js 첫 줄 주석, health JSON (supabase_env false 여도 정상 — direnv 없이 띄웠으므로)

- [ ] **Step 5: Commit**

```bash
git add ui/
git commit -m "feat: UI — 3열 비교·게이트 배지·Gate D 카드·블라인드 판정 카드·판정 논리 박스·대시보드"
```

---

### Task 10: 실데이터 스모크 → 전체 실행

**Files:** 없음 (검증 태스크). 발견된 버그는 이 태스크에서 수정 후 커밋.

- [ ] **Step 1: 서버를 실환경으로 기동**

```bash
cd /Users/cokoroad/translator-test && ./run.sh &
sleep 5
curl -s http://127.0.0.1:8765/api/health
```

Expected: `"ollama": {"ok": true, ...}`, `"supabase_env": true`, `"claude_cli": true`. 하나라도 false면 UI의 hint대로 원인 해결 후 재시도 (direnv allow, gcloud 로그인, ollama serve).

- [ ] **Step 2: 실제 문제 확보**

```bash
curl -s http://127.0.0.1:8765/api/samples
```

Expected: EN 해설이 있는 question_id 목록. 첫 항목의 question_id를 이후 스텝에 사용.

- [ ] **Step 3: 1문제 × 1언어(vi) 스모크 실행**

```bash
curl -s -X POST http://127.0.0.1:8765/api/runs \
  -H "Content-Type: application/json" \
  -d '{"question_id": "<Step 2의 UUID>", "languages": ["vi"]}'
# 이후 상태 폴링:
curl -s http://127.0.0.1:8765/api/runs/<run_id> | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['status']['state']); print(*d['status']['log'][-5:], sep='\n')"
```

Expected (수 분 소요): state가 `translating → evaluating → completed`로 전이. 완료 후:
- `runs/<run_id>/gemma/vi.json`에 번역 + 게이트 결과
- `runs/<run_id>/eval/vi.json`에 gate_d 양측 점수·blind(assignment·raw.axes 6개·persona_round 3개)·verdict(logic 문장)
- manifest.json에 모델 digest·프롬프트 해시·편차 기록

실패 시 여기서 디버깅·수정·커밋 후 재실행 (superpowers:systematic-debugging).

- [ ] **Step 4: 브라우저 확인**

`open http://127.0.0.1:8765/#/run/<run_id>/vi` — 3열 비교(마크다운 렌더), 게이트 배지, Gate D 카드, 블라인드 카드(6축 표·페르소나·배정 공개), 판정 논리 박스가 모두 보이는지. `#/dashboard`에서 vi 집계·deployable 조건표 확인.

- [ ] **Step 5: 8개 언어 전체 실행**

UI 새 실행 폼에서 같은 문제로 8개 언어 전체 실행 (또는 curl로 `"languages"` 생략). 소요: 번역 8×(컴포넌트 수×수십 초) + 평가 8×3회 Claude 호출. 완료 후 대시보드에서 언어별 승패·deployable 확인. ur 상세에서 RTL 렌더링 확인.

- [ ] **Step 6: 최종 검증 + 커밋**

```bash
.venv/bin/python -m pytest tests/ -v   # 전체 회귀
git add -A && git commit -m "fix: 실데이터 스모크에서 발견된 이슈 수정"  # 변경이 있는 경우만
```

superpowers:verification-before-completion 스킬로 완료 검증 후 사용자에게 실행 URL과 사용법 보고.

---

## Self-Review 결과

- Spec 커버리지: §2(공정성 5원칙→Task 4·5·6·7 매니페스트), §3(구조→Task 1–9), §4(입력 3종+붙여넣기→Task 7 `_load`, Task 9 폼), §5(플로우→Task 7), §6(각색 프롬프트→Task 6), §7(판정 논리→Task 3, RULES_TEXT), §8(UI 요구 전부→Task 9), §9(API→Task 8), §10(매니페스트→Task 7), §11(에러→각 태스크 + health), §12(테스트→각 태스크), §13(범위 외 준수) — 커버 확인.
- 타입 일관성: `eval/<lang>.json` 스키마(Task 7 산출 ↔ Task 3 소비 ↔ Task 8 aggregate ↔ Task 9 렌더) 필드명 대조 완료 (`gates.passed`, `gate_d.{baseline,gemma}.score`, `blind.assignment/winner_side/raw`, `verdict.{winner,path,logic,gate_d_delta}`).
- 알려진 리스크(실행 중 확인): ① `local_agent` import 시 추가 의존성 발견 가능 → Task 1 Step 1 폴백 절차. ② Task 8 aggregate 테스트 기대값 주의사항 명시. ③ 실 Supabase 컬럼명 차이는 Task 10 스모크에서 드러남 — 그 시점에 수정.
