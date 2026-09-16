# ADR-0019: 브라우저 실행 제출/승인 controls — 세션·CSRF 경계, entity 권한 포트, POST 라우트, 중복 제출 방지 확정

**Status:** Accepted
**Date:** 2026-09-16
**Deciders:** 사용자 — 2026-09-16 명시적 승인
**관련:** ADR-0018 (브라우저 관측 표면, 이 ADR이 남긴 `.cord-action-slot`을 이어받음), ADR-0016
(control plane/Testbed 경계), ADR-0013·0014·0015 (교환원 경계, 그래프 비묘사, 연결 비차단),
ADR-0017 (ExecutionBackend 식별자/상태/결과 계약), ADR-0006 (마스킹 강제 지점)

## Context

[#49](https://github.com/agent-topology/cordboard/issues/49)는 `planning:backlog`이며 본문이
명시한다: "planning:backlog — incorporate prerequisites and resolve the explicitly named contract
details before implementation." 선행 이슈 #48·#44·#42는 이미 머지됐다(ADR-0018, 그리고
`submit_response`/`discover_waiting`/`SyntheticEntityAuthBoundary`를 남긴 #44,
`ExecutionBackend`/`execute`/`resume`을 남긴 #42/ADR-0017). 이 ADR이 0018과 동일한 "readiness
gate 선행 ADR" 패턴을 따르는 이유는 이슈 본문 자신이 요구하기 때문이다 — origin/session 방어,
POST 라우트 모양, 이중 제출 방지, 그리고 특히 "Do not accept approver authority from unverified
client fields"를 실제로 어떻게 지키는지는 아직 이 저장소 어디에도 결정된 적이 없다.

ADR-0018 §6 Consequences가 이미 못 박아 둔 것: "§3 라우트 표는 다음 controls 슬라이스가 안전하게
걸 수 있는 안정된 URL/템플릿 삽입 지점을 이미 제공한다" — `page.html`의
`.cord-action-slot[data-run-id][data-thread-id][data-interrupt-id]`(`src/cord_runtime/web/templates/page.html:89,93`)
이 빈 채로 남아 있고, "그 안에 버튼이나 JS 핸들러를 넣지 않는다"가 0018의 범위 경계였다. 이 ADR이
그 슬롯을 실제로 채우는 방법을 정한다.

`submit_response`(`src/cord_runtime/approval_inbox.py:129-195`)와 `discover_waiting`
(`approval_inbox.py:78-126`)은 이미 완전히 구현되어 있지만, 현재 호출자는
`tests/test_approval_inbox.py`뿐이다 — CLI(`cli.py`)에도 `cmd_respond` 같은 커맨드가 없고, 웹에도
연결되어 있지 않다. 즉 이 ADR은 이 두 함수의 **첫 실제 호출자**를 결정하는 자리이고, "Cordboard가
CLI/API와 같은 도메인 연산을 재사용한다"(이슈 Scope)는 요구를 지키려면 그 첫 호출자가 만드는 계약이
나중에 CLI가 같은 함수를 호출해도 그대로 맞아야 한다.

같은 이유로 `EntityAuthBoundary`(`entity_auth.py:43-53`)의 실제 프로덕션 구현도 이 저장소에 아직
없다 — 있는 것은 `SyntheticEntityAuthBoundary`(`entity_auth.py:56-80`, "테스트 전용, 프로덕션
authorization 구현이 아님"이라고 스스로 명시)뿐이다. ADR-0013 §"Cordboard는 요청한 그래프를
연결하고, 모델 선택은 모른다"와 이슈 본문 Context의 "retaining ... entity authority"는 이 ADR이
새 authorization 정책을 Cordboard 안에 구현하는 것을 금지한다 — 결정할 것은 **누가 그 정책을
갖고 있는지 Cordboard가 어떻게 묻는가**이지 정책 자체가 아니다.

`AegraExecutionBackend.list_assistants`(`backends/aegra.py:152-155`, `GET /assistants`)는 이미
존재하는, 지금까지 아무 caller도 없는 capability다 — 이슈 Scope의 "registered Deployment/Assistant
selection"이 정확히 이것을 가리킨다.

## Decision

### 1. Session/CSRF/origin 방어 — 새 프레임워크·로그인 시스템 없이 stdlib만

이 서버는 여전히 loopback 전용(`server.py:123-131`, ADR-0018 §7)이며 인증은 이 슬라이스의 범위가
아니다(0018 §7 "인증/토큰은 이 슬라이스에서 추가하지 않는다"). 그러나 뮤테이션 POST가 처음
생기므로 **cross-origin 요청 위조(CSRF) 방어는 인증과 별개로 필요**하다 — 같은 머신에서 열린
악성 페이지가 브라우저의 자동 쿠키 전송을 이용해 loopback 서버에 조작된 POST를 보낼 수 있는
표준 위협이다.

`make_server`가 프로세스당 한 번 `secrets.token_urlsafe(32)`로 세션 시크릿을 만든다. 모든 응답에
`Set-Cookie: cord_csrf=<secret>; HttpOnly; SameSite=Strict; Path=/`를 싣고(더블서밋 쿠키), 뮤테이션
폼이 있는 모든 GET 페이지(§3의 execute/approvals 상세)는 같은 값을 숨은 `csrf_token` 필드로
렌더한다. 모든 POST는 두 조건을 **둘 다** 통과해야 한다:

1. `Origin` 헤더가 존재하고 정확히 `http://{host}:{port}`와 같아야 한다. 없으면 거부한다 — 이
   표면은 브라우저 UI 전용이고, 비브라우저 호출자는 이미 있는 공개 Python API/CLI를 쓰면 된다.
2. 폼의 `csrf_token`이 `cord_csrf` 쿠키 값과 `hmac.compare_digest`로 일치해야 한다.

실패하면 403과 짧고 고정된 사유("origin rejected"/"csrf token missing or invalid")만 반환한다 —
제출된 바디를 절대 반영하지 않는다(`test_errors_are_legible_and_do_not_echo_payload`,
`tests/test_web.py:250-259`가 이미 세운 패턴을 뮤테이션 경로까지 확장).

이중 제출 쿠키+Origin 검사는 새 의존성이 없다(`secrets`/`hmac`/`http.cookies`는 stdlib) — ADR-0018
§1이 세운 "새 런타임 의존성은 Jinja2 하나뿐" 제약을 유지한다.

### 2. 중복 제출 방지 — execute는 1회용 nonce, respond는 기존 `response_dedupe` 재사용

**execute**(§3)는 매번 새 Thread/Run을 만든다(`aegra_client.execute`의 docstring,
`aegra_client.py:39-80`: "Each call creates a new Thread ... API retries are deliberately not
exposed") — 더블클릭이나 네트워크 재시도가 그대로 두 번째 실행을 만들 수 있다. execute 폼을 렌더할
때마다 `secrets.token_urlsafe(16)` nonce를 하나 찍어 폼에 숨겨 넣고, 서버 프로세스 메모리의
`threading.Lock`으로 보호된 `set()`에 등록한다. POST가 오면 lock 아래서 nonce를 **소비**(꺼내고
제거)한다 — 없거나 이미 소비됐으면 `execute()`를 아예 호출하지 않고 "already submitted; reload to
submit a new execution" 같은 고정 문구만 반환한다. 서버 재시작 시 이 집합은 사라지는데, 재시작 전에
렌더된 폼도 함께 무효가 되므로 안전한 방향의 실패다 — 이 저장소가 이미 지키는 "invocation 사이의
standing daemon 없음"(ADR-0018 §4, `docs/browser-viewer.md:24-25`) 원칙과 같은 결의 제약이다.

**respond**(§3)는 새 메커니즘이 필요 없다 — `response_dedupe.claim`(`response_dedupe.py:98-113`)이
이미 `(deployment, thread_id, interrupt_id)`로 디스크에 원자적으로 락을 걸고, `submit_response`가
authorize보다도 먼저 이를 호출한다(`approval_inbox.py:137-138`, docstring 순서 1). 웹 layer는 그저
`submit_response`를 그대로 호출하면 되고, 이는 재시작·여러 탭·여러 창을 가로질러도 durable하다 —
execute의 nonce보다 강한 보장이며, 새로 흉내 낼 이유가 없다.

### 3. POST 라우트 — 기존 도메인 함수를 그대로 호출, 새 검증 로직을 만들지 않는다

| 라우트 | 메서드 | 위임 대상 |
| --- | --- | --- |
| `GET /connections/{alias}/graphs/{graph_id}/execute` | GET | `backend.list_assistants()`로 채운 Assistant `<select>`, Subject/input JSON/context JSON 폼, nonce+csrf 발급 |
| 같은 경로 | POST | `cord_runtime.aegra_client.execute`(§2 nonce 소비 후) — `cmd_run`(`cli.py:343-378`)과 정확히 같은 함수, 성공 시 `record_submission`(`cli.py:369-375`와 동일)도 그대로 호출 |
| `GET /approvals` | GET | `discover_waiting`(등록된 모든 connection에 대해, `--reminder-after`/`--timeout-after`로 설정된 값) — 대기 중인 interrupt 목록, 항목마다 상세 링크 |
| `GET /approvals/{alias}/{thread_id}/{interrupt_id}` | GET | 그 시점에 새로 `discover_waiting`을 다시 호출해 해당 조합이 여전히 대기 중인지 확인 후, 있으면 bounded 렌더된 `value`와 응답 폼, 없으면("이미 처리됨/만료됨") 설명 문구만 — 절대 오래된 폼을 다시 보여주지 않는다(#49 AC3) |
| 같은 경로 | POST | `cord_runtime.approval_inbox.submit_response` — `approver`는 폼값(서명 안 된 텍스트 입력)을 **claim으로만** 넘기고, 그 claim의 accept/reject는 전적으로 §4가 넘기는 `auth_boundary`가 결정한다 |

Assistant는 자유 입력이 아니라 `list_assistants()`가 실제로 보고하는 목록에서 고르게 한다 —
"registered ... Assistant selection"(이슈 Scope)을 문자 그대로 만족시키고, 오타로 존재하지 않는
Assistant에 제출을 시도하는 경로를 애초에 없앤다(그래도 백엔드가 여전히 최종 검증자다 — 목록이
그 사이 바뀌었을 수 있으므로 `execute()` 자신의 실패는 그대로 표면화한다).

`execute`/`submit_response`가 이미 구분하는 상태(success/waiting/rejected/duplicate/unknown)를
그대로 HTTP 응답에 옮긴다 — 새 상태 분류를 만들지 않는다. `execute()`가 `RuntimeError`를 던지면
(전송이 끊겨 실제로 시작됐는지 알 수 없는 경우 포함, `aegra_client.py:39-80`이 이 구분을 만들지
않으므로) 웹 layer는 "unknown: submission outcome could not be confirmed" 고정 문구를 반환하고
**nonce를 되살리지 않는다** — 재시도는 사용자가 명시적으로 새로 제출하는 행동이어야지, 자동
재시도가 되어서는 안 된다(이슈 Verification "no guessed success after an ambiguous POST").

### 4. Entity 권한 — 새 connection 필드 `auth_endpoint`, HTTP 기반 `EntityAuthBoundary`, 미설정 시 항상 거부

`approver` 폼 필드를 그대로 권한으로 취급하는 것은 이슈가 명시적으로 금지한다. 동시에 authorization
정책을 Cordboard 프로세스 안에 Python으로 심는 것(예: connections.json에 dotted-path를 적어
`importlib.import_module`로 로드)은 ADR-0013·0014의 "모델/정책은 그래프가 소유"를 정면으로
어긴다 — 임의 Python을 Cordboard 프로세스에 로드하는 것은 그 자체로도 권한 상승 경로가 된다.

대신 `launch`가 이미 쓰는 것과 같은 모양 — **"connection이 가리키는, entity가 소유한 외부
프로세스/포트"** — 를 따른다. connections.json에 선택적 `auth_endpoint`(URL, `validate_endpoint`로
같은 검증을 통과해야 함, `connections.py:72-82`)를 추가한다. 이는 그래프를 묘사하지 않는다 —
"이 connection의 승인 결정을 어디에 물을지"라는 connection 자신의 속성이라서 ADR-0014의
"그 값이 그래프를 묘사하는가, 연결을 묘사하는가" 기준을 통과한다.

`HttpEntityAuthBoundary`(신규, `EntityAuthBoundary` Protocol 구현)는
`SyntheticEntityAuthBoundary`와 정확히 같은 순서로 검사한다(stale → revision → identity,
`entity_auth.py:71-80`이 이미 세운 순서) — 앞의 두 검사는 Cordboard가 이미 가진 정보(`still_pending`,
`current_revision`)로 로컬에서 하고, 마지막 identity/grant 판단만 `POST {auth_endpoint}/authorize`
로 위임한다(`AuthSubmission`의 필드를 그대로 JSON body로, `entity_auth.py:19-34`). 응답은
`{"accepted": bool, "reason": str}`. **포트에 연결할 수 없거나 응답이 계약과 다르면 항상 거부** —
"연결 안 됨"을 암묵적 허용으로 다루지 않는다. `auth_endpoint`가 connection에 아예 없으면 `/respond`
POST는 `submit_response`를 호출하지 않고 즉시 "no authority configured for this deployment; approval
is refused by default"를 반환한다 — 열려 있는 기본값을 두지 않는다.

이 설계는 새 도메인 로직이 아니다 — `AegraExecutionBackend`가 이미 실행을 위해 하는 것과 똑같은
일(loopback 밖 HTTP 포트에 그대로 위임)을 승인 결정에 대해서도 한다.

### 5. 액션 슬롯 해소 — 읽기 페이지는 계속 읽기 전용, 링크만 심는다

`page.html`의 `.cord-action-slot`(`page.html:89,93`)은 Run detail 렌더 시점에 그 Run의
`thread_id`로 `discover_waiting`을 다시 조회해 일치하는 interrupt가 있으면
`/approvals/{alias}/{thread_id}/{interrupt_id}` 링크로, 없으면("현재 대기 중인 interrupt 없음" 또는
"이 connection에 auth_endpoint 없음") 설명 문구로 채운다 — **인라인 폼이나 JS 뮤테이션 핸들러는
여전히 두지 않는다**. 이는 ADR-0018 §5·Consequences가 세운 "읽기 페이지는 읽기 전용 마크업"
경계를 유지하면서, 실제 컨트롤은 `/approvals/...`라는 분리된 표면에만 둔다 — 관측과 뮤테이션의
템플릿이 섞이지 않는다.

### 6. 임의 endpoint 전달 방지

`execute`/`respond` POST 바디는 **URL을 담지 않는다** — 대상은 언제나 경로의 `{alias}`로만
지정되고, 서버가 `load_connections(board_dir)`로 그 alias의 등록된 `endpoint`/`auth_endpoint`를
직접 조회한다(`connections.py:52-69`). 클라이언트가 임의 호스트로 전달을 유도할 방법이 구조적으로
없다 — CLI의 `cmd_run`이 이미 같은 방식으로 `connection[args.alias]["endpoint"]`만 쓰는 것과
동일하다(`cli.py:344-350`).

### 7. 민감 값 — 로그·아카이브에 절대 넣지 않는다

`Handler.log_message`는 이미 모든 요청에 대해 억제되어 있다(`server.py:41-42`, "Request URLs can
contain opaque Subject identifiers") — POST 바디에도 그대로 적용되고 새 로깅을 추가하지 않는다.
`WaitingInterrupt.value`(승인 대기 payload)와 `response_value`/execute의 `input`/`context`는
**요청/응답 렌더링 동안만 메모리에 존재**하고 board_dir에 쓰지 않는다 — 이는 이미 그렇다:
`response_dedupe.claim`/`record_outcome`(`response_dedupe.py:98-138`)과 `run_continuity.
record_submission`(`run_continuity.py:69-90`)은 식별자만 저장하고 값을 받지 않는다. 이 계층이
그 불변식을 어기지 않는다는 것이 새 책임이다 — Collector 게이트(ADR-0006)는 span에만 적용되고
이 값들은 span으로 나가지 않으므로, 여기서 새지 않게 하는 것은 이 ADR이 처음 지는 책임이다.
`/approvals` 상세 페이지의 `value` 렌더링은 Jinja2 autoescape(HTML 이스케이프, ADR-0018 §1이
이미 세운 것)로 충분히 bounded하다 — 별도 크기 제한을 두지 않되(임의로 자르면 승인자가 실제
내용을 못 보고 승인하게 될 위험이 더 크다), 렌더된 페이지 자체가 그 실행 하나 보는 동안만 존재하는
요청/응답이라는 점이 "no automatic archive/log persistence" 요구를 만족시킨다.

### 8. `cord serve` 신규 플래그

`--reminder-after`(기본 300초)·`--timeout-after`(기본 3600초) — `discover_waiting`이 요구하는
필수 키워드 인자(`approval_inbox.py:78-80`)를 위한 것으로, 지금까지 이 값을 고른 caller가 없었다.
합리적인 사람 단위 승인 대기 시간으로 골랐고, 필요하면 실행 시점에 오버라이드한다. `serve()`
시그니처(`server.py:123`)에 같은 이름의 키워드 인자로 그대로 전달한다 — 새 설정 저장소를 만들지
않는다.

### 9. 로컬 브라우저 테스트 범위 — 기존 fixture 재사용, Testbed 경계 유지

`tests/test_aegra_client.py`가 이미 쓰는 패턴(`monkeypatch.setattr(requests.Session, "request",
fake_request)`, `test_aegra_client.py:35-46`)을 그대로 확장해 Aegra HTTP 호출과 `auth_endpoint`
HTTP 호출을 모두 스크립트로 대체한다 — **브라우저와 `cord serve` 프로세스는 실제**이고, 그 둘이
실제 loopback HTTP로 통신하는 것도 실제다; 가짜인 것은 이 저장소 바깥의 Aegra/entity 프로세스뿐
이다. `examples/replay_counter_graph.build_safe_graph`(이미 존재, "approve" 노드가 `interrupt()`를
쓰고 `resumed_from` 키로 중복 적용을 막는 안전한 counter, `replay_counter_graph.py:61-97`)와
`SyntheticEntityAuthBoundary`가 이슈 Verification이 요구하는 "synthetic authorization boundary and
safe counter graph"다 — 새로 만들지 않는다.

`tests/test_web.py`(또는 신규 `tests/test_web_controls.py`)에 추가할 로컬 스모크 시나리오:

1. submit(execute 폼 제출) → pause(스크립트된 interrupted 응답) → 실제 Playwright로 `/approvals`
   진입 → 인가된 respond 제출 → resume → 기록된 완료(페이지가 `resumed`/성공 상태를 보여줌).
2. 거부 1건 — `auth_endpoint`가 `{"accepted": false, ...}`를 반환하는 스크립트, 페이지가 그
   reason을 그대로 보여주고 backend.resume은 호출되지 않았음을 확인(스크립트에 그 경로가 없으면
   호출 시 실패하도록 구성).
3. 알 수 없는 전송 결과 1건 — resume 스크립트가 예외를 던지게 해 "unknown" 문구가 뜨고
   `response_dedupe`가 `unknown`으로 기록됐는지 확인.
4. execute 더블클릭 — 같은 nonce로 두 번째 POST가 두 번째 Thread를 만들지 않는지 확인(스크립트에
   `/threads` 두 번째 호출이 없으면 실패하도록 구성).
5. CSRF/Origin 거부 — `Origin` 없이 또는 틀린 `csrf_token`으로 보낸 POST가 403이고 `execute`/
   `submit_response`가 전혀 호출되지 않는지(모킹된 함수의 호출 횟수로 확인).

이 로컬 스위트는 ADR-0018 §6/Consequences가 이미 세운 경계를 그대로 유지한다 — cordboard-testbed의
설치된 wheel 기준 실제 브라우저 acceptance를 대체한다고 주장하지 않는다. `docs/browser-viewer.md`
Verification 섹션의 실행 기록 갱신과 `scripts/verify-browser-artifact.py`로의 이 시나리오 편입은
구현 PR의 몫이며, 이 ADR은 그 도구(모킹 패턴, 재사용할 fixture, 5개 시나리오)만 정한다.

## Options Considered

1. **서버 세션 로그인 시스템(사용자명/비밀번호, 토큰 발급).** 기각 — loopback 전용 단일 운영자
   도구에 로그인 UI/자격증명 저장을 들이는 것은 이슈 Out of scope("Entity credential management")를
   침범하고, ADR-0018 §7이 이미 "인증은 이 슬라이스가 아니다"로 못 박았다. CSRF는 인증과 별개
   문제이므로 §1로 충분하다.
2. **`approver` 폼 값을 그대로 신뢰(감사 로그만 남김).** 기각 — 이슈 AC가 명시적으로 금지한다.
   로컬 loopback이라도 같은 머신의 다른 프로세스/브라우저 탭이 같은 서버에 도달할 수 있다.
3. **connections.json에 Python dotted-path로 `EntityAuthBoundary` 구현을 지정, `importlib`로
   로드.** 기각 — 임의 Python을 Cordboard 프로세스에 로드하는 것 자체가 권한 상승 경로이고,
   ADR-0013·0014가 세운 "정책은 그래프가 소유, Cordboard 프로세스 안에 두지 않는다"를 어긴다.
4. **`auth_endpoint` 미설정 시 기본 allow(모든 승인 자동 승인).** 기각 — 이슈가 요구하는 "never
   accept approver authority from unverified client fields"의 정반대다. 기본은 항상 거부.
5. **채택 — 더블서밋 쿠키+Origin 검사(§1), execute 전용 1회용 nonce + 기존 `response_dedupe`
   재사용(§2), 기존 도메인 함수를 그대로 호출하는 POST 라우트(§3), HTTP 기반
   `EntityAuthBoundary`+`auth_endpoint`+기본 거부(§4), 읽기 페이지는 링크만 심고 뮤테이션은
   `/approvals`로 분리(§5), alias 기반 대상 해석으로 임의 전달 차단(§6), 값은 렌더링 동안만
   메모리에(§7), 기존 스크립트 HTTP 모킹 패턴으로 로컬 브라우저 테스트(§9).** 새 도메인/정책
   로직을 하나도 만들지 않고, 기존에 이미 검증된 함수(`execute`/`submit_response`/
   `discover_waiting`/`response_dedupe`/`list_assistants`)의 첫 실제 caller가 되는 것으로
   이슈의 다섯 AC를 모두 만족시킨다.

## Trade-off Analysis

HTTP 기반 entity 권한 포트를 새로 정의하는 비용은, Cordboard 프로세스 안에 정책을 두지 않는다는
불변식을 지키기 위해 피할 수 없다 — `ExecutionBackend`가 이미 같은 모양(loopback 밖 HTTP로 실행
위임)을 실행에 대해 쓰고 있으므로 새 패턴이 아니라 기존 패턴의 대칭적 확장이다. `auth_endpoint`
미설정 시 항상 거부하는 기본값은 매력적이지 않아 보일 수 있지만(승인 기능이 "그냥 안 됨"으로
보임), 반대 기본값(무조건 허용)은 보안 사고이고 이슈 AC를 직접 위반한다 — 로컬 스모크 테스트가
`auth_endpoint`를 구성해 실제 경로를 검증하므로 기능 자체가 죽어 있는 것은 아니다. execute의
nonce가 서버 재시작에 살아남지 않는 대가는, 재시작 전에 열어 둔 탭의 실행 폼이 재시작 후
"already submitted"류 메시지 없이 새 CSRF 쿠키 불일치로 먼저 거부된다는 것인데(§1이 먼저 걸림),
사용자에게는 "페이지를 새로고침하라"는 동일한 결과로 나타나 혼란이 없다.

## Consequences

- 이 ADR이 **Accepted**로 확정되면, #49는 `planning:ready` 전환 조건을 만족한다. 라벨은 사용자
  승인과 함께 `planning:backlog`에서 `planning:ready`로 전환한다.
- 구현 PR은 §1–§9를 그대로 옮긴다: `connections.py`에 `auth_endpoint` 필드와 검증,
  `entity_auth.py`(또는 새 `web/auth.py`)에 `HttpEntityAuthBoundary`, `web/server.py`에 §1의
  CSRF/nonce 가드와 §3 라우트, `web/templates/`에 execute 폼·approvals 목록/상세 템플릿, 기존
  `page.html`의 action slot을 §5대로 채우는 변경, `cli.py`의 `cmd_serve`에 §8 플래그, 로컬 테스트는
  §9의 5개 시나리오.
- 실제 브라우저/설치 wheel 최종 acceptance는 여전히 cordboard-testbed의 몫으로 남는다 — 이
  ADR과 후속 구현 PR은 그것을 대체한다고 주장하지 않는다(ADR-0016·0018과 동일한 경계).
- 후속(이번 범위 아님): loopback 밖 원격 노출 시의 인증 경계(ADR-0018 §7이 이미 남긴 것과 동일한
  follow-up), Entity 쪽 `auth_endpoint` 서버 자체의 구현(그래프/entity 소유, Cordboard 범위 아님).

## Action Items

1. [x] 사용자 승인에 따라 Status를 Accepted로 변경하고 Deciders/날짜 기록 (2026-09-16)
2. [x] DECISIONS.md 색인에 0019 등록, 상태를 Accepted로 갱신
3. [x] #49 본문/코멘트에 이 ADR을 연결하고 `planning:backlog` → `planning:ready` 전환
4. [ ] 구현 PR: `connections.py`(`auth_endpoint`), `entity_auth.py`/`web/auth.py`
   (`HttpEntityAuthBoundary`), `web/server.py`(CSRF/nonce 가드 + §3 라우트), `web/templates/`
   (execute 폼, approvals 목록/상세, action slot 채움), `cli.py`(`cord serve` §8 플래그)
5. [ ] 구현 PR: §9의 로컬 스모크 테스트 5개 시나리오, 기존 `test_aegra_client.py` 모킹 패턴 재사용
6. [ ] 구현 PR: `docs/browser-viewer.md`/`ARCHITECTURE.md` 갱신 — #49 "remain unimplemented" 문구
   교체, 새 라우트/플래그 문서화
7. [ ] 후속 이슈(이번 범위 아님): 원격 노출 인증 경계
