# ADR-0018: 브라우저 관측 표면 — 서버/프론트엔드 스택, 공개 read/update 라우트, 접근성 시각 상태 확정

**Status:** Accepted
**Date:** 2026-09-16
**Deciders:** 사용자 — 2026-09-16 명시적 승인
**관련:** ADR-0016 (control plane/Testbed 경계), ADR-0013·0014·0015 (교환원 경계, 그래프
비묘사, 연결 비차단), ADR-0006 (마스킹 강제 지점), ADR-0009·0011 (topology 문서/드리프트),
ADR-0012 (topology spec 분리), ADR-0017 (identity/status 계약, 이 ADR과 동일한
"readiness gate 선행 ADR" 패턴)

## Context

[#48](https://github.com/agent-topology/cordboard/issues/48)은 `planning:backlog`이며
본문 Blockers and handoff가 명시한다: "Backlog until identity and live read contracts
land and the web stack/startup contract is documented." 의존 이슈 #43(Deployment
identity/scoping)과 #46(continuous live reconciliation)은 이미 머지됐다
(`workbench/43-deployment-graph-identity`, `workbench/46-continuous-live-reconciliation`).
남은 유일한 선행 조건은 이 이슈 자신의 본문이 요구하는 것 — "Before ready, settle the
frontend/server choice, public read/update shapes and accessible visual states in this
Task" — 뿐이다. 이는 #42가 ADR-0017 없이는 `planning:ready`로 전환될 수 없었던 것과 같은
패턴이다.

오늘 시점에 브라우저로 서빙되는 패키지형 프런트엔드, loopback 서버, 시작 명령은
존재하지 않는다 — [ARCHITECTURE.md:60](../../ARCHITECTURE.md)이 명시: "No `cord` CLI or
web surface is added by this slice; that is still planned." [ARCHITECTURE.md:170]도
"#47(a real web viewer over the same shared read model) remains separate and
unimplemented"라고 확정한다. 존재하는 것은 텍스트/JSON 뿐인 `cord view`
(`src/cord_runtime/viewer.py`, [docs/viewer.md](../viewer.md))이며, 이슈 본문 Context가
지적하듯 "Existing text/JSON output is not visual UI evidence."이다.

`pyproject.toml`에는 웹 프레임워크나 JS 툴체인이 전혀 없다 — `dependencies`는
`agent-topology-spec`/`langgraph`/`redact-secret`/OTel/`requests`/`pyyaml`/
`langchain-core`뿐이고 `[dependency-groups]`는 `dev`(pytest)와 `proxy`(litellm)뿐이다.
즉 프런트엔드/서버 스택은 완전히 무에서 결정해야 하는 선택이며, 이 ADR의 범위다.

`cord_runtime.viewer.build_catalog`가 이미 이 문제가 필요로 하는 shared read model 전부를
구현하고 있다: 연결/토폴로지/기록된 실행/라이브 실행을 하나의 JSON-직렬화 가능한 dict로
합치고(`cli.py`의 `render()`가 이미 `json.dumps(catalog)`로 이를 증명한다,
`cli.py:273-286`), `topology_status`를 `absent|unreachable|invalid|stale|current|
not_checked|no_connection|ambiguous`로 명시적으로 분류하며([docs/viewer.md](../viewer.md)
Topology correlation 표), `unmatched_node_names`로 상관되지 않은 실행 증거를 보존하고,
`build_execution_tree`가 Run/Step/Attempt 경계·`approval_wait_ns`·`timeline_incomplete`·
`awaiting_resume`를 계산해 둔다([docs/viewer.md](../viewer.md#execution-timeline-fields-46-ac5)).
`cord_runtime.live_reconciliation.reconcile`은 `live|ingestion_pending|ingestion_failed`
세 가지 라이브 source를 이미 모델-프리하게 계산한다. **이 ADR이 결정할 것은 이 값들을
브라우저에 어떻게 실어 나르고 그릴지이지, 새 도메인 분류 로직이 아니다** — 이슈 본문의
"one source of domain truth" 요구사항이 정확히 이것이다.

## Decision

### 1. 서버: Python 표준 라이브러리 `http.server` 위의 얇은 라우터, 새 웹 프레임워크 없음

`http.server.ThreadingHTTPServer` + 직접 작성한 20줄 내외의 경로 매처. 각 요청은
`cord_runtime.viewer.build_catalog`/`topology.check_freshness`/
`live_reconciliation.reconcile`을 그대로 호출해 응답을 만든다 — 새 상태 저장소, 새 캐시,
새 상관 로직을 두지 않는다.

FastAPI/Flask/Starlette 같은 프레임워크를 들이지 않는 이유: ADR-0016 §2가 이미 "지금
범용 plugin framework나 미사용 adapter를 구현하지 않는다"는 원칙을 세웠고, 이 서버의 실제
표면은 GET 라우트 10여 개 + SSE 스트림 1종뿐이라 프레임워크가 절감해 줄 보일러플레이트가
크지 않다. `cord view --watch`가 이미 스레드 + 공유 큐 패턴으로 동시 라이브 target을
소비하고 있으므로([docs/viewer.md](../viewer.md#live-runs-20)), SSE 연결마다 스레드를 하나
쓰는 이 서버의 동시성 모델은 새 패턴이 아니라 기존 패턴의 연장이다.

새 런타임 의존성은 딱 하나: **Jinja2**(순수 Python, autoescape 지원). 서버 렌더 HTML에
Subject id·node 이름·경고 문자열 등 archive에서 온 값을 꽂을 때 수동 이스케이프를 신뢰하지
않기 위해서다 — Collector 게이트(ADR-0006)가 비밀값은 걸러내지만 임의의 표시 가능한
텍스트까지 걸러내는 것은 아니므로, XSS는 이 계층이 스스로 막아야 한다.

### 2. 프론트엔드: 빌드 스텝 없는 서버 렌더 다중 페이지, JS는 점진적 향상뿐

각 라우트는 실제 이동 가능한 URL이 완전한 HTML을 반환하는 전통적 다중 페이지 앱이다.
SPA 클라이언트 라우터, 번들러, npm/node_modules를 들이지 않는다 — "small dependency
footprint" 요구를 서버뿐 아니라 프런트엔드에도 동일하게 적용한 결과다. JS는
`<script>` 인라인/정적 파일로만 존재하고 두 가지만 한다: (a) 라이브 진단 영역에
`EventSource`로 SSE를 열어 `aria-live` 영역을 갱신, (b) `EventSource`를 못 쓰는 환경을 위한
polling 폴백(`fetch` 반복). 토폴로지 다이어그램도 별도 그래프 라이브러리(D3, cytoscape 등)
없이 서버가 이미 아는 노드/엣지 집합을 레이어드 DAG로 배치해(BFS depth = row, 그 안에서는
document 순서 유지) 인라인 SVG로 그린다 — `agent-topology-spec`이 정의하는 노드/엣지 필드
이름 자체는 이 ADR이 재정의하지 않는다(ADR-0012·AGENTS.md: 포맷은 upstream 소유).

정적 자산(CSS, 두 개 남짓의 JS 파일, Jinja2 템플릿)은 `src/cord_runtime/web/` 아래
패키지 데이터로 두고 `hatchling`의 wheel 빌드가 `src/cord_runtime` 패키지를 그대로
포함하므로(`pyproject.toml` `[tool.hatch.build.targets.wheel]`) 추가 패키징 설정 없이
"packaged frontend" 요구를 만족한다.

### 3. 공개 read 라우트 — `build_catalog`의 entry 모델을 그대로 URL로 노출

`build_catalog`의 항목은 셋 중 하나로 이미 나뉜다 — 등록된 connection(alias, graph_id),
`no_connection`(graph_id만), `ambiguous`(graph_id + 그 graph_id를 광고하는 alias들). 이
세 갈래를 그대로 별개 네임스페이스로 노출해, 같은 `graph_id`를 광고하는 서로 다른
Deployment가 항상 서로 다른 URL로 구분되게 한다(#48 AC1):

| 라우트 | 대응하는 read model | 비고 |
| --- | --- | --- |
| `GET /` | `build_catalog`의 전체 항목 목록 | 인덱스. 실행 기록이 전혀 없는 connection도 항상 나열(기존 `cord view` 동작 그대로) |
| `GET /connections/{alias}/graphs/{graph_id}` | 한 항목(`_graph_view`) | 정상 케이스. `alias`는 `cord_runtime.connections`가 검증한 값 그대로 |
| `GET /graphs/{graph_id}` | `topology_status == "no_connection"` 항목 | 등록된 connection이 없는, 기록으로만 아는 Graph |
| `GET /graphs/{graph_id}/ambiguous` | `topology_status == "ambiguous"` 항목 | 두 개 이상의 alias가 같은 graph_id를 광고 |
| `GET /connections/{alias}/graphs/{graph_id}/runs/{run_id}` | 해당 Run의 tree/timeline | Step/Attempt 트리, `approval_wait_ns`/`timeline_incomplete`/`awaiting_resume` 포함 |

`/graphs/{graph_id}`·`/graphs/{graph_id}/ambiguous`는 `/connections/`가 아니라 별도
최상위 경로다 — `_validate_alias`가 사실상 임의의 비어있지 않은 문자열을 alias로 허용하므로
(`connections.py:85-88`), alias 네임스페이스 안에 `_ambiguous` 같은 예약어를 두면 실제
alias와 충돌할 수 있다. 별개 최상위 경로를 쓰면 이 충돌이 구조적으로 불가능해진다.

각 HTML 라우트는 `Accept: application/json`(또는 `?format=json`)일 때 정확히 같은
`build_catalog`/`_graph_view`/`build_execution_tree`의 dict를 `json.dumps`해 반환한다 —
`cord view --json`이 이미 증명한 것과 동일한 payload 모양이다. 새 JSON 스키마를 이
ADR에서 새로 설계하지 않는다.

### 4. Live 갱신 — 별도 SSE 엔드포인트, catalog 전체 재계산과 분리

"public read/update shapes"의 "update"는 뮤테이션 엔드포인트가 아니다 — #48 Out of scope가
"Submitting executions, approval mutation, ... graph editing"을 명시적으로 제외한다.
여기서 "update"는 **브라우저가 갱신을 받는 방식**만을 뜻한다:

`GET /connections/{alias}/graphs/{graph_id}/runs/{run_id}/events` (SSE, `text/event-stream`)
는 `cord_runtime.live_reconciliation.reconcile`이 그 Run에 대해 계산하는
`live|ingestion_pending|ingestion_failed` 상태가 바뀔 때만 이벤트를 보낸다. 전체 catalog를
매 tick마다 재계산하지 않는다 — 아카이브가 커질수록 비용이 커지는 부분(기록된 Run 트리
전체)과 실제로 자주 바뀌는 부분(한 라이브 Run의 reconciliation 상태)을 분리한다. 연결이
끊기면 Aegra 자체 SSE가 이미 쓰는 것과 같은 계약을 재사용한다 — 단조증가 이벤트 id를
`id:` 필드로 보내고, 브라우저 재연결 시 `Last-Event-ID` 헤더를 그대로 반영해 재연결이
진행 상황을 잃거나 중복시키지 않게 한다([docs/viewer.md](../viewer.md#live-runs-20)의
기존 Aegra reconnect 계약과 동일한 아이디어를 한 계층 위, 서버→브라우저 구간에 적용). 이
SSE 라우트가 끊기거나 서버가 재시작되면 클라이언트는 일반 GET으로 폴백해 최신 스냅샷을
다시 받는다 — standing daemon을 새로 만들지 않는다는 기존 제약([docs/viewer.md](../viewer.md#live-runs-20)
"there is no standing daemon or background poller across separate invocations")을
유지하되, 서버 프로세스 자체의 수명 동안에는 여러 브라우저 탭이 같은 라이브 상태를 공유해
구독할 수 있다.

### 5. 접근성 시각 상태 — 8가지 `topology_status` + 3가지 live source + disconnect/empty를
각각 구분되는, 텍스트로 이름 붙은 상태로 렌더

일반 HTML(빌드 스텝도, SPA 클라이언트 라우터도 없음)이 기본 접근성 baseline이다: 실제
`<a href>` 내비게이션, `<nav>`/`<main>` 랜드마크, skip-to-content 링크, 보이는 focus
outline. 이 위에 이 ADR이 명시적으로 요구하는 것:

- `topology_status`의 8개 값(`absent|unreachable|invalid|stale|current|not_checked|
  no_connection|ambiguous`) 각각은 서로 다른 아이콘+텍스트 라벨을 갖는 별개의 배지로
  렌더한다 — 색만으로 구분하지 않는다("stale"과 "invalid"를 색맹 사용자가 구분할 수
  있어야 한다). `unmatched_node_names`가 있으면 트리/타임라인 옆에 별도로 나열하고
  숨기지 않는다(#48 AC2).
- 라이브 진단 영역은 `aria-live="polite"`(일상적 상태 전환: `live` → `ingestion_pending`
  → 사라짐)와 `role="alert"`(연결 끊김·`ingestion_failed`처럼 사용자가 즉시 알아야 하는
  상태)를 구분해 사용한다(#48 AC4 "disconnect ... states remain legible").
- 빈 상태("no recorded Runs", 등록된 connection 없음)는 빈 테이블이 아니라 명시적 문구로
  렌더한다 — 기존 `cord view`의 텍스트 출력이 이미 하는 것과 같다
  (`docs/viewer.md`의 "no recorded Runs" 예시).
- 좁은 뷰포트: 단일 CSS breakpoint로 다단 레이아웃을 1열로 접는다. 수평 스크롤을 만드는
  고정폭 요소(특히 타임라인/토폴로지 SVG)를 두지 않는다 — 너무 좁으면 SVG가 축소되거나
  세로로 재배치되지, 페이지가 옆으로 스크롤되지 않는다.
- `run/step` 트리는 `<details>/<summary>`(네이티브 접기, 키보드 기본 지원)로 구현한다 —
  커스텀 JS 아코디언을 새로 만들지 않는다.

### 6. 브라우저 테스트/스크린샷: Python `playwright` 패키지, 새 dependency-group

#48 AC5의 "Browser integration tests and screenshot inspection"과 Testbed 핸드오프
코멘트("No fixture-generated replacement frontend or mocked screenshot can satisfy
product acceptance")는 최종 acceptance를 cordboard-testbed(외부 저장소, 설치된
wheel 기준)의 몫으로 이미 명시했다 — ADR-0016의 control plane/Testbed 경계와 일치한다.
이 ADR이 정하는 것은 Cordboard 쪽 로컬 스모크 테스트의 도구뿐: `playwright`(Python
바인딩, npm/Node 불필요)를 새 `[dependency-groups] web-test` 그룹으로 추가한다 — 런타임
wheel에는 포함되지 않는다(`[tool.uv] default-groups`에 넣지 않는다). 로컬 테스트는 실제
DOM assertion(텍스트/역할/aria 속성)과 좁은 뷰포트(예: 375×667) 스크린샷 캡처를 최소
1개 이상의 success/retry/interrupt 픽스처에 대해 수행하지만, 이는 Testbed의 실제 브라우저
acceptance를 대체하지 않는다 — 구현 PR이 그 경계를 다시 흐리지 않아야 한다.

### 7. 시작 명령과 바인딩 경계

`cord serve [alias] [--host 127.0.0.1] [--port 0]` — `cord view`와 같은 위치의 새
서브커맨드. `--host` 기본값은 `127.0.0.1`이고, 이슈 본문이 명시한 "loopback server"
제약을 코드로 강제한다: `0.0.0.0`이나 외부 인터페이스 바인딩은 명시적 플래그로만
가능하며 기본 동작이 아니다. 인증/토큰은 이 슬라이스에서 추가하지 않는다 — loopback
바인딩 자체가 이 슬라이스의 경계이고(AGENTS.md: Cordboard는 로컬 도구), 원격 노출은
별도 후속 이슈의 몫이다. 이 서버는 `archive_query.read_spans`를 통해서만 실행 데이터를
읽으므로 Collector 게이트(ADR-0006)가 이미 적용한 마스킹을 그대로 상속한다 — 이 ADR은
새 마스킹 지점을 만들지 않는다. `cord serve`는 모델/Provider 설정을 요구하지 않는다
(ADR-0013) — `build_catalog`가 이미 model-free이므로 그대로 상속된다.

## Options Considered

1. **FastAPI + React/Vite SPA.** 기각 — npm 툴체인과 번들 빌드 스텝을 uv 전용 Python
   저장소에 통째로 들여온다. "small dependency footprint" 요구와 정면으로 충돌하고,
   ADR-0016 §2의 "지금 범용 프레임워크를 두지 않는다" 원칙과도 맞지 않는다.
2. **htmx + 작은 프레임워크(Flask/Starlette).** 검토했으나 기각 — 라우트 수가 적어
   프레임워크가 줄여줄 보일러플레이트가 작고, htmx는 서버 partial-render 규약을 새로
   설계해야 해 "기존 read model을 그대로 노출"이라는 목표에 프레임워크 자체의 템플릿
   분할 관례가 끼어든다. 표준 라이브러리 + Jinja2만으로 같은 결과를 더 적은 결정으로
   낸다.
3. **완전 정적 페이지 + 클라이언트 전용 polling(SSE 없음).** 기각 — #48 AC4("update
   continuously")를 polling만으로 만족시키려면 간격을 짧게 잡아야 하고, 그러면 매
   polling마다 `build_catalog` 전체를 다시 계산하는 비용이 커진다. SSE로 "무엇이 실제로
   자주 바뀌는가"(라이브 reconciliation 상태)만 분리해 밀어주는 쪽이 §4가 설명한 대로
   더 싸다.
4. **채택 — 표준 라이브러리 HTTP 서버 + Jinja2, 빌드 스텝 없는 다중 페이지 프론트엔드,
   `build_catalog`의 entry 모델을 그대로 라우트로 노출, SSE는 라이브 reconciliation
   상태만 분리해 전달, Python `playwright`로 로컬 스모크 테스트.** 새 런타임 의존성이
   Jinja2 하나뿐이고, 도메인 분류 로직을 한 줄도 새로 만들지 않으며, #48의 다섯 AC를
   모두 기존 read model의 있는 그대로의 노출로 만족시킨다.

## Trade-off Analysis

프레임워크 없이 라우팅/SSE/정적 파일 서빙을 직접 작성하는 비용은, 이 서버의 실제 표면이
GET 라우트 10여 개 + SSE 하나로 작다는 사실과 이 저장소가 이미 스레드+큐 패턴을 검증된
방식으로 갖고 있다는 사실(§1) 앞에서 작다. 반대급부로 얻는 것은: 새 프레임워크의 버전
고정/보안 공지 추적 부담이 없고, "read model이 곧 API 응답"이라는 불변식을 프레임워크의
직렬화/라우팅 관례가 흐리지 않는다. SSE를 라이브 상태 전용으로 좁힌 대가는 recorded 쪽
트리가 바뀐 순간(새 Run이 archive에 막 기록됨)을 폴링 없이는 즉시 반영하지 못한다는
것인데, 이는 §4가 이미 명시한 제약이고 "Live/recorded/ingestion diagnostics" 중
"recorded"까지 실시간 push할 것을 AC4가 요구하지는 않는다(라이브 소스 세 가지 —
`live`/`ingestion_pending`/`ingestion_failed` — 가 그 자체로 recorded로의 전이를
already covers한다).

## Consequences

- 이 ADR이 **Accepted**로 확정되면, #48은 `planning:ready` 전환 조건 중 "필요한 선행
  ADR 결정이 확정" 및 "구현자가 스택/라우트 모양을 다시 결정하지 않아도 된다"를
  만족한다. 라벨은 이 승인과 함께 `planning:backlog`에서 `planning:ready`로 전환한다.
- 후속 구현 PR(들)은 §1–§7의 라우트 표·SSE 계약·시작 명령 시그니처·의존성 목록을 그대로
  옮긴다: `src/cord_runtime/web/`(서버, 라우터, 템플릿, 정적 자산), `cli.py`에 `cord
  serve` 서브커맨드 추가, `pyproject.toml`에 `jinja2` 런타임 의존성과 `web-test`
  dependency-group(`playwright`) 추가.
- §3 라우트 표는 다음 "controls" 슬라이스(승인/뮤테이션 UI)가 안전하게 걸 수 있는
  안정된 URL/템플릿 삽입 지점을 이미 제공한다 — Run 상세 템플릿의 `awaiting_approval`
  Step 옆에 비어 있는, 문서화된 selector(`.cord-action-slot[data-run-id][data-thread-id]
  [data-interrupt-id]`)를 이 구현 PR이 이미 심어 두지만, 그 안에 버튼이나 JS 핸들러를
  넣지 않는다 — 이슈 본문 Out of scope("Submitting executions, approval mutation")를
  침범하지 않기 위해서다.
- 실제 브라우저/DOM/스크린샷 최종 acceptance는 여전히 cordboard-testbed의 몫으로
  남는다 — 이 ADR과 후속 구현 PR은 "설치된 candidate wheel에 대한 실제 브라우저 검증"을
  대체한다고 주장하지 않는다.

## Action Items

1. [x] 사용자 승인에 따라 Status를 Accepted로 변경하고 Deciders를 기록 (2026-09-16)
2. [x] DECISIONS.md 색인에 0018 등록 (본 변경에 포함)
3. [ ] #48 본문/코멘트에 이 ADR을 연결하고 `planning:backlog` → `planning:ready` 전환
4. [x] 구현 PR: `src/cord_runtime/web/`(라우터, `http.server` 핸들러, Jinja2 템플릿,
   정적 CSS/JS, 레이어드 DAG SVG 렌더러)
5. [x] 구현 PR: `cli.py`에 `cord serve` 서브커맨드(§7 시그니처)
6. [x] 구현 PR: `pyproject.toml`에 `jinja2` 의존성, `web-test` dependency-group
   (`playwright`) 추가
7. [x] 구현 PR: §6의 로컬 스모크 테스트(DOM assertion + 좁은 뷰포트 스크린샷, 최소
   success/retry/interrupt 각 1개 픽스처)
8. [ ] 후속 이슈(이번 범위 아님): 승인/뮤테이션 controls를 §3의 action slot에 실제로
   연결하는 작업
9. [ ] 후속 이슈(이번 범위 아님): loopback 밖 원격 노출이 필요해질 경우의 인증/전송
   경계


## 구현 확인과 계약 구체화 (2026-09-16, #48)

사용자가 Status 변경을 승인했다. 본문 Context의 미구현 진술은 ADR 작성 시점의
이력이며, 현재 구현과 검증 범위는 [브라우저 뷰어](../browser-viewer.md)를 따른다.

구현 점검에서 기존 catalog가 노드 ID와 시간 없는 Subject 요약만 노출한다는 점을
확인했다. 공유 모델 자체에 전체 `runs`와 검증된 `topology_structure`를 추가해
CLI/웹이 같은 데이터를 읽는다. 별도 웹 상관 규칙은 만들지 않는다.
아카이브는 기존 `read_active_spans` → `read_spans` 경로로 읽어 첫 실행 전 빈
디렉터리와 쓰기 중인 마지막 줄을 구분한다.

SSE는 `snapshot` 이벤트로 Run/source/diagnostics를 보내고, 재접속은
`Last-Event-ID` 다음 번호의 최신 전체 스냅샷을 받는다. 중간 이벤트의 영구 이력
재생을 보장하지 않는다. 기록 전이는 `recorded`, 소실은 `unavailable`로 명시한다.
정상 GET polling은 새 기록과 연결 변경도 발견하며, SSE tick은 catalog를 재계산하지
않고 archive 파일 변경 시에만 tree를 다시 읽는다. 프로세스 수명 동안 continuity
store와 backend identity/status API로 실행을 관측하며 graph state는 읽지 않는다.

§7의 원격 노출은 후속 인증/전송 경계 작업으로 남긴다. 이번 구현은
IPv4 loopback만 허용하며 외부 바인딩 플래그를 제공하지 않는다.
알 수 없는 action-slot Thread/interrupt 식별자는 빈 값으로 두고 controls 작업이
공개 inbox에서 해소해야 한다. span ID를 interrupt ID로 추정하지 않는다.
