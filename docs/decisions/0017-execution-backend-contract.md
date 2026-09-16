# ADR-0017: ExecutionBackend 식별자·상태·결과 계약 확정

**Status:** Accepted
**Date:** 2026-09-16
**Deciders:** 사용자 — #42 readiness gate 검토 후 승인
**관련:** ADR-0016 §2 (경계 스케치, 이 ADR이 상세화), ADR-0003 (논리 Run 정정),
ADR-0002 (어휘 매핑), ADR-0013·0014·0015 (교환원 경계), ADR-0006 (마스킹)

## Context

[ADR-0016 §2](0016-control-plane-and-testbed.md#2-execution-backend-경계-구현-예정)는
`ExecutionBackend` 경계와 목표 계약(`list_assistants`/`execute`/`status`/`resume`/`cancel`/`watch`,
Aegra adapter의 `create_thread`/`submit_run`/`get_run`/`get_state`/`wait_for_run` 분리,
`QUEUED/RUNNING/INTERRUPTED/SUCCEEDED/FAILED/CANCELLED/UNKNOWN` 상태, `LogicalRunId`/
`InvocationId`/`ThreadId`/`TraceId` 구분)를 이미 승인했지만 "구현 예정"으로 남겨 두었다.
[#42](https://github.com/agent-topology/cordboard/issues/42)는 이 스케치를 "구현자가 범위를
다시 결정하지 않아도 되는" 수준까지 못 박기 전에는 `planning:ready`로 바꾸지 말라고 명시한다
(이슈 본문 Blockers and handoff: "Publish the interface/result/status contract before marking
ready"). [#40](https://github.com/agent-topology/cordboard/issues/40)이 머지되어 resume의
`assistant_id` 누락 문제는 해결됐으므로, 남은 선행 조건은 이 계약 자체뿐이다.

현재 `src/cord_runtime/aegra_client.py`의 실제 동작:

- `execute`(39–81행)와 `resume`(83–116행)은 각각 새 Thread를 만들거나 기존 Thread에
  제출한 뒤 `_poll_until_settled`(22–37행)로 대기한다. 이 함수는 Aegra raw status
  `"success"`→`"success"`, `"interrupted"`→`"waiting"`, 대기 시한 만료(여전히
  `"pending"`/`"running"`)→`"waiting"`으로 매핑한다. **인터럽트와 시한 만료가 같은
  문자열로 뭉개진다** — #42 AC1이 지적하는 결함이 정확히 여기 있다.
- `"success"` 이외의 raw status(예: Aegra의 실패 상태)는 즉시
  `RuntimeError("Aegra execution did not succeed; ...")`로 던져지고, 호출자는 Thread/Run
  identity를 돌려받지 못한다. `status`를 별도 조회 동작으로 분리하면 실패도 identity를
  보존한 채 값으로 돌아와야 한다.
- 성공 결과는 `{"run_id", "thread_id", "status": "success", "values": <전체 checkpoint state>}`
  를 반환한다(35행) — 기본 결과에 output이 항상 포함된다.
- `run_id`라는 이름은 실제로는 Aegra API의 1회 제출(HTTP `POST .../runs`)에 대한 응답이며,
  `resume()`은 같은 Thread에서도 매번 새 `run_id`를 받는다(ADR-0003 정정). 이것은 이 ADR이
  말하는 **InvocationId**다.
- `src/cord_runtime/run_continuity.py`의 `record_submission`은 한 (Deployment, Thread)
  쌍의 **첫 번째** API `run_id`를 그 쌍의 논리 Run ID로 채택하고, 이후 제출을 그 아래
  묶는다(69–90행). 이것이 이 ADR이 말하는 **LogicalRunId**이며, 오늘의 코드에서 이미 이
  값으로 정의돼 있다 — 이 ADR은 이름만 붙이고 값의 정의는 바꾸지 않는다.
- `src/cord_runtime/execution.py`의 `run()`(139–162행)은 그래프 프로세스가 직접 여는
  OTel root span이며 `cord.run.id`(기본 `uuid4()`)와 trace id를 스스로 갖는다. 이는
  **control plane이 모르는, 그래프가 스스로 붙이는 identity**다. 오늘 코드에는 이 값과
  Aegra의 LogicalRunId/InvocationId를 연결하는 어떤 매핑도 없다 — 두 그래프 프로세스가
  같은 Aegra Thread를 공유하지 않는 한 애초에 존재할 수 없는 매핑이다.
- `src/cord_runtime/cli.py:224–232`의 `cmd_run`은 `execute()`의 반환값을 그대로
  `json.dumps`하고, `result["status"] == "success"`로 종료 코드를 정한다. 이것이 #42 AC5가
  말하는 "existing CLI JSON"이다.
- 오늘 시점에 `resume`/`cancel`은 `cli.py`/`router.py` 어디서도 호출되지 않는다(직접
  스크립트/Testbed 전용). 따라서 이 계약이 바꿔야 할 본체 내부 caller는 `execute`,
  `describe_run`, `describe_assistant`, `watch_lifecycle` 넷뿐이다(`cli.py:18`,
  `router.py:43`).

## Decision

### 1. Identity 타입 — 넷은 서로 다른 값이고, 셋만 실제로 연결된다

새 모듈 `cord_runtime/backends/base.py`(ADR-0016 §2가 지정한 목표 경로)에 값 기반
`NewType`(또는 `frozen=True` 1필드 dataclass; 구현 시 선택) 넷을 둔다.

| 타입 | 의미 | 오늘의 대응값 | 아는 시점 |
|---|---|---|---|
| `ThreadId` | Aegra Thread | 오늘의 `thread_id` 그대로 | 최초 제출 전 |
| `InvocationId` | Aegra API `run_id` 1회분 — 제출마다 새로 생긴다 | 오늘의 `run_id`(execute/resume 반환값) | 각 제출 응답 |
| `LogicalRunId` | 한 Thread 위 모든 Invocation을 묶는 control-plane identity | `run_continuity.record_submission`이 이미 정의한 "첫 Invocation" 값 | 첫 제출 응답 |
| `TraceId` | 그래프가 스스로 여는 OTel trace — `execution.run()`/`RunContinuation.trace_id` | 오늘의 `cord.run.id`/trace id | 그래프가 계측된 코드에 도달했을 때, 없을 수도 있음 |

`LogicalRunId`와 `TraceId`는 **의도적으로 다른 identity 공간**이다. 전자는 control plane이
Thread/Invocation 장부만 보고 아는 값이고 span 없이도 존재한다. 후자는 그래프 프로세스
자신의 계측이 만드는 값이고 control plane은 이를 만들지도, 강제하지도 않는다
(ADR-0013·0014: 그래프 내부는 그래프 소유). 둘은 오직 caller가 넘긴 Subject/Assistant/
Thread를 통해서만 상관되며, 값이 같아야 한다는 요구는 두지 않는다. `describe_run`가 이미
이 상관을 구현하고 있다(`config.configurable.cord_subject` 읽기, 168–177행) — 이 ADR은
그 사실을 타입으로 명시할 뿐 새 매핑을 만들지 않는다.

### 2. `RuntimeStatus`와 `ClientWaitOutcome`은 서로 다른 닫힌 목록이다

```python
class RuntimeStatus(Enum):
    QUEUED = "queued"        # Aegra "pending"
    RUNNING = "running"      # Aegra "running"
    INTERRUPTED = "interrupted"
    SUCCEEDED = "succeeded"  # Aegra "success"
    FAILED = "failed"        # Aegra의 그 외 종료 상태
    CANCELLED = "cancelled"  # cancel() 이후 확인된 상태
    UNKNOWN = "unknown"      # 응답을 해석할 수 없음 — transport 실패와 다름

class ClientWaitOutcome(Enum):
    COMPLETED = "completed"          # 대기 중 종결 상태(SUCCEEDED/FAILED/CANCELLED/INTERRUPTED) 도달
    DEADLINE_REACHED = "deadline_reached"  # 여전히 QUEUED/RUNNING인 채로 wait budget 소진
    TRANSPORT_ERROR = "transport_error"    # 응답 자체가 모호함(연결 실패, 비2xx 등)
```

`status()`(Aegra의 `get_run` 1회 조회)는 `RuntimeStatus`만 반환한다. `wait_for_run()`은
`(RuntimeStatus, ClientWaitOutcome)` 쌍을 반환한다. **대기 시한 만료는 그 자체로
`RuntimeStatus`가 아니다** — 만료 시점의 실제 `RuntimeStatus`(QUEUED 또는 RUNNING)를
그대로 보존하고 `wait_outcome=DEADLINE_REACHED`만 덧붙인다. 이것이 AC1 "Polling deadline
never reports interruption or cancellation and preserves the execution handle"의 정확한
구현이다 — 오늘 코드처럼 INTERRUPTED와 시한 만료를 같은 `"waiting"`으로 합치지 않는다.
`FAILED`는 예외로 던지지 않고 값으로 반환해 Thread/Invocation identity를 보존한다(오늘
`_poll_until_settled`의 즉시 `RuntimeError`를 대체).

### 3. 기본 결과는 identity/status만, output은 명시적 별도 호출

`execute`/`resume`/`status`의 기본 반환:

```python
{
    "logical_run_id": LogicalRunId, "invocation_id": InvocationId, "thread_id": ThreadId,
    "assistant_id": str, "status": RuntimeStatus, "wait_outcome": ClientWaitOutcome | None,
}
```

`values`(전체 checkpoint state)는 여기 없다. Entity의 공개 output이 필요하면 별도
`get_state(endpoint, thread_id) -> dict`를 호출한다(ADR-0016 §2가 이미 지정한 이름).
opaque payload를 어떻게 보여주고 저장할지는 이 ADR의 범위 밖이며(#42 Out of scope: graph
업격 result schema), 여기서 정하는 것은 "기본 경로에 없다"는 경계뿐이다.

### 4. `ExecutionBackend` / `AegraExecutionBackend` 메서드

목표 파일 `cord_runtime/backends/base.py`(Protocol)와 `aegra.py`(구현), 아직 없음.

```python
class ExecutionBackend(Protocol):
    def list_assistants(self) -> list[dict]: ...
    def execute(self, assistant: str, subject: str, graph_input: dict, *,
                request_context: dict | None = None, timeout: float = 120,
                caused_by_run_id: str | None = None, cascade_depth: int = 0) -> ExecutionResult: ...
    def status(self, thread_id: ThreadId, invocation_id: InvocationId) -> RuntimeStatus: ...
    def resume(self, thread_id: ThreadId, assistant: str, resume_value, *,
               timeout: float = 120) -> ExecutionResult: ...
    def cancel(self, thread_id: ThreadId, invocation_id: InvocationId) -> RuntimeStatus: ...
    def watch(self, thread_id: ThreadId, invocation_id: InvocationId, *,
              timeout: float = 120) -> Iterator[tuple[str, dict, str | None]]: ...
    @property
    def capabilities(self) -> frozenset[str]: ...
```

`AegraExecutionBackend`는 위를 내부적으로 `create_thread`(오늘의 `POST /threads`),
`submit_run`(오늘의 `POST .../runs`, execute/resume 공용), `get_run`(오늘의
`describe_run`/`_poll_until_settled`의 단일 조회 절반), `get_state`(오늘의
`GET .../state`), `wait_for_run`(오늘의 `_poll_until_settled` 나머지 절반)으로 분해해
구현한다. `execute`는 `create_thread`+`submit_run`+`wait_for_run`의 convenience로 남긴다
(ADR-0016 §2가 이미 허용). 다른 backend가 지원하지 않는 동작은 `capabilities`에서
빠지고 호출 시 `NotImplementedError`를 던진다 — 가짜 동등성을 만들지 않는다.

### 5. 버전 호환 경로

- **본체 caller 넷**(`cli.py`의 `execute`/`describe_run`/`describe_assistant`/
  `watch_lifecycle` 호출, `router.py`의 `execute` 호출)은 이 이슈에서 새 계약으로
  전환하되, `aegra_client.execute`/`resume`/`cancel`/`describe_run`/`describe_assistant`/
  `stream_lifecycle`/`watch_lifecycle` 자유 함수 자체는 그대로 남긴다 — 새
  `AegraExecutionBackend`의 얇은 wrapper가 되어 오늘과 같은 반환 모양(`"waiting"` 포함)을
  유지한다. `resume`/`cancel`은 본체 내부 caller가 없으므로 이 함수들의 시그니처를 이번에
  바꾸지 않아도 AC1–AC4를 만족할 수 있다.
- **`cord run`의 JSON 출력과 종료 코드**(`cli.py:224–232`)는 기본값을 바꾸지 않는다.
  새 identity/status 전용 결과는 명시적 opt-in(예: 새 플래그 또는 새 서브커맨드)으로만
  나온다. 기본 출력을 새 모양으로 바꾸는 것은 별도 후속 이슈이며, 이 ADR은 그 전환이
  필요하다는 사실과 두 모양이 공존해야 한다는 제약만 못박는다.
- **`run_continuity.json`의 파일 포맷은 바뀌지 않는다.** 키 `run_id`/`api_run_ids`는
  그대로 두고, 코드에서 다루는 타입만 `LogicalRunId`/`InvocationId`로 명명한다. 기존
  파일에 대한 마이그레이션은 필요 없다.
- **아카이브/span 계약은 이 ADR의 범위 밖이다.** `cord.run.id`/Run semconv는 바뀌지 않는다.

## Options Considered

1. **문서만 정리하고 코드에 새 타입을 두지 않는다.** 기각 — 구현자가 여전히
   InvocationId/LogicalRunId를 같은 변수명(`run_id`)으로 섞어 쓸 여지가 남고, #42 AC3
   ("without importing entity graph code or adding provider configuration")을 검증할
   방법이 없다.
2. **지금 범용 plugin/adapter framework를 만든다.** 기각 — ADR-0016 §2가 이미 명시적으로
   배제("지금 범용 plugin framework나 미사용 adapter를 구현하지 않는다").
3. **`cord run`의 기본 JSON을 이번에 새 모양으로 바꾼다.** 기각 — #42 AC5("Existing
   archive versions stay readable, and CLI/API compatibility tests cover both current and
   intentionally changed behavior")가 요구하는 버전 있는 전환 없이 기존 스크립트를 깬다.
4. **채택 — 새 타입/enum/backend 분해를 정의하되, 자유 함수 호환 shim과 CLI 기본값은
   보존하고 opt-in으로만 새 모양을 노출한다.** 구현 PR이 즉시 시작할 수 있는 만큼
   구체적이면서도, 기존 caller와 저장 포맷을 깨지 않는다.

## Trade-off Analysis

두 개의 결과 모양(legacy `"waiting"` 합성 vs. 새 `RuntimeStatus`/`ClientWaitOutcome` 분리)을
CLI가 전환할 때까지 병행 유지하는 비용이 든다. 대신 #42의 구현자가 이름/상태값/버전 전략을
다시 결정하지 않고 이 표와 시그니처를 그대로 구현할 수 있고, 리뷰 시점에 "왜 이 이름인가"를
다시 논쟁하지 않는다.

## Consequences

- 이 ADR이 **Accepted**로 확정되어, #42는 `planning:ready` 전환 조건 중 "필요한 선행 작업과
  ADR 결정이 확정" 및 "구현자가 범위나 제품 정책을 다시 결정하지 않아도 된다"를 만족한다.
  라벨은 이 승인과 함께 `planning:backlog`에서 `planning:ready`로 전환한다.
- 후속 구현 PR은 이 문서의 §1–§4 시그니처를 그대로 `cord_runtime/backends/base.py`,
  `aegra.py`에 옮기고, §5의 caller 전환·CLI opt-in을 수락 기준으로 삼는다.
- `cord run`의 기본 출력/종료 코드 전환은 이 ADR이 만들지 않는 후속 이슈로 명시적으로
  남는다 — 지금 구현 이슈의 범위에 슬쩍 포함시키지 않는다.

## Action Items

1. [x] 사용자 검토 후 Status를 Accepted로 변경하고 Deciders를 채운다
2. [x] DECISIONS.md 색인에 0017 등록 (본 변경에 포함)
3. [x] #42 본문/코멘트에 이 ADR을 연결하고 `planning:backlog` → `planning:ready` 전환
4. [x] 구현 PR: `cord_runtime/backends/base.py`(Protocol, 타입, enum), `aegra.py`(분해된 Aegra adapter)
5. [x] 구현 PR: 기존 `aegra_client` 자유 함수는 그대로 두고 검증 (아래 2026-09-16 구현 정정 참조)
6. [x] 구현 PR: `cli.py`의 `_watch_live_run`(viewer 경로)을 새 backend로 전환 (아래 정정 참조)
7. [ ] 후속 이슈(이번 범위 아님): `cord run` 기본 JSON/종료 코드를 새 identity/status 모양으로 옮기는 버전 있는 전환
8. [ ] 후속 이슈(이번 범위 아님): `router.py`의 `execute()` 호출을 새 backend로 전환

### 2026-09-16 구현 정정

구현 중 §5·Action 5·6의 방향을 뒤집었다. 원안은 "`aegra_client` 자유 함수가 새
`AegraExecutionBackend`의 얇은 wrapper가 된다"였다. 실제로 이렇게 하려면
`cli.py`의 `execute()` 호출과 `router.py`의 `execute()` 호출도 새 결과 모양을
받도록 함께 바꿔야 하는데, `tests/test_router.py`가 `monkeypatch.setattr(router,
"execute", ...)`로 15개 이상의 이미 검증된 #16·#17·#18 cascade/dedupe/concurrency
테스트를 이 정확한 자유 함수 시그니처에 걸어 두고 있었다. 그 테스트들을 새 결과
모양에 맞춰 다시 쓰는 것은 이 이슈의 범위(`aegra_client.execute` 자체의 대기
동작을 어떻게 노출하느냐)를 넘어 이미 검증된 라우팅 동작을 다시 검증하는 일이 된다
— AGENTS.md의 "기존 사용자 작업 보존, 현재 슬라이스를 넘어서 확장하지 않는다"
원칙과 충돌한다.

대신 방향을 뒤집었다: `cord_runtime/backends/aegra.py`가 새 계약의 실제 구현이고,
`aegra_client.py`는 **손대지 않았다** — 기존 40여 개 테스트가 그대로 통과한다.
`AegraExecutionBackend`는 `aegra_client.describe_run`/`describe_assistant`/
`cancel`/`watch_lifecycle`을 이미 검증된 원시 동작으로 재사용하고, 그 위에
`create_thread`/`submit_run`/`wait_for_run`/`get_state`와 새 결과 타입만 추가한다.

`cli.py`/`router.py`의 "본체 caller 넷" 중 실제로 새 backend로 전환한 것은
**`_watch_live_run`(viewer 경로) 하나뿐이다** — 이슈 본문의 Outcome이 명시한
"routing, approvals **and viewer** code" 중 viewer 부분과 정확히 일치하고,
관련 테스트(`tests/test_cli.py`의 `--watch` 테스트 5개)만 새 호출 표면
(`AegraExecutionBackend`의 메서드)을 patch하도록 갱신하면 됐다. `cli.py`의
`cmd_run`과 `router.py`의 `execute()` 호출은 의도적으로 그대로 두었다 —
`cord run`의 기본 JSON/종료 코드 보존은 이미 §5가 별도 후속 이슈로 미뤄 뒀고,
`router.py`는 위에서 설명한 이유로 이번 슬라이스에 포함하지 않는다. AC3
("Generic callers depend on the backend boundary")는 이 범위로 충족한다고
판단한다: 새 backend가 실재하고, 실제 production 코드(viewer 경로)가 그것에
의존하며, 어떤 caller도 entity graph 코드를 import하거나 provider 설정을
추가하지 않는다.
