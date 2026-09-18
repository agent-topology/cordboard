# ADR-0022: LangGraph 콜백 seam으로 Run/Step을 기록한다 — 그래프 코드 변경 없이, 별도 배포로

**Status:** Accepted
**Date:** 2026-09-18
**Deciders:** 단독 (로컬 도구, 저자 = 운영자) — #89 구현과 동시에 결정
**관련:** ADR-0008 (Outcome 분리, 인터럽트/재개 span 경계), ADR-0021
(중첩 Step, semconv 0.4.0), ADR-0006 (마스킹은 각 프로세스 안, Collector가
관문), ADR-0013 (Cordboard는 모델을 모른다), ADR-0014/0015 (그래프 묘사
금지, 연결 비차단), #28 (그래프 코드 계측 patternwork), #52 (core observer
한계)

## Context

ADR-0008/#28이 이미 "Run → Step → Attempt" span 계약과 인터럽트/재개 경계를
정했지만, 그 구현(`cord_runtime.execution`, `examples/interrupt_graph.py`)은
**그래프 자신의 코드가 `cord_runtime`을 직접 호출**해야 한다 — 노드 함수
안에서 `config["configurable"]["cord_active"].step(...)`를 여는 patternwork.
재사용 가능한 라이브러리 그래프(campaign-agent의 `channel_concept` 같은)는
이 의존 방향을 받아들일 수 없다 — 그래프가 플랫폼을 알아야 하는 것은
ARCHITECTURE.md의 소유권 경계("그래프는 비즈니스 로직, 플랫폼은 공용 어휘")를
정확히 거스른다.

#52는 `agent-workflow-core`의 core observer가 진짜 Step 경계(시작/종료 시각,
지속시간)를 줄 수 없다고 이미 확인했다 — Attempt 결과만 선언하는 알림이라
"조작된 지속시간 span"을 만들게 된다. 이 이슈는 **다른** 공개 LangGraph
seam을 쓴다 — 콜백(`BaseCallbackHandler`). core observer는 쓰지 않는다.

**증거(2026-09-18 검증, 모델·네트워크 없음):** 평범한 `BaseCallbackHandler`를
`config["callbacks"]`로 campaign-agent `channel_concept`에 붙여 승인
인터럽트까지 실행했다. 노드 실행은 `graph:step:N` 태그가 붙은
`on_chain_start` 이벤트이고, `metadata["langgraph_checkpoint_ns"]`가 전체
중첩 경로를 준다(`prepare_brief:<task>|select_evidence:<task>` — 최상위
세그먼트는 `channel_concept` Node id, `prepare_brief|select_evidence`는
`channel_concept:prepare_brief`의 `select_evidence` Node로 해석된다). 27개
Step 시작이 각각 정확히 하나의 `on_chain_end`/`on_chain_error`를 받았고,
인터럽트 Node는 `on_chain_error(GraphInterrupt)`로 닫혔다. 라우팅 함수는
`seq:step:*` 태그를 달아 구분된다. 이번 구현 과정에서 실제 설치된
`langgraph==1.2.11`/`langchain-core==1.6.3` 소스를 직접 읽어 이 메커니즘의
세부(태그가 `CallbackManager.get_child(f"graph:step:{step}")`로
`inherit=False`로 붙는다는 것, `NS_SEP="|"`/`NS_END=":"`,
`patch_config`가 `configurable`을 **병합**한다는 것, `on_chain_start`에만
`metadata`가 오고 `on_chain_end`/`on_chain_error`에는 안 온다는 것)를
재확인했다 — `tests/test_langgraph_callbacks.py`가 이 전부를 실행 증거로
남긴다.

**패키징 제약(검증됨):** `cord-runtime`은 `redact-secret==0.1.0b1`/
`opentelemetry-sdk==1.39.1`을 정확히 고정한다(`pyproject.toml`).
omiologic-aegra는 `redact-secret==0.1.0b4`/`opentelemetry-sdk` 1.44대를
쓴다 — 둘은 같은 프로세스에 공존할 수 없다. `cord-runtime`은 PyPI에도 없다.
콜백 통합은 그래서 **자기만의 배포**가 필요하다.

## Decision

### 1. 별도 배포 `cord-langgraph-callbacks` — `cord-runtime`을 의존하지 않는다

`cord-langgraph-callbacks/`(저장소 최상위, `aegra/`와 같은 패턴 — 자기
`pyproject.toml`, `cord-runtime`과 무관)에 새 배포를 둔다. 의존성은
`opentelemetry-api`/`langchain-core`뿐이고 둘 다 **범위** 지정이다
(`redact-secret`/`opentelemetry-sdk`는 아예 의존하지 않는다 — 엔티티가
이미 고정한 버전과 절대 충돌할 수 없다). `TracerProvider`를 만들거나
설치하지 않는다 — 엔티티가 이미 구성한 redacted provider(ADR-0006, #28)에서
얻은 `Tracer`를 인자로만 받는다.

`cord_runtime.execution.SEMCONV_VERSION`/span 이름/`cord.*` 속성 어휘는
**복제**한다(가져오지 않는다) — 두 패키지가 이 값들에서 벗어나면
`tests/test_langgraph_callbacks.py::test_semconv_version_matches_cord_runtime`가
잡는다. root `pyproject.toml`의 `dev` 그룹은 테스트 전용으로 이 패키지를
`path`/`editable` 소스로 문다(`aegra/pyproject.toml`이 `cord-runtime`을
문 것과 같은 패턴) — 실제 배포 경로에는 영향이 없다.

**검증(2026-09-18):** 휠을 빌드해 격리된 venv에 `redact-secret==0.1.0b4`
`opentelemetry-sdk==1.44.0`과 함께 설치, `uv pip check` "All installed
packages are compatible"를 확인했고, 선언된 `Requires-Dist`가
`opentelemetry-api`/`langchain-core` 둘뿐임과 `TracerProvider(`/
`set_tracer_provider(` 호출이 소스에 없음을 확인했다.

### 2. `CordCallbackHandler` — 태그·checkpoint_ns만으로 Run/Step 트리를 재구성

한 인스턴스 = 한 엔티티 호출. `graph_id`/`subject`/`subject_type`(새 Run)
또는 `resume=`(기존 Run의 trace 재개, 아래 §4)를 받는다.

- `on_chain_start`: `graph:step:\d+` 태그가 없으면 무시(라우팅 함수 등).
  있으면 `metadata["langgraph_node"]`/`["langgraph_checkpoint_ns"]`로 Step을
  연다 — 부모는 `checkpoint_ns.rsplit("|", 1)[0]`이 가리키는, **이미 열려
  있는** Step(있으면) 또는 Run(없으면, 최상위). 이 부모 조회는 `cord_runtime.
  execution._open_step`을 흉내 낸 순수 OTel API 호출(`tracer.start_span`,
  context 명시)이다 — `start_as_current_span`이 아니다: 콜백은 이벤트
  기반이라 Python `with` 블록 하나로 시작/종료를 감쌀 수 없고, 병렬 분기가
  스레드를 넘나들며 "현재 span" contextvar를 놓고 다툴 수 있어서다.
- `on_chain_end`/`on_chain_error`: LangChain 자신의 `run_id`(콜백에
  `metadata` 없이도 항상 오는 유일한 안정적 키)로 열린 Step을 찾아 닫는다.
  `GraphInterrupt`(`langgraph.errors`, 지연 import — 이 배포는 `langgraph`를
  의존하지 않지만 실제 엔티티 프로세스엔 항상 있다)면 `cord.outcome=
  awaiting_approval`, span 상태는 에러 아님(ADR-0008과 동일 규칙). 그 외
  예외는 `failed`+에러 상태.
- `close()`(또는 컨텍스트 매니저): Run span을 실제로 끝내 내보낸다. 이
  핸들러는 LangGraph 자신의 "그래프 전체 호출"이라는 별도 이벤트를 추적하지
  않는다 — 그런 이벤트가 항상 오는지 신뢰할 근거가 없었다. 대신 엔티티가
  자기 `invoke()` 호출을 감싸고 끝나면 명시적으로 닫는다. `graph:step`
  이벤트가 하나도 없었다면 Run 자체가 기록되지 않는다 — ADR-0015("연결·기록은
  span이 있어야 한다")와 그대로 맞는다.
- **Attempt는 만들지 않는다.** 콜백은 Node 실행 경계만 본다 — 그 안에서 모델
  재시도가 몇 번 있었는지는 안 보인다(#89 범위: "No Attempt or escalation
  inference from callbacks"). Step만 기록한다.

### 3. Subject/graph_id는 생성자 인자다 — `on_chain_start`의 `metadata`에서 읽지 않는다

이슈 본문은 "Subject from `config["configurable"]["cord_subject"]`"라고
썼지만, 실제 설치된 `langchain-core` 소스를 읽어 확인한 사실은 다르다:
`configurable`은 `patch_config`가 매 Pregel task마다 **병합**해 아래로
흐르지만(`config["configurable"] = {**config.get("configurable", {}),
**configurable}`), 일반 `BaseCallbackHandler`가 받는 `on_chain_start`의
`metadata` 인자에는 **들어오지 않는다** — `get_callback_manager_for_config`가
`configurable`을 메타데이터로 승격하는 경로(`langsmith_inheritable_metadata`)는
`LangChainTracer`(LangSmith 전용)에만 적용되고 일반 핸들러엔 적용되지 않는다
(`copy_with_metadata_defaults`, langchain_core 1.6.3 `callbacks/manager.py`).

그래서 이 ADR은 이슈 문구를 정정한다: `cord_subject`는 엔티티가 **이미 알고
있는** 값이다 — `configurable["cord_subject"]`를 만든 바로 그 코드가
`CordCallbackHandler(tracer, subject=..., ...)`도 만든다. 콜백이 `config`를
훔쳐보는 메커니즘은 없고, 필요하지도 않다 — 증거의 "config["callbacks"]로
붙였다"는 서술과 정확히 일치한다.

### 4. 재개: `cord.resumed_from`은 엔티티가 넘겨야 한다 — 그래프도, 콜백 혼자서도 못 한다

`resume_run`(#28)은 그래프가 checkpoint된 state 안에 `RunContinuation`을
직접 들고 다녀야 동작했다 — 그건 정확히 이번 이슈가 금지한 "그래프 코드
변경"이다. 콜백 혼자서는 이전 호출의 Step span id를 알 방법이 전혀 없다 —
LangGraph의 공개 인터럽트/재개 계약은 그 값을 checkpoint에도, config에도
노출하지 않는다.

**검증된 메커니즘(범위가 명시적으로 허용한 대로 — "entity-side persistence
across the call boundary", 그래프 자체는 손대지 않는다):** 인터럽트가 발생한
호출에서 `handler.awaiting_approval_steps`(각 항목이 `span_id`/`node`/
`checkpoint_ns`)와 `handler.continuation`(`RunContinuation`, trace 재개용)을
읽어 **엔티티가** 자기 상태(Thread id로 키)에 저장한다. 재개할 때 새
`CordCallbackHandler(tracer, resume=continuation, resumed_from=span_id)`를
만들어 붙인다 — 첫 Step이 그 `resumed_from`을 받는다.

이건 검증되지 않은 채 문서화만 된 한계가 아니다 —
`tests/test_langgraph_callbacks.py::test_resume_links_cord_resumed_from_across_two_handler_instances`가
실제로 두 번 `graph.invoke`를 부르고(하나는 인터럽트, 하나는
`Command(resume=True)`), `cord.resumed_from`이 정확히 연결되고 trace가
하나(Run span도 하나)임을 확인한다. 문서화해야 하는 진짜 한계는 이것이다:
**엔티티가 이 두 값을 실제로 저장/전달하지 않으면 연결이 안 생긴다** — 콜백은
"공짜로" 재개를 추론할 수 없다. `docs/langgraph-callbacks.md#resume`에 적는다.

## Options Considered

### Option A: `cord-runtime`의 새 extra(`cord-runtime[langgraph-callbacks]`)

| 차원 | 평가 |
|---|---|
| 코드 재사용 | 최대 — `cord_runtime.execution`을 그대로 import |
| 설치 격리 | **실패** |

**Pros:** `_open_step`/`RunContinuation`/`SEMCONV_VERSION`을 복제하지 않아도
된다. **Cons:** extra는 여전히 같은 배포(`cord-runtime`)의 `dependencies`를
전부 끌고 온다 — `pip install cord-runtime[langgraph-callbacks]`는
`redact-secret==0.1.0b1`/`opentelemetry-sdk==1.39.1`을 정확히 요구해
omiologic-aegra 환경과 그대로 충돌한다. 패키징 제약(Context)을 전혀 풀지
못한다.

### Option B: core observer(`agent-workflow-core`)를 계속 시도

| 차원 | 평가 |
|---|---|
| 실경계 시각 | 없음 — Attempt 결과만, 타임스탬프 없음 |

**Cons:** #52가 이미 기각한 이유가 여기도 그대로 적용된다 — 진짜 시작/종료가
없는 곳에 span 경계를 지어내는 것.

### Option C: 콜백 seam, 별도 배포, subject/graph_id는 생성자 인자 ← **채택**

Decision 절 그대로.

## Trade-off Analysis

**A를 버리는 결정적 이유는 패키징이지 코드가 아니다.** `execution.py`가
import하는 것은 사실 `opentelemetry.trace`/`opentelemetry.context`뿐이고
(SDK도, redact-secret도 아니다) — 하지만 pip은 **파일이 실제로 무엇을
import하는지**가 아니라 **배포가 선언한 `dependencies`**로 설치 가능성을
정한다. 그래서 코드 재사용이 아니라 **어느 배포에 속하는가**가 유일하게
문제였다.

**어휘 복제는 의도적 비용이다.** `SEMCONV_VERSION`/span 이름/`cord.*` 속성을
두 곳에 유지하는 것은 명백히 중복이다. 대안(공유 서브패키지를 셋으로 쪼개
`cord-runtime-vocabulary` 같은 세 번째 배포를 만드는 것)은 이 슬라이스
하나를 위해 배포를 하나 더 늘리는 것이라 지금은 과하다고 판단했다 — 대신
교차 검증 테스트(`test_semconv_version_matches_cord_runtime`)로 드리프트를
잡는다. Consequences에 재검토 조건을 적는다.

**이슈 문구의 `config["configurable"]` 서술을 그대로 구현하지 않은 이유.**
설계 문서 문구보다 실제 설치된 라이브러리 소스가 우선한다 — 이 저장소의
`AGENTS.md` "일 좀 똑바로 하자" 원칙과 같다: 검증 비용이 실행 비용보다 훨씬
작을 때는 반드시 먼저 검증한다. `langchain-core==1.6.3`을 직접 읽는 데 몇
분이면 됐고, 그 결과가 실제 동작 가능한 유일한 설계(생성자 인자)를 정확히
가리켰다.

## Consequences

### 쉬워지는 것

- 재사용 가능한 라이브러리 그래프가 Cordboard를 전혀 몰라도 Run/Step이
  기록된다 — `examples/callback_child_graph.py`/`callback_interrupt_graph.py`
  둘 다 `cord_runtime` import가 하나도 없다.
- 기존 아카이브 계약(`query_spans`)·뷰어 오버레이(`run_topology`, ADR-0021)를
  전혀 바꾸지 않는다 — 콜백은 그 계약이 이미 받아들이는 모양의 span을 만들
  뿐이다.
- 엔티티는 자기 환경의 `redact-secret`/`opentelemetry-sdk` 버전을 그대로
  유지한 채 이 통합만 추가로 설치할 수 있다.

### 어려워지는 것

- **어휘가 두 배포에 흩어진다.** `SEMCONV_VERSION`이 바뀌면 두 곳을 함께
  고쳐야 하고, 테스트가 잊지 않게 잡아 줄 뿐 자동으로 동기화되진 않는다.
- **재개 연결은 엔티티의 몫이다.** 그래프도, 콜백도 대신해 줄 수 없다 —
  통합 가이드를 안 읽은 엔티티는 조용히 `cord.resumed_from` 없는 새
  Step만 얻는다(에러는 아니다 — 그냥 링크가 없다).
- **`NS_SEP`/checkpoint_ns 모양은 LangGraph의 문서화된 공개 계약이 아니다.**
  `langgraph.pregel`이 내부 구현을 바꾸면(태그 이름, 구분자) 이 통합이
  깨진다 — `agent-topology`의 `subgraphId`처럼 버전 범위를 추적해야 하는
  종류의 의존이다.

### 다시 볼 조건

- **두 배포의 `cord.*` 어휘가 실제로 어긋나는 사례가 나오면** → 공유 상수
  전용 세 번째 배포(Option A의 변형)를 다시 고려.
- **다른 프레임워크(LangGraph 아닌)용 콜백 통합이 필요해지면** → 이 ADR의
  "checkpoint_ns 파싱"은 LangGraph 전용이니, 프레임워크 중립 부분(Run 열기,
  redaction 없음, TracerProvider 미설치)만 뽑아낼지 검토.

## Action Items

1. [x] 새 배포 `cord-langgraph-callbacks/` — `opentelemetry-api`/
   `langchain-core` 범위 의존만, `cord-runtime` 비의존 (#89)
2. [x] `CordCallbackHandler` — `graph:step:N` 태그·`langgraph_checkpoint_ns`
   중첩으로 Run/Step 트리 재구성, `GraphInterrupt`→`awaiting_approval` (#89)
3. [x] 재개: `RunContinuation`/`AwaitingApproval`을 엔티티가 저장·전달하는
   `resume=`/`resumed_from=` 경로 (#89)
4. [x] 격리 venv에서 `redact-secret==0.1.0b4`/`opentelemetry-sdk==1.44.0`과
   충돌 없이 설치됨을 확인 (#89)
5. [x] `tests/test_langgraph_callbacks.py` — 2단 그래프, redactor 통과,
   아카이브 계약, 뷰어 오버레이, 인터럽트/재개, 미태그 이벤트 무시 (#89)
6. [x] ARCHITECTURE.md 갱신, 이 ADR 인덱스 등록, `docs/langgraph-callbacks.md`
   작성 (#89)
