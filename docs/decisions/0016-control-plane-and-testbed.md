# ADR-0016: Cordboard는 deterministic control plane이며 실프로세스 검증은 Testbed가 소유한다

**Status:** Accepted
**Date:** 2026-09-16
**Deciders:** 사용자 — control plane 경계 기록과 별도 Testbed scaffold 요청
**관련:** ADR-0003 (논리 Run), ADR-0006 (마스킹), ADR-0009 (발견), ADR-0013·0014·0015 (교환원 경계)

## Context

Cordboard는 여러 Agent Deployment를 등록하고 실행을 전달하며 실행 관계와 기록을
통합한다. 자체 LLM Agent나 production Aegra entity가 필요하지 않다. 현재 `aegra/`는
Aegra 공개 API, Postgres checkpoint와 관측 경계를 검증하는 integration fixture다.
`aegra/pyproject.toml`의 `cord-runtime = { path = ".." }`는 소스 결합 검증이며,
배포 wheel의 완전성이나 외부 소비자의 설치 가능성을 증명하지 않는다.

2026-09-16 현재 `cord add/list/run`, Rule/Signal routing, managed deployment의
지연 기동·idle sweep, live reconciliation은 구현되어 있다. 일반 `ExecutionBackend`,
새 상태 모델, identity-only result는 아직 없다. `aegra_client`의 `waiting`은
interrupt와 polling 기한 만료를 함께 나타내며 성공 결과는 `/state`의 `values`를 포함한다.
이 ADR은 이 사실을 새 설계가 이미 구현된 것으로 바꾸지 않는다.

## Decision

### 1. Deterministic control plane

Cordboard는 Agent execution switchboard다. connection catalog, deployment identity,
Graph/Assistant discovery, Signal/Rule routing, concurrency/deduplication, execution
submission, interrupt/resume/cancel 전달, live reconciliation, topology/telemetry 상관,
archive/query/viewer와 플랫폼 승인 만료 정책을 소유한다. LLM은 필수가 아니다.

Deployment lifecycle은 운영자가 연결에 지정한 기존 launch/health/stop 계약을
사용하는 범위다. Entity의 runtime, dependency provisioning, checkpoint 운영,
credentials, authorization, workspace와 business data를 플랫폼이 흡수하지 않는다.
Graph 내부 delegation은 Graph/Entity가 소유하고 Graph 간 연결은 새 Run을 만드는
deterministic routing으로 처리한다.

### 2. Execution backend 경계 (구현 예정)

첫 backend는 Aegra이며 현재 공개 HTTP/SSE 계약을 보존하면서
`AegraExecutionBackend` 뒤로 옮긴다. router/approval/viewer는 transport 고유 동작에
직접 의존하지 않도록 단계적으로 전환한다. 목표 위치는
`cord_runtime/backends/base.py`와 `aegra.py`이며 아직 존재하는 API가 아니다.

목표 계약은 `list_assistants`, `execute`, `status`, `resume`, `cancel`, `watch`다.
Aegra adapter에서는 `create_thread`, `submit_run`, `get_run`, `get_state`와
`wait_for_run`을 분리한다. `execute`는 조합용 convenience로 남길 수 있다.
다른 backend에 없는 기능은 명시적으로 지원하지 않음을 표현하며, 가짜 동등성을 만들지 않는다.
지금 범용 plugin framework나 미사용 adapter를 구현하지 않는다.

- runtime status는 `QUEUED/RUNNING/INTERRUPTED/SUCCEEDED/FAILED/CANCELLED/UNKNOWN`으로
  구분하고 client wait outcome(`deadline_reached` 등)과 분리한다. 대기 만료는 취소가 아니다.
- 기본 result는 Deployment, Thread, invocation, logical Run의 identity/status다.
  전체 checkpoint state 조회는 명시적인 별도 동작으로 만든다. Entity의 공개 output을
  운반할 때도 opaque payload의 표시·저장 정책을 identity/telemetry와 분리한다.
- `LogicalRunId`, `InvocationId`, `ThreadId`, `TraceId`를 domain type으로 구분한다.
  Aegra resume은 같은 Thread에 새 invocation을 만든다. 논리 Run/trace 연속성은 별도
  매핑이며 API ID를 이름만 바꿔 해결하지 않는다. 기존 CLI/보관 기록의 호환 전환을 검증한다.

### 3. Aegra fixture는 Testbed로 분리

Cordboard does not operate its own production Aegra entity. Aegra compatibility
fixtures belong in [cordboard-testbed](https://github.com/agent-topology/cordboard-testbed).
이 요청의 최종 위치는 별도 저장소다. 먼저 제안된 본체의
`tests/integration/fixtures/aegra-deployment/` 이동은 중간 단계로 강제하지 않는다.

| 본체에 유지 | Testbed 이전 후보 |
| --- | --- |
| `tests/test_aegra_client.py`, mocked HTTP/SSE, backend interface contract | `aegra/`, `tests/test_aegra.py` |
| router/rule/dedupe/concurrency/lifecycle 단위 검증 | 실제 서버·Postgres·HTTP/SSE·interrupt/resume/cancel |
| topology parsing, span 생성, archive query, CLI 단위 검증 | `tests/test_collector.py`, `tests/test_langfuse_integration.py` |
| 공개 계약과 제품 코드 | `examples/aegra_graph.py`, `aegra_run.py`, `aegra_server.py` |
| 모델 없는 제품 acceptance | `examples/provider_smoke.py`, `examples/model_proxy/`의 선택적 통합 |

이전은 파일 복사만으로 완료되지 않는다. fixture가 공유하는 helper와 문서 참조를 확인하고,
독립 설치·동등한 positive/negative 관문을 검증한 뒤 기존 자산을 제거한다. 기존 본체 tests를
Testbed에서 import하거나 `PYTHONPATH`로 소스 checkout을 주입하지 않는다.

Testbed의 허용 경계는 실제 `cord` CLI, 문서화된 공개 Python API, Aegra HTTP API,
Entity의 topology HTTP endpoint, OTLP와 공개 archive/filesystem artifact다.
`cord_runtime.router` 내부 함수를 호출해 상태를 조작하는 테스트는 본체에 남긴다.
synthetic Entity는 자기 Graph와 instrumentation을 소유하므로 공개 producer API를
wheel에서 사용할 수 있다. assertion 측의 내부 구현 접근과 구분한다.

Cordboard 설치 입력은 릴리스 package, 명시적인 Git commit SHA 또는 CI/build wheel이다.
`path = ".."`와 editable 설치를 금지하고 artifact hash와 실제 설치 버전을 증거로 남긴다.
예시 버전 `0.1.0b1`을 실제 Cordboard 릴리스라고 가정하지 않는다(현재 소스는 `0.0.1`).

### 4. Synthetic 우선, internal은 선택

첫 환경은 모델 없는 deterministic/interrupt Graph, 실제 Aegra와 Postgres이며
관측 경로는 producer redaction → Collector gate → archive다. 최소 입력으로 fresh
Thread/Run, state 보존, 실제 HTTP/SSE, parentage와 redaction을 구분해 측정한다.

두 번째 `multi-entity` 환경은 personal/employer의 별도 프로세스·DB·합성 credential로
같은 graph ID를 실행한다. Deployment별 identity, credential/state 격리, 올바른
Rule 대상 선택, topology 결합, 한 Entity 실패의 격리, 통합 archive 조회를 검증한다.
`failure-lab`은 timeout, 끊긴 SSE, 중복 delivery, ingestion 실패와 관문 음성 시험을 맡는다.

기본 CI는 synthetic만 사용한다. Omiologic·git-agent·campaign-agent를 사용하는
`internal` 검증은 별도 opt-in이며 private package나 provider credential을 기본 의존성으로
넣지 않는다. Testbed scaffold 생성은 전체 migration 또는 위 시나리오 통과의 증거가 아니다.

### 5. Entity, discovery와 선택적 Agent

Omiologic/employer Entity는 Aegra, Graph 등록, credentials/model gateway, authorization,
workspace, Git/GitHub adapter, checkpoint, 승인 callback과 request context를 소유한다.
Cordboard는 `git-agent`/`campaign-agent`를 직접 설치하거나 import하지 않고 이들을
등록한 Entity Deployment에 연결한다. 기존 내부 구현 근거는
[2026-09-15 점검](../internal-dependencies.md)을 따른다.

topology publication과 execution instrumentation도 Entity 책임이다. Cordboard는
발행된 문서를 읽고 cache/snapshot/drift detection만 한다. 기존 discovery 경로
`/.well-known/agent-topology.manifest.json`을 유지한다. 제안의
`/.well-known/agent-topology.json` 또는 `/graphs/{graph_id}/topology`는 이번에 새 표준으로
채택하지 않는다. topology 부재는 연결을 막지 않는다(ADR-0015).

A2A는 미래의 optional execution backend다. Agent Card, 외부 delegation/artifact 교환이
필요할 때 검토하며 core domain model이나 현재 Aegra 연결의 전제조건으로 삼지 않는다.
단순 webhook은 Signal adapter로 처리한다.

자연어 계획이 필요하면 별도 `cordboard-operator` Deployment를 만든다.
`LLM proposes → User approves → Cordboard validates and executes deterministically`를
따르며 일반 client와 동일한 명령/API만 쓴다. 특별 권한이나 기본 설치 의존성을 주지 않는다.

| 저장소 | 역할 |
| --- | --- |
| agent-topology / agent-topology-testbed | topology spec·adapter / conformance·framework 검증 |
| redact-secret / redact-secret-benchmarks | 탐지·redaction engine / corpus·성능 검증 |
| git-agent / campaign-agent | Entity가 설치하는 domain graph library |
| omiologic-aegra / employer별 deployment | 각 Entity의 isolated runtime와 business resources |
| cordboard / cordboard-testbed | 연결·routing·lifecycle·기록·viewer / 외부 프로세스 black-box 검증 |
| optional cordboard-operator | 자연어 계획과 승인된 Cordboard 명령 호출 |

## Options Considered

1. 본체를 Aegra entity/Agent로 운영: 제품과 Entity의 책임이 섞여 기각.
2. 본체 안 fixture만 재배치: 이름은 명확해져도 소스 설치 결합이 남아 최종 구조로 기각.
3. 외부 Testbed + 얇은 backend 경계: 채택. 설치 artifact와 실행 계약을 함께 검증한다.

## Trade-off Analysis

저장소 간 artifact 전달과 버전 조합 관리 비용이 늘어난다. 반면 source checkout에서만
통과하는 오류를 잡고 Entity 책임을 본체에 넣을 이유를 줄인다. migration 중에는 기존
fixture가 남아 있으므로 README의 지위 설명과 실제 완료 여부를 함께 유지한다.

## Consequences

ADR-0013·0014·0015를 확장한다. ADR-0009의 Cordboard가 Entity HTTP publication을
자동 생성한다는 남은 계획은 Entity 소유로 정정한다. 운영자가 명시한 연결 lifecycle은
유지하며 Entity provisioning으로 확장하지 않는다. 과거 통합 결과는 당시 fixture의
증거이며 새 Testbed의 실행 결과로 옮겨 적지 않는다.

## Action Items

1. [x] ADR, README, Architecture에 제품·Entity·Testbed 경계 기록
2. [x] Testbed scaffold에서 wheel 설치와 최소 synthetic 실행 검증 (3개 real-process check)
3. [ ] 실프로세스 통합 후보의 의존 관계·동등한 관문을 확인한 뒤 물리적 이전
4. [ ] Aegra backend 경계와 transport/wait 분리, caller 전환
5. [ ] 상태·wait outcome, identity type, 명시적 output 조회의 호환 계약 구현
6. [ ] multi-entity·failure-lab·telemetry·SSE/cancel 관문 확장
7. [ ] Entity topology publication/observer 연동 및 opt-in internal 검증

### 최초 scaffold 검증 (2026-09-16)

별도 Testbed에 실제 wheel을 설치하고 CLI discovery, 같은 Subject의 독립 실행,
HTTP interrupt/resume 3개 검증이 통과했다. 상세 artifact hash와 미실행 범위는
[Testbed 검증 기록](https://github.com/agent-topology/cordboard-testbed/blob/main/VERIFICATION.md)에 둔다.
resume 요청에서 `assistant_id`를 빼면 Aegra 0.10.4가 422를 반환했고 명시하면 통과했다.
현재 본체 `aegra_client.resume`은 이 필드를 보내지 않으므로 별도 호환성 확인·수정 대상이다.
Testbed는 HTTP를 직접 호출했으며 본체 helper가 검증되었다고 주장하지 않는다.
