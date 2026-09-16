# cordboard — 첫 고객 요구사항

`cordboard`가 `agent-topology`와 `redact-secret`의 첫 소비자로서 겪은 것.
**cordboard가 우회할 목록이 아니라 업스트림에 올릴 목록이다.** 우회를 만들면 스펙이 성숙하지 않는다.

검증 환경 — `agent-topology-langgraph` 0.1.0b2, LangGraph 1.2.11, Python 3.12.
아래 상태표는 0.1.0b3로 다시 실측했다.

---

## 현재 상태 — 내부 의존성 소스 점검 (2026-09-15)

여섯 저장소의 정확한 커밋, 태그, 실제 pin과 구현 근거는
[내부 의존성 점검](../internal-dependencies.md)에 기록했다. 아래 2026-09-12 표는
beta.3 실측 이력이며, 현재 개발 소스 상태는 다음과 구분한다.

- **AT-1:** beta.3에서는 남아 있지만 beta.4 후보 소스에서 해결됐다. 부모 node를 유지하고
  `subgraphId`로 별도 `graphs[]` 자식을 가리킨다. `expanded-subgraph-metadata`는 후보에서
  폐기됐다. 후보 artifact 검증은 기록되어 있으나 배포는 아직 대기 중이다.
- **AT-2·AT-3:** 여전히 실험적 해석이다. materialized child가 있는 graph는 revision `"2"`를
  쓰며 revision `"1"`만 아는 소비자는 이를 불투명하게 취급한다. 동적 interrupt는 여전히
  정적 topology에서 알 수 없다. **AT-4·AT-5**의 기존 결론은 유지한다.
- **AT-6:** Python LangGraph 1.2.10–1.2.11 범위는 후보에서도 그대로다.
- **RS:** upstream beta.2와 Omiologic의 `0.1.0b2` pin을 확인했다. Cordboard는 계속
  `0.1.0b1`이며 아래 RS-1/2/5는 그 버전의 검증 이력이다. HEAD의 추가 탐지 수정은
  unreleased이고, 이번 문서 점검에서 패키지 업그레이드나 관문 재검증은 하지 않았다.
- `agent-workflow-core`의 observer/event/notification 계약, `git-agent`와 `campaign-agent`의
  graph factory, Omiologic의 Aegra 조합 경계가 구현됐다. 하지만 entity의
  `issue_resolution` observer는 현재 no-op이며 topology는 파일 생성까지다.
  이 구현들을 Cordboard 연결·기록 완료로 바꾸어 읽지 않는다.

ADR-0014·0015의 경계는 유지한다. 위 항목은 연결 거부 조건이 아니며, Cordboard에
graph policy나 per-graph 설명 파일을 추가할 근거도 아니다.

## 이전 실측 — 0.1.0b3와 cordboard에서 걸리는 곳 (2026-09-12)

[ADR-0014](0014-no-graph-descriptors.md)와 [ADR-0015](0015-never-block-connection.md) 이후
cordboard는 그래프를 묘사하지 않고, 매니페스트는 뷰어의 선택적 입력이며, 카탈로그 규칙은
전부 경고다. 그래서 아래 어느 항목도 **연결을 막지 않는다.** 표의 마지막 열은 "업스트림이
해결했나"가 아니라 "cordboard가 이걸 실제로 어디에 쓰나"다.

| 항목 | 0.1.0b3 실측 | cordboard에서 걸리는 곳 |
|---|---|---|
| AT-1 서브그래프 부모 소실 | **열림.** `depth=1`에서 부모 `sub`가 노드 목록에 없다. 해석 확장은 `sub:inner`를 `scope-not-inspected`로 표시하고 `expanded-subgraph-metadata` 갭을 낸다 | 뷰어의 span 대조. 그래프가 스스로 `depth≥1`을 고를 때만. 기본은 0 |
| AT-2 sentinel | **실험적 해결.** 프레임워크 중립 `x-topology-interpretation.nodes[].sentinel`(revision 1). 코어는 그대로이고 `entryNodeIds`는 여전히 `__start__` | 뷰어가 `__start__`/`__end__`를 그릴지. 표시 문제 |
| AT-3 팬아웃 병렬 의미 | **실험적 해결.** 팬아웃 노드에 `branch.value = "all-declared"`(revision 1). 코어는 아님 | R3 **경고**의 품질. `unknown`이면 "확인할 수 없음" |
| AT-4 joins | **정의로 해결.** `joins`는 명시적 AND 합류다. 일반 엣지 둘이 모이는 것은 join이 아니다. 헬퍼 `derived_join_edges(structure)` | 뷰어의 합류점 표시 |
| AT-5 이름·id | **전제가 틀렸고 이후 해결.** 이름은 `compile(name=...)`, id는 `describe(graph_id=...)` | cordboard의 요구가 아니다. 문서를 합치거나 고치지 않는다 |
| AT-6 LangGraph 범위 | 릴리스 노트상 1.2.10–1.2.11 그대로 (범위 밖 버전은 실측하지 않았다) | 뷰어가 **그림을 그릴 수 있는** 범위. 범위 밖 그래프도 그림 없이 연결·기록된다 |

같은 그래프의 `structureHash`는 기본 id에서 0.1.0b2와 0.1.0b3가 같았다(`192398ec…`).

---

## agent-topology

### AT-1 · `depth≥1`에서 부모 노드가 노드 목록에서 사라진다 — beta.3 이력

> **2026-09-15 정정.** 아래 재현은 beta.3 기준이다. beta.4 후보의 부모 보존·자식 참조
> 계약이 이 문제에 답한다. 배포·Cordboard 채택 여부는 위 현재 상태와 구분한다.

```
depth=0   nodes: [__start__, __end__, prep, sub]
depth=1   nodes: [__start__, __end__, prep, sub:inner]
```

`sub`가 없어진다. 그런데 **런타임에 그 노드가 내는 Span의 이름은 여전히 `sub`다.**
토폴로지-트레이스 상관을 하는 소비자는 매니페스트에 없는 노드의 실행 증거를 받게 되고,
그걸 `unmatched`로 분류할 수밖에 없다 — 실제로는 정상 실행인데.

**필요한 것:** 확장된 문서가 부모 노드를 유지하거나(자식을 담은 컨테이너로),
자식 id에서 부모를 복원하는 규칙이 문서에 명시되거나, 둘 중 하나.

**cordboard에 미치는 영향:** 서브그래프를 쓰는 그래프에서 뷰어가 거짓 미스매치를 보고한다.
당장은 `depth=0`으로 회피 가능하므로 블로커는 아니다.

---

### AT-2 · sentinel 판별이 코어에 없다 🟡

```jsonc
"entryNodeIds": ["__start__"],          // 실제 첫 노드가 아니라 sentinel
"nodes": [{ "id": "__start__", "x-langgraph": { "sentinel": true } }]
```

`entryNodeIds`가 sentinel을 가리키므로, "실제 시작 노드"를 알려면 sentinel을 걷어내야 한다.
그런데 **sentinel 여부가 `x-langgraph`에만 있다.**

즉 **프레임워크 중립인 소비자가 쓸 수 없다.** LangGraph 문서에서는 `x-langgraph.sentinel`,
Temporal 문서에서는 또 다른 확장을 봐야 한다. 그건 코어 문서를 읽는 의미를 줄인다.

**필요한 것:** 코어에 sentinel 표시. 예를 들어 `nodes[].kind: "sentinel" | "node"`,
또는 `entryNodeIds`가 sentinel이 아닌 실제 진입 노드를 가리키도록.

**cordboard에 미치는 영향:** 뷰어가 `__start__`/`__end__`를 그릴지 판단하려면 확장을 읽어야 한다.
지금은 LangGraph만 쓰므로 동작은 하지만, 코어를 읽는 코드가 확장에 의존하게 된다.

---

### AT-3 · `direct` 팬아웃의 병렬 의미가 명시되지 않았다 🟡

문서는 `edges[].kind`로 `direct`와 `conditional`을 구분한다. 구조적 구분은 충분하다.
다만 README의 Known limits가 *"여러 목적지가 대안인지 전부 실행되는지는 확립하지 않는다"* 고 적는다.

cordboard는 이 구분 위에 **거부 규칙**을 세운다 — 동시에 실행되는 노드들이 인터럽트를 걸면
LangGraph에서 인터럽트 ID가 충돌해 그 Run이 영구히 재개 불가가 된다(langgraph#6626).
따라서 "한 source에서 `direct` 엣지가 2개 이상 = 동시 실행"이라는 해석에 의존한다.

**필요한 것:** 그 해석이 스펙의 의도인지 확인. 의도라면 코어 문서에 명시하고,
아니라면 병렬 여부를 표현할 코어 필드를 논의.

**cordboard에 미치는 영향:** 거부 규칙의 근거. LangGraph에서는 현재 해석이 맞으므로 동작한다.

> **2026-09-12 정정.** R3은 거부가 아니라 경고다([ADR-0015](0015-never-block-connection.md)).
> 0.1.0b3의 실험적 branch 해석이 이 질문에 답했고, 확인되지 않는 경우는 경고가 불확실성으로 표시한다.

---

### AT-4 · `joins`가 비어 있다 🟡

```jsonc
// review_a → join, review_b → join 인 그래프에서
"joins": []
```

두 엣지가 같은 target으로 모이는데 `joins`가 비어 있다. 필드의 의도가
"암묵적 합류"가 아니라 "명시적 join 선언"이라면 정상이지만, 문서에서 구분이 안 된다.

**필요한 것:** `joins`의 정의. 그리고 암묵적 합류를 소비자가 어떻게 찾아야 하는지.

**cordboard에 미치는 영향:** 뷰어가 합류 지점을 표시하려면 엣지를 직접 집계해야 한다.

---

### AT-5 · 그래프 이름을 호출자가 줄 수 없다 🟢

```python
describe(compiled_graph)   # → id: "main", name: "LangGraph"
```

여러 그래프를 등록하는 소비자는 이름이 전부 같아진다. cordboard는 `cord.yaml`의 이름으로
문서를 덮어쓰는데, **그건 생성된 문서를 소비자가 수정하는 것**이라 "파생된 것은 손대지 않는다"는
이 프로젝트의 원칙과 어긋난다.

**필요한 것:** `describe(..., name=...)` 또는 이름 소유권이 소비자에게 있다는 명시.

> **2026-09-12 정정.** 이 항목의 전제가 틀렸다. 표시 이름은 원래 `StateGraph.compile(name=...)`로
> 그래프가 정할 수 있었고, `describe()`가 그 값을 그대로 옮긴다. 실제 결함은 graph id가 `main`으로
> 고정된 것이었고, [agent-topology#127](https://github.com/agent-topology/agent-topology/issues/127)을
> 거쳐 0.1.0b3의 `describe(graph_id=...)`로 해결됐다. cordboard는 생성 문서를 덮어쓰지도 합치지도
> 않으므로([ADR-0014](0014-no-graph-descriptors.md)) 이 항목은 cordboard의 요구가 아니다.

---

### AT-6 · LangGraph 지원 범위가 좁다 🟡

현재 1.2.10–1.2.11. 벗어나면 그래프를 보기 전에 `UnsupportedLangGraphVersionError`.

cordboard는 "그래프마다 자기 의존성"을 원칙으로 두는데(ADR-0001), 이 제약이 그 약속에 구멍을 낸다 —
파이썬 버전은 달라도 되지만 LangGraph 버전은 공통 범위 안에 있어야 한다.
매니페스트가 없으면 등록도 안 되기 때문에 우회가 없다.

**필요한 것:** 지원 범위 확장 정책. 새 LangGraph 마이너가 나올 때 얼마나 빨리 따라가는지.

> **2026-09-12 정정.** "매니페스트가 없으면 등록도 안 되기 때문에 우회가 없다"는 더 이상 맞지 않다.
> 매니페스트는 연결의 전제조건이 아니다([ADR-0015](0015-never-block-connection.md)). 범위 밖 그래프는
> 그림 없이 연결·기록되므로, 이 항목은 연결 조건이 아니라 뷰어가 그림을 그릴 수 있는 범위의 문제다.
> 범위를 넓혀 달라는 요구 자체는 여전히 유효하다.

---

### 잘 되어 있는 것 — 기록해 둔다

- **beta.1 → beta.2에서 같은 그래프의 `structureHash`가 동일했다.** 포맷 안정성의 실측 증거
- `completeness.gaps`(그래프별)와 `producerLimitations`(생산자 전체) 분리. 항상 존재하는 한계가
  모든 문서를 incomplete로 만들지 않는다
- `edges[].kind`와 `nodes[].interrupts`가 코어에 있어, R3 규칙이 확장을 읽지 않고 판정된다 (R3은 이제 경고, ADR-0015)
- `agt`의 종료 코드 8종이 구분돼 있어, 호출자가 원인별로 다르게 대응할 수 있다

---

## redact-secret

### RS-1 · OTel 공개 exporter 통합 검증 ✅

2026-09-11, Python 3.12.14 / macOS arm64에서 `redact-secret==0.1.0b1`,
OTel SDK 및 OTLP 패키지 `1.39.1`을 설치해 검증했다.
`SimpleSpanProcessor`가 호출하는 exporter 안에서 공개 `encode_spans()`로
OTLP 복사본을 만들고 문자열마다 공개 `scan_and_redact()`를 호출한다.
`ReadableSpan`의 내부 필드를 수정하지 않는다. 공유 resource/scope까지 처리한 후
네트워크로 보내므로 배출 이전 경계를 유지한다. 성능 매트릭스는 측정하지 않았다.

이 경로는 #5 픽스처에서 검증됐으며 LiteLLM/DeepAgents 연결까지 완료했다는 뜻은 아니다.
[구현 및 재현 명령](../archive.md), [회귀 테스트](../../tests/test_telemetry.py).

---

### RS-2 · `block` 호출자 계약 검증 ✅

공개 `scan_and_redact()`는 block 범위도 치환한 `result.text`를 반환한다.
`result.findings` 중 하나라도 `action == "block"`이면 Cordboard는 그 span 전체를
버린다. 치환 여부만으로 export를 허용하지 않는다. 합성 PEM 하나로 이 계약을
재현했고, resource/event/bytes 경로와 실제 Collector 아카이브에서도 억제를 검증했다.

---

### RS-3 · 기본 정책이 우리가 쓰려던 표와 같다 ✅

개인키 `block`, 알려진 자격증명 `redact`, 중·저신뢰 `warn`.
**cordboard는 정책을 정의하지 않고 기본값을 쓴다.** 요구사항이 아니라 기록이다.

---

### RS-4 · 커스텀 detector 불가를 수용한다 ✅

바인딩이 새 detector 구현을 만들 수 없다는 설계에 동의한다. detector가 언어마다 갈리면
탐지 드리프트가 조용히 생기고, 그건 보안 도구에서 최악의 실패다.

cordboard는 `cord.yaml`의 `redaction.extra_patterns` 계획을 폐기했다.
**우리 도메인 패턴이 실제로 새면 로컬 우회가 아니라 upstream detector로 제안한다.**

---

### RS-5 · 릴리스 가용성 확인 ✅

2026-09-11 정정: [v0.1.0-beta.1](https://github.com/redact-secret/redact-secret/releases/tag/v0.1.0-beta.1)
릴리스의 Python 배포판 `redact-secret==0.1.0b1`을 레지스트리에서 설치했다.
`uv sync --locked` 및 최소 credential/private-key/ordinary-text 회귀가 통과했다.
Rust CLI도 같은 릴리스 바이너리와 SHA256SUMS로 확인했다.

이전의 **마스킹 없이 시작**한다는 문장은 폐기한다. 미처리/차단 span은 아카이브에
들어갈 수 없다. 릴리스 대기는 더 이상 블로커가 아니다.
실제 명령, 고정 버전, 한계는 [아카이브 문서](../archive.md)에 기록한다.

---

## 이 문서를 쓰는 이유

두 프로젝트가 cordboard보다 오래 살 가능성이 높다. cordboard는 특정 조합에 용접된 개인 도구이고,
저 둘은 생태계의 빈자리를 채우는 물건이다.

그래서 **cordboard가 우회를 만들면 두 번 손해다** — 우회 코드가 남고, 스펙은 그 요구를 못 듣는다.
첫 고객의 역할은 문제를 안고 가는 게 아니라 **문제를 보고하는 것**이다.


## LiteLLM — 공개 callback 통합 (2026-09-11, #8)

LiteLLM 1.87.0의 기본 OTel handler에서 `_get_span_context`는 metadata 부모를
context에 넣지만 두 번째 반환값을 `None`으로 준다. 따라서 성공 handler가
별도 `litellm_request`를 만들며 직접 Attempt 자식은 proxy request span뿐이다.
`USE_OTEL_LITELLM_REQUEST_SPAN=false`만으로 직접 model 부모 계약을 만들 수 없다.

공개 `CustomLogger.async_log_success_event`/`async_log_failure_event`로 충분히
구현할 수 있어 통합 블로커는 아니다. callback kwargs의 원래 HTTP `traceparent`를
공개 OTel propagator로 추출하고 metadata만 담은 자식 span을 만든다. SDK 내부나
기존 span을 수정하지 않는다. 같은 `RedactingOTLPExporter`가 proxy 안에서 실행된다.

[최소 실제 프록시 재현 테스트](../../tests/test_proxy.py)는 직접 부모 ID,
승격 1회, 같은 별칭의 모델 매핑 교체, credential 치환과 PEM block을 검증한다.
[설정과 검증 기록](../model-proxy.md). 실제 provider 자격증명은 없어 smoke 미실행이다.
이 기록은 RS-1의 #5 범위를 확장하며 DeepAgents 연결을 완료했다는 뜻은 아니다.

## Aegra — 공개 실행 계약 확인 (2026-09-11, #9)

`aegra-api==0.10.4`의 요청별 async context-manager factory를 사용한다.
팩터리의 실행 수명 안에서 Run span을 만들고 기존 graph를 반환하면 Aegra가
Postgres checkpointer를 붙여 실행한다. schema/state 조회에는 Run span을 만들지
않는다. 내부 실행기나 저장된 span을 수정하지 않는다.

공개 Run 제출은 `command.resume`에도 새 API Run ID를 발급한다. 서버가 graph
config에 run_id/thread_id를 고정한다. 이는 API 결함이 아니라 Cordboard가 가정한
논리 Run과 API 호출 정체성이 다르다는 발견이다. [ADR-0003 정정](0003-subject-not-thread.md)에
반영했으며 복수 API 호출을 같은 논리 Run으로 잇는 재개는 이번 범위 밖이다.

LiteLLM 1.87.0은 Uvicorn 0.33.0 고정, Aegra는 >=0.36.0 요구로 한 환경에서
해결되지 않았다. 별도 uv 프로젝트와 lockfile로 실제 프로세스 경계를 반영했다.
업스트림 제약을 무시하거나 낮은 버전을 강제로 설치하지 않았다.

기본 Aegra 관측 exporter는 꺼 두고 graph 팩터리에서 기존 공개 redacting exporter만
사용한다. 요청별 instrumentation은 closure에 두고 직렬화되는 config에는 넣지 않는다.
동기 Attempt subgraph는 checkpoint를 상속하지 않고 Step 단위 상태를 호스트가
저장한다. [실제 OTLP·DB 검증과 한계](../aegra.md).


## 교환원 경계 정정 (2026-09-11, #7)

[ADR-0013](0013-switchboard-boundary.md)에 따라 위 LiteLLM 검증은 선택적 그래프 예제의
통합 기록이다. 공용 게이트웨이·모델 설정·공급자 키는 Cordboard 요구사항이 아니다.
모델 없는 그래프에도 동일한 실행 API와 redaction/Collector 계약을 적용한다.
