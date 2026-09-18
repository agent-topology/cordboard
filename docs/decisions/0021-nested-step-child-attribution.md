# ADR-0021: 자식 그래프 실행은 Step 아래 중첩 Step으로 기록한다 — semconv 0.4.0

**Status:** Accepted
**Date:** 2026-09-18
**Deciders:** 단독 (로컬 도구, 저자 = 운영자) — #86 구현과 동시에 결정
**관련:** ADR-0008 (Outcome 분리, span 트리 모양 — Attempt는 자식 span),
ADR-0002 (어휘, `cord.semconv.version` 갱신 절차), ADR-0012/0014/0015
(`subgraphId`는 문서 주소, 그래프 묘사 금지, 연결 비차단), #85 (재귀 렌더링)

## Context

부모 Node가 자식 그래프를 부른다 — beta.5 `declare_children`로 감싼 wrapper든,
직접 바인딩한 subgraph든. 오늘은 그 호출 안에서 자식 Node가 몇 개를 거쳤는지
전혀 기록되지 않는다. `cord_runtime.archive_query.query_spans`는 Step의 부모가
**항상 Run**이어야 한다고 강제한다(`parent(spans, span, "run")`, 158–199행).
`cord_runtime.viewer.build_execution_tree`도 같은 가정으로 Run → Step → Attempt
평평한 트리만 만든다. 중첩된 자식 실행은 오늘 이 계약 안에 표현할 자리가 없다.

`agent-topology` beta.5(#85)는 이미 정적 구조 쪽 절반을 풀었다 — 부모 Node의
`subgraphId`가 같은 문서의 `graphs[]` 항목을 가리키고, `cord_runtime.web.presentation.
expansions`가 그 자식 구조를 호출 지점(call site)별로 재귀 확장한다. 남은 절반은
**실행**이다: 그 부모 Node를 실제로 실행했을 때 자식 Node들이 뭘 했는지.
ARCHITECTURE.md가 이미 이 이슈로 이 절반을 명시적 gap으로 적어 뒀다(#86).

Cordboard는 그래프를 묘사하지 않는다(ADR-0014) — `declare_children` 선언이
맞는지 검사하는 것은 그래프 소유다. 우리가 하는 일은 **이미 일어난 실행**을
**이미 선언된** 문서 구조에 귀속시키는 것뿐이다. 이름 매칭이나 ID 파싱으로
추측하지 않는다(ADR-0014/0015) — 기록된 부모-자식 관계(span parentage)와
문서 참조(`subgraphId`)만 근거로 삼는다.

## Decision

### 1. 자식 Step은 부모 Step의 자식 span이다 — Attempt와 나란히, 새 계층 아님

```
span C  step:call-child          cord.outcome=passed
├ span D  attempt  attempt=1  tier=fast  outcome=passed        (이 Node 자신의 시도)
└ span E  step:a                cord.outcome=passed             (선언된 자식 호출)
  └ span F  attempt  attempt=1  outcome=passed                  (자식 Node "a"의 시도)
```

호출 관계가 실제로 중첩(nesting)이므로, ADR-0008이 Attempt에 span event 대신
자식 span을 고른 논거(지속시간이 있고, 자식을 가질 수 있어야 한다)가 여기도
그대로 적용된다. Step은 이미 Attempt를 자식으로 갖는 계층이니, 자식 그래프
호출도 같은 자리(Step의 자식)에 놓는다 — Attempt와 형제가 아니라, **또 다른
Step**이라는 점만 다르다. Node 실행을 기록하는 유일한 span 종류가 Step이므로,
자식 Node 실행도 Step이어야 한다는 것은 어휘가 이미 정한 것이다(ADR-0002).

**선택하지 않은 대안(Step 속성으로 부모를 명명):** 모든 Step을 Run의 형제로
평평하게 두고 `cord.parent_step_id` 같은 속성으로 호출자를 가리키는 방법도
있었다. 아래 Options Considered에서 기각한다.

### 2. 공개 헬퍼: `Step.child()`

`cord_runtime.execution.Step`에 `child(node, outcome, *, resumed_from=None,
pause=None)`를 추가한다. `Run.step()`과 같은 어휘(`StepOutcome`)와 같은
pause/resume 규약을 쓰되, 부모 span이 Run이 아니라 **이 Step 자신**이다.
`pause`를 넘기지 않으면 이 Step 자신의 pause 집합을 물려받는다 — 자식 호출도
그래프가 이미 선언한 인터럽트 타입만 통과시켜야 하기 때문이다.

내부적으로 `Run.step()`과 `Step.child()`는 하나의 헬퍼(`_open_step`)를
공유한다 — 둘의 차이는 부모 span과 "베이스" 속성(그래프/Run/Subject 정체성,
Node 이름 제외)뿐이다. `Step`은 이제 이 베이스 속성을 `base_attributes`로
따로 들고 다녀서, 손자 Step(자식의 자식)을 열 때도 조부모의 Node 이름이
새지 않는다.

이 헬퍼는 계측 지점일 뿐이다 — 실제로 `declare_children`이 선언한 것과
호출이 일치하는지는 그래프 책임으로 남는다(위 Context, ADR-0014 범위 밖).

### 3. `cord.semconv.version`을 `0.4.0`으로 올리고, `0.2.0`·`0.3.0`은 계속 읽는다

새 Run은 `cord.semconv.version=0.4.0`을 찍는다(`SEMCONV_VERSION`).
`archive_query.SUPPORTED_RUN_VERSIONS = ("0.2.0", "0.3.0", "0.4.0")` —
ADR-0013이 0.2.0→0.3.0에서 그랬듯, 이번에도 이전 버전을 버리지 않는다.
0.1.0을 버린 것(ADR-0002 #6 정정, Graph 정체성 자체가 없었다)과는 다르다 —
0.2.0/0.3.0 아카이브는 이미 Graph/Run/Subject 정체성을 완전히 갖추고 있고,
이번 변경은 그 위에 **허용되는 모양을 넓히는** 것뿐이다.

**중첩은 `0.4.0` Run에서만 유효하다.** `query_spans`가 Step의 부모가 다른
Step임을 발견하면, 그 Step 사슬을 거슬러 올라간 Run 루트의
`cord.semconv.version`이 정확히 현재 버전인지 검사한다(`run_root` 헬퍼,
158–229행 부근). `0.2.0`/`0.3.0` 계측은 애초에 이 모양을 만들 수 없었으므로,
그런 아카이브에서 이 모양을 만나면 새 증거가 아니라 손상이다 — 조용히
받아들이지 않고 명시적으로 거부한다("nested Step under Step requires
current cord.semconv.version"). 이 게이팅이 없으면 "현재 유효하지 않은
모양을 실수로 받아들이지 않는다"는 이번 확장 자체의 요구를 못 지킨다.

### 4. 귀속(attribution)은 아카이브 계약이 아니라 뷰어의 일이다

`query_spans`/`build_execution_tree`는 중첩이 유효한 **모양**인지만 본다 —
어느 자식 구조에 속하는지는 모른다(그 정보는 span에 없다, 위 Context).
`cord_runtime.viewer.build_execution_tree`는 각 Step 레코드에 `child_steps`를
추가해 트리를 그대로 보존한다. **귀속은 표시 시점 결정**이다:

- `cord_runtime.web.presentation.run_topology`가 부모 Node의 `subgraphId`를
  이미 상관된 `correlate_topology(...)["subgraphs"]`에서 찾아 해석되면, 그
  Step 발생(occurrence)의 `child_steps`를 그 자식 구조 위에 재귀적으로
  겹친다 — `expansions()`가 이미 쓰는 "주소가 아니라 호출 지점 기준" 규약을
  그대로 따라서, 같은 자식을 부르는 두 호출 지점과 같은 Node의 재시도가
  각자 별도 항목으로 남는다.
- `subgraphId`가 없거나(깊이 0, 선언 안 된 wrapper), 있어도 이 문서에서
  풀리지 않으면(`unresolved`) — 또는 토폴로지 자체가 표류/부재/무효라
  `correlate_topology`가 애초에 상관하지 않으면 — 기록된 `child_steps`는
  **추측 없이 미귀속 증거로만** 보여준다. `status` 필드(`resolved` /
  `undeclared` / `unresolved`)가 이유를 밝힌다.

## Options Considered

### Option A: 평평한 Step + `cord.parent_step_id` 속성

| 차원 | 평가 |
|---|---|
| 아카이브 계약 변경 | 없음 — 기존 "Step 부모는 항상 Run" 유지 |
| 트레이스 시각화 | 부정확 — OTel 뷰어가 실제 호출 중첩을 못 보여줌 |
| `run_root`/깊이 검증 | 불필요 |
| 새 속성 | `cord.parent_step_id` 하나 |

**Pros:** `query_spans`의 "Step은 항상 Run의 자식" 규칙을 안 건드린다.
**Cons:** span parentage가 이미 부모-자식 실행 관계를 정확히 표현하는
메커니즘인데, 그걸 두고 속성으로 같은 정보를 다시 인코딩하는 것은 중복이다.
표준 OTel 트레이스 뷰어(Jaeger 등)에서 열어 봐도 자식 호출이 평평하게
보여 실제 실행 구조와 어긋난다. `resumed_from`이 이미 "속성으로 관계를
표현"하는 예외적 패턴인데, 그건 재개가 **새 trace 위치**(같은 trace, 다른
시점)라 진짜 부모-자식 span 관계가 아니기 때문이었다(ADR-0008 추가절). 여기는
그 예외가 적용되지 않는다 — 자식 호출은 정확히 스택처럼 중첩된 실행이다.

### Option B: 자식 그래프를 완전히 새 Run으로 (`run.finished` cascade 재사용)

| 차원 | 평가 |
|---|---|
| 기존 계약 재사용 | 최대 — #18의 cascade 그대로 |
| 의미 정확성 | **틀림** |

**Pros:** 새 span 계층이 전혀 필요 없다 — `cord.caused_by.run_id`/
`cord.cascade.depth`를 그대로 쓴다.
**Cons:** `run.finished` cascade는 **비동기** 합성이다(ARCHITECTURE.md
"Isolation, routing, and approval") — 완전히 종료된 Run의 결과로 독립적인
새 Run/Thread/trace를 만드는 것. 여기 자식 호출은 **동기** 호출이다 — 같은
프로세스, 같은 trace, 부모가 자식이 끝나길 기다렸다가 계속한다. 서로 다른
질문에 답하는 기존 계약을 재사용하면 "이 Run이 다른 Run을 유발했나"와
"이 Node 실행이 자기 안에서 무엇을 실행했나"가 뭉개진다.

### Option C: Step 아래 중첩 Step, semconv 0.4.0 게이팅 ← **채택**

Decision 절 그대로. 어휘 확장 하나(`Step.child`), 버전 하나 추가, 기존
`role`/`parent` 헬퍼가 이미 하는 정체성·trace 일치 검사를 그대로 재사용한다.

## Trade-off Analysis

**B를 버리는 결정적 이유는 동기/비동기 구분이다.** 자식 그래프 호출을 새
Run으로 만들면 그 Run의 `end_ns`가 부모 Run의 execution_ns 계산에서
빠진다 — 부모가 기다린 시간이 통계에서 사라진다. 동기 호출은 부모 trace
안에 있어야 타임라인이 truthful하다(#46 AC5가 이미 이 원칙을 세웠다).

**A를 버리는 이유는 이중 인코딩이다.** OTel span parentage와 커스텀 속성이
같은 사실(누가 누구를 불렀나)을 두 번 말하면, 언젠가 어긋난다 — 그리고
어긋나는 순간 어느 쪽을 믿을지 정해야 한다. span 트리 하나만 진실을 말하게
둔다.

**게이팅이 필요한 이유.** 이 확장은 과거 아카이브가 만들 수 없었던 모양을
새로 허용한다. 게이팅 없이 그냥 "Step의 부모가 Step이어도 된다"고만 하면,
`0.2.0`/`0.3.0` 아카이브에서 우연히 또는 손상으로 그런 모양이 나타나도
조용히 통과한다 — "현재 버전으로 재계측하라"고 말해야 할 자리에서 새
증거인 척한다. 버전 검사가 그 구분을 명시적으로 만든다.

## Consequences

### 쉬워지는 것

- 자식 실행이 실제 실행 순서·중첩 그대로 trace에 남는다. 표준 OTel 도구로
  열어도 정확하다.
- 뷰어의 토폴로지 상관 로직(`correlate_topology`/`expansions`)을 그대로
  재사용해 귀속을 계산한다 — 새 상관 알고리즘이 필요 없다.
- 손자 Step(다단계 중첩)도 같은 메커니즘으로 자연히 지원된다 — 재귀 깊이
  제한을 따로 안 둔다.

### 어려워지는 것

- **아카이브 계약이 한 단계 더 복잡해진다.** `query_spans`가 이제 Step
  부모의 두 가지 유효한 역할(Run 또는 Step)을 분기해야 한다.
- **버전 게이팅을 매 확장마다 다시 결정해야 한다.** 이번엔 "이전 버전은
  이 모양을 못 만든다"가 명백했지만, 다음 확장이 항상 이렇게 깔끔하게
  나뉘지는 않을 수 있다.
- **그래프가 새 API를 배워야 한다.** `Step.child()`를 안 쓰면 자식 실행은
  여전히 안 보인다 — ADR-0008의 승격 선언과 같은 "헬퍼로 완화하되 부담은
  남는다" 패턴.

### 다시 볼 조건

- **중첩 깊이가 실제로 2를 넘는 사례가 나오면** → `run_root`의 순회 비용과
  뷰어 재귀 렌더링이 실제 규모에서 괜찮은지 재검토.
- **그래프가 `Step.child()`를 안 쓰고 직접 span을 열어 같은 모양을
  만들려는 시도가 관측되면** → 헬퍼 API가 발견하기 어렵다는 신호.

## Action Items

1. [x] `cord_runtime.execution.Step.child()` 추가, `Run.step`과 `_open_step`
   공유 (#86)
2. [x] `SEMCONV_VERSION`을 `0.4.0`으로, `archive_query.SUPPORTED_RUN_VERSIONS`에
   `0.2.0`/`0.3.0`/`0.4.0` 모두 유지 (#86)
3. [x] `query_spans`: Step 부모가 Step일 때 identity/trace 검증 + 소유 Run의
   semconv 버전이 현재 버전인지 게이팅 (#86)
4. [x] `build_execution_tree`: Step 레코드에 `child_steps` 추가, 종료
   시각·근사치 집계가 중첩 깊이와 무관하게 정확하도록 재귀 처리 (#86)
5. [x] `web.presentation.run_topology`: 호출 지점별로 `subgraphs`를 통해
   해석되는 자식 실행을 재귀 오버레이, 미해석/미선언은 미귀속 증거로만
   표시 (#86)
6. [x] ARCHITECTURE.md의 "#86 미구현" 문장 갱신, 이 ADR 인덱스 등록 (#86)
