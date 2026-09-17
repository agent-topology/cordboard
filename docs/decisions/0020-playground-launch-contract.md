# ADR-0020: 플레이그라운드 아티팩트, 소유권, 런치 계약

**Status:** Accepted
**Date:** 2026-09-17
**Deciders:** 사용자 — Slice 6 첫 Task(#66) 계약 확정
**관련:** ADR-0016 (control plane/Testbed 경계), ADR-0018 §7 (`cord serve` 시작 명령과
바인딩 경계), ADR-0014 (그래프 비묘사 — 연결 단위 설정만)

## Context

[#65](https://github.com/agent-topology/cordboard/issues/65)는 첫 사용 조작자가
provider credential이나 Testbed 내부 지식 없이 하나의 지원되는 경로로 idle topology·성공
실행·실패 후 재시도·대기 중 승인 네 시나리오가 채워진 loopback 뷰어에 도달할 것을
요구한다. [#66](https://github.com/agent-topology/cordboard/issues/66)은 그 경로의
정확한 진입 명령, artifact 경계, 소유권을 구현 전에 확정하는 이 Task다.

현재 실측 가능한 사실 셋이 이 결정의 입력이다.

1. **`cord serve`는 이미 있다** (ADR-0018 §7, `src/cord_runtime/web/server.py:413-436`,
   `cli.py:488-496`). loopback IPv4만 받고, 실제 URL을 표준출력에 찍고, `KeyboardInterrupt`로
   깨끗하게 멈춘다. 이 ADR은 이 계약을 바꾸지 않는다 — 그 앞에 무엇을 채워 넣을지만 정한다.
2. **성공/재시도/승인 시나리오를 채우려면 실제 Aegra가 필요하다.** `cord_runtime`이 갖는
   유일한 `ExecutionBackend`는 Aegra HTTP API이고([docs/aegra.md](../aegra.md)), Aegra
   0.10.4는 checkpoint에 Postgres를 쓴다(`examples/aegra_server.py`). ADR-0016 §1·§3은
   Cordboard가 production Aegra entity를 직접 운영하는 것을 이미 금지했다 — 이 사실은
   이번에 새로 발견된 것이 아니라 그 결정이 이 문제에 그대로 적용된 결과다.
3. **거의 다 만들어져 있다.** `cordboard-testbed`의 `environments/minimal/`
   (`aegra.json`, `collector.yaml`, `graphs/deterministic.py`, `graphs/interrupt.py`)과
   `scripts/start-environment`가 이미 Postgres(Docker Compose)와 Aegra를, 선택적으로
   pin된 Collector 바이너리를 기동해 "Minimal environment ready; Ctrl-C to stop"까지
   출력한다. `scripts/verify-browser-artifact.py`는 그 위에서 `cord` CLI만 호출해
   idle 두 alias·success·retry·interrupt 네 시나리오를 실제로 채우고 Playwright로 검증한다
   — 이슈 본문 Context가 지적한 "Testbed의 완전한 설정은 원하는 제품 상호작용이 아니다"는
   이 스크립트가 **조작자용이 아니라 CI 단언(assertion) 스크립트**라는 뜻이지, 조립 자체가
   틀렸다는 뜻이 아니다: 매 실행마다 새 evidence 디렉터리를 강제하고, 검증 후 즉시
   프로세스를 종료하며, 사람이 아니라 headless Playwright가 브라우저를 대신 조작한다.

즉 이 Task가 결정할 것은 "Aegra/Postgres/Collector를 누가 소유하는가"(이미 ADR-0016이
답했다)가 아니라, **이미 소유권이 정해진 조각들을 조작자가 실제로 실행할 수 있는 하나의
명령 순서로 어떻게 조립하는가**다. 이 결정은 ADR-0016·0018을 대체하거나 수정하지 않는다.

## Decision

### 1. Artifact 경계 — 두 개의 설치 아티팩트, 하나가 다른 하나를 감싸지 않는다

| Artifact | 저장소 | 소유 |
| --- | --- | --- |
| `cord-runtime` wheel | `cordboard` 릴리스 또는 명시적 커밋 SHA 빌드 | Cordboard — CLI, viewer, backend client, 공개 계약 |
| Testbed 체크아웃 + pin된 공개 바이너리(otelcol-contrib, redact-secret) + Docker | `cordboard-testbed` | Testbed — 실제 Aegra/Postgres/Collector 프로세스, 픽스처 그래프 |

Cordboard 쪽 artifact 설치 입력은 ADR-0016 §3과 동일하다(릴리스 package, 명시적 Git
commit SHA, 또는 CI/build wheel; `path=".."`/editable 설치 금지). 새 규칙을 추가하지
않는다.

### 2. 진입 명령 — Testbed가 조립하고, Cordboard의 공개 CLI만 호출한다

```sh
# 1) 후보 설치 (기존 cordboard-testbed 계약, 변경 없음)
python3 scripts/install-wheel /absolute/path/cord_runtime-<version>-py3-none-any.whl \
  --sha256 <64-character-build-sha256>
.venv/bin/python scripts/prepare-tools     # 두 pin된 공개 바이너리 해시 검증 + 추출

# 2) 신규 — Testbed 소유, 이번 Task에서 구현하지 않음(#67의 몫)
.venv/bin/python scripts/run-playground --evidence .artifacts/playground
```

`run-playground`는 새 스크립트이지만 새 메커니즘을 도입하지 않는다 — 기존
`scripts/start-environment`(Postgres+Aegra(+Collector) 기동, 포트 점유 검사, 신호
처리, `docker compose stop`)를 그대로 호출한 뒤, `scripts/verify-browser-artifact.py`가
이미 증명한 것과 같은 절차(설치된 `cord` CLI만 호출 — 내부 함수 import 없음)로 세 커넥션을
등록하고 두 번의 실행을 제출하고, 마지막에 Playwright 대신 **`cord serve`를 포그라운드로
실행**해 사람이 탐색할 세션을 연다. 새 조립 로직이나 새 도메인 판단을 이 스크립트가 만들지
않는다 — §3~§5가 그 이유다.

### 3. 프로세스·픽스처·board·archive·정리(cleanup) 소유권

| 대상 | 소유자 | 근거 |
| --- | --- | --- |
| Aegra 프로세스, Postgres(Docker Compose), Collector 프로세스 | `run-playground`(Testbed) | ADR-0016 §3 — 실프로세스는 본체가 흡수하지 않는다 |
| `deterministic`/`interrupt` 픽스처 그래프의 노드·비즈니스 내용 | `environments/minimal/graphs/`(Testbed) | AGENTS.md — Cordboard는 그래프를 묘사하지 않는다; synthetic entity가 자기 그래프를 소유(ADR-0016 §4) |
| idle-전용 topology 발행자(실행 없이 `/.well-known/agent-topology.manifest.json`만 응답) | `run-playground`(Testbed) — `verify-browser-artifact.py`의 `IdlePublisher`를 독립 모듈로 추출, 재사용 | 실행이 전혀 필요 없는 시나리오에 Aegra를 새로 붙이는 것보다 싸다 |
| `--board` 디렉터리 | 매 `run-playground` 호출마다 `--evidence` 아래 새로 생성 | 형식은 Cordboard 소유(`cord_runtime.connections`), 위치·수명은 Testbed 소유 |
| `--archive` 디렉터리 | `run-playground`가 관리하는 고정 경로(`.artifacts/playground/archive`), 재실행 시 재사용 | §4에서 근거 설명 |
| 정리(Ctrl-C) | `run-playground` → 이미 구현된 `start-environment`의 신호 처리(소유 프로세스 terminate → `docker compose stop`, checkpoint volume 보존)를 그대로 상속 | 새 정리 로직을 만들지 않는다 |

### 4. 재실행 시맨틱 — archive는 고정 경로로 재사용, evidence 메타데이터는 매번 새로 만든다

기존 `start-environment --evidence`는 acceptance 세션마다 **새 디렉터리를 강제**한다(감사
가능성이 acceptance의 목적이므로 옳다). 플레이그라운드는 다른 목적 — 조작자가 같은 명령을
두 번째 실행할 때 매번 새 경로를 지어내야 한다면 "하나의 진입 명령"이 아니다. 그래서
`run-playground`는 두 가지를 분리한다: 세션 메타데이터(`session.json`, candidate
identity)는 여전히 매 실행 새 하위 디렉터리에 쓰지만, **`--archive`가 가리키는 실행 기록
디렉터리 자체는 고정되고 append-only로 재사용**된다(ADR-0005의 append-only 아카이브
시맨틱과 일치). 재실행은 이전 세션의 Run 기록을 지우지 않고, Postgres checkpoint volume도
`start-environment`가 이미 보존하므로 이전 Thread도 그대로 남는다.

### 5. 네 시나리오와 관측 가능한 상태 — 그래프 비즈니스 데이터를 규정하지 않는다

| 시나리오 | 조작 | 뷰어에서 보이는 것 |
| --- | --- | --- |
| Idle topology | 실행이 전혀 없는, topology만 발행하는 alias 둘(같은 `graph_id`를 광고) | 두 개의 독립된 `/connections/{alias}/graphs/{graph_id}` 페이지, 각각 "No recorded Runs", 같은 published topology SVG |
| 성공 실행 | 결정적 그래프를 "즉시 성공"으로 해석되는 입력으로 1회 실행 | Execution timeline에 Step 1개·Attempt 1개(`passed`) |
| 실패 후 재시도 | 같은 그래프를 "1회 실패 후 재시도로 성공"으로 해석되는 입력으로 실행 | 같은 Step 아래 Attempt 2개, outcome이 `["failed", "passed"]` |
| 대기 중 승인 | `interrupt()`를 갖는 그래프를 실행해 일시정지 상태로 둔다(응답 제출하지 않음) | Live 진단 영역에 "Live identity and status only."와 `ingestion_pending`(픽스처가 텔레메트리를 내지 않으므로 완료된 아카이브 레코드는 만들지 않는다 — 기존 `verify-browser-artifact.py` 범위와 동일) |

"성공/실패 후 재시도로 해석되는 입력"이라고만 적는다 — 정확한 JSON 페이로드나 노드 이름은
이 Task가 아니라 이미 존재하는 픽스처 그래프(§3)와 §2의 구현 Task 몫이다. 이 표는 #65 AC2를
그대로 좁혀 옮긴 것이며 새 시나리오를 추가하지 않는다.

### 6. Docker와 pin된 공개 바이너리 — 전제조건으로 명시, 숨기지 않는다

§2의 명령 순서가 그대로 답이다: `docker`, 두 개의 해시 검증된 바이너리(otelcol-contrib,
redact-secret)는 눈에 보이는 사전 단계로 문서화된다. `run-playground`가 이들을 몰래
다운로드하거나 자동 설치하지 않는다 — ADR-0016 §3이 이미 세운 "artifact hash와 실제 설치
버전을 증거로 남긴다" 원칙과 같다. Collector/redact-secret 없이 Aegra+Postgres만 띄우는
축소 경로(§7 각주)는 존재하되 기본값이 아니다 — 그 경로는 archive gate를 거치지 않으므로
Run 타임라인이 채워지지 않고 identity/status만 보이는, "채워진 뷰어"에 못 미치는 결과를
내기 때문이다.

### 7. 실패 모드 — 새 처리 로직 없이 기존 것을 상속한다

포트 점유(55432/52026/54318), 이미 실행 중인 Compose 세션, 새로 만들지 않은 evidence
디렉터리는 `start-environment`가 이미 조작자에게 바로 실패 이유를 알려주고 아무 프로세스도
남기지 않는다(§3 표 인용). `run-playground`는 이 위에 새 실패 분류를 추가하지 않는다 —
Docker daemon 부재나 누락된 pin 바이너리도 같은 스크립트가 이미 종료 시 명확한 에러로
드러낸다. `cord serve` 자체의 실패(잘못된 host/port)는 ADR-0018 §7의 기존 검증을 그대로
쓴다.

## Options Considered

1. **Cordboard 자체 서브커맨드(`cord playground`)가 Docker/Aegra/Postgres까지 구동.**
   기각 — ADR-0016 §1·§3이 이미 금지한 "본체가 production Aegra entity를 운영"하는
   모양을 그대로 재현한다.
2. **Cordboard 내부에 Docker 없는 가짜 in-memory 백엔드를 새로 만들어 데모.** 기각 —
   ADR-0016 §2 "가짜 동등성을 만들지 않는다"와 정면 충돌하고, #65가 요구하는 "안전하지만
   정직한(honest)" 경로에 어긋난다 — 실제로 설치할 제품과 다른 경로를 보여주는 셈이다.
3. **`scripts/verify-browser-artifact.py`를 그대로 조작자 진입점으로 재사용.** 기각 —
   assertion 전용(실패 시 예외로 죽는다), 매 실행 새 evidence 디렉터리 강제, headless
   Playwright가 브라우저를 대신 조작해 사람이 남아서 탐색할 세션이 없다.
4. **전체 `scripts/run-acceptance` 매트릭스를 조작자 진입 경로로 지정.** 기각 — multi-entity/
   failure-lab까지 포함한 12개 이상의 실프로세스 검사와 해시 고정 툴, JUnit/스크린샷
   evidence 보존은 이슈 본문 Context가 명시한 "desired product interaction"이 아니다.
5. **채택 — 기존 `start-environment` 조립 위에 얇은 `run-playground`를 추가해 세 커넥션·두
   실행을 공개 `cord` CLI로만 채우고 `cord serve`를 포그라운드로 남긴다.** 새 도메인 로직이
   없고, 소유권 변경도 없으며, §5의 네 상태를 이미 존재하는 두 픽스처 그래프만으로 만족한다.

## Trade-off Analysis

`run-playground`는 `start-environment`와 `verify-browser-artifact.py` 사이에 세 번째
조립 스크립트를 추가한다는 비용이 있다 — 셋 다 유지보수 대상이 된다. 그러나 각각의 책임이
분리돼 있다: acceptance(감사 가능성 우선) vs CI 단언(자동 검증 우선) vs 조작자 진입(탐색
가능한 세션 우선). 하나로 합치면 acceptance의 "매번 새 디렉터리" 요구와 플레이그라운드의
"같은 명령 재실행" 요구가 충돌한다. Docker/pin 바이너리를 여전히 전제조건으로 요구하는
비용은 실제로 존재한다 — 이는 "완전히 무료로 데모 가능"을 포기하는 대가지만, 대신 뷰어가
보여주는 것이 실제로 설치될 제품의 실제 경로라는 정직성을 얻는다.

## Consequences

- 이 ADR은 ADR-0016·ADR-0018의 소유권/시작 계약을 변경하지 않는다 — 그 경계 안에서
  하나의 조작자 진입 명령을 조립하는 방법만 확정한다.
- 코드 구현은 이 ADR에 포함되지 않는다. `run-playground`, 독립 `IdlePublisher` 모듈,
  고정 archive 경로 처리는 [#67](https://github.com/agent-topology/cordboard/issues/67)의
  몫이며 `cordboard-testbed` 저장소에 구현된다 — `cord-runtime` 쪽 코드 변경은 없다.
- [#68](https://github.com/agent-topology/cordboard/issues/68)은 §2의 문서화된 명령
  순서 자체를 headless로 재현해 §5의 네 상태에 도달하는지 검증한다(black-box qualification).
  새 acceptance 매트릭스를 발명하지 않고 §2의 명령을 그대로 구동한다.
- `docs/playground.md`가 이 ADR의 §1–§7을 조작자용 실행 가이드로 옮긴다(구현 전이므로
  "planned" 상태로 표시).

## Action Items

1. [x] ADR 작성 및 DECISIONS.md 색인 등록 (본 변경)
2. [ ] `cordboard-testbed`에 `scripts/run-playground` 구현 (§2·§3·§4) — #67
3. [ ] `verify-browser-artifact.py`의 `IdlePublisher`를 독립 모듈로 추출해 양쪽에서
   재사용 (§3) — #67
4. [ ] §2 명령 순서의 headless 재현으로 흑백 qualification (§5) — #68
5. [ ] `docs/playground.md` 최초 실행 evidence로 "planned" 표시 제거 — #67 완료 후
