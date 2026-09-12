# ADR-0012: 그래프 구조 포맷을 독립 프로젝트로 분리한다

**Status:** Accepted
**Date:** 2026-09-10
**Deciders:** 단독
**관련:** ADR-0002 (어휘), ADR-0009 (well-known 두 문서), ADR-0011 (Manifest 파생)
**산출물:** [`agent-topology/agent-topology`](https://github.com/agent-topology/agent-topology) — 별도 조직·레포, MIT, **0.1 public preview**

> **갱신 2026-09-10.** 프로젝트가 이 ADR이 예상한 것보다 훨씬 멀리 갔다. 패키지 넷이 공개 레지스트리에 있다 —
> `agent-topology-spec` / `agent-topology-langgraph` (PyPI), `@agent-topology/spec` / `@agent-topology/langgraph` (npm).
> TypeScript 생산자(LangGraph.js)와 정본 JSON Schema, 그리고 첫 소비자 예제(topology-to-trace 상관)까지 있다.
> **"두 생산자가 포맷을 잡는다"는 0.1 조건이 이미 충족됐다.**

---

## Context

ADR-0011에서 Manifest를 `declared` / `derived` 두 절로 나눴다. `derived` 절은 컴파일된 그래프에서 뽑은 순수한 구조 — 노드, 엣지, 분기, 루프, 인터럽트 지점이다.

중간 점검에서 이 프로젝트의 오픈소스 가치를 따져 봤고, 결론이 갈렸다.

**cordboard 전체는 외부 가치가 낮다.** Aegra + Langfuse + LiteLLM + uv라는 특정 조합과 Subject·Tier 같은 특정 개념에 용접돼 있다. 남이 쓰려면 그 조합을 다 받아야 한다. 마켓플레이스를 거절한 것과 같은 논리가 우리 자신에게도 적용된다.

**그런데 `derived` 절만은 다르다.** 세 가지가 이걸 필요로 하는데 생태계에 없다.

- **관측** — 트레이스는 무엇이 실행됐는지 보여주지만, 무엇이 실행**될 수 있었는지**, 어느 분기를 안 탔는지는 못 보여준다. 그건 정의가 있어야 한다
- **감사** — "이 워크플로에 쓰기 전 사람 승인 단계가 있나"는 구조 질문이다. 지금은 소스를 읽어야 답한다
- **이식** — 오케스트레이터 간 비교나 마이그레이션에 공통 중간 서술이 없다

셋 다 지금은 프레임워크별 introspection을 각자 다시 구현한다.

### A2A가 이 자리를 채우지 않는다

A2A Agent Card는 **의도적으로 내부를 감춘다.** 스펙의 설계 원칙이 "동료는 계약을 볼 뿐 내부 툴셋은 보지 않는다"이다. 그건 결함이 아니라 목적이다.

그래서 **"안이 어떻게 생겼는가"를 표준화한 것이 없다.** 경쟁이 아니라 빈자리다.

### 분리하려면 지금이어야 한다

포맷을 cordboard 안에 두면 cordboard가 그것을 소유하고, 나중에 빼내는 것은 파괴적 변경이 된다. 지금 나누면 cordboard가 처음부터 **소비자**로 태어난다. 비용이 0인 시점이 지금뿐이다.

---

## Decision

### 1. `agent-topology`를 별도 레포로 분리한다

MIT. 독립 배포. **cordboard를 모른다** — 코드에 그 단어가 한 번도 나오지 않는다.

### 2. 함수 하나짜리 범위

```python
from agent_topology.langgraph import describe
doc = describe(compiled_graph)
```

```bash
agt describe src/my_agent/graph.py:graph --out agent-topology.manifest.json
```

패키지는 **스펙 유틸리티와 생산자로 나뉜다.** 소비자는 프레임워크 런타임 없이
`agent_topology.spec` / `@agent-topology/spec` 만으로 검증·정규화·해싱할 수 있다.
cordboard는 생산자를 의존성으로 걸고, 스펙 패키지는 따라온다.

검증 서버도, 뷰어도, 레지스트리도, 저장소도 없다. **"오후 한나절에 재구현할 수 있을 만큼 작을 것"** 이 설계 목표다.

### 3. 경계는 ADR-0011의 declared/derived 선을 그대로 쓴다

| 스펙 소관 | cordboard 소관 |
|---|---|
| nodes · edges · branches · fanouts · cycles | Subject 추출식 |
| entry · exit · 인터럽트 지점 · 서브그래프 | Tier 사다리 |
| completeness (파생 한계) · provenance | 승인 정책 · 동시성 · 트리거 |

**핵심 필드 판정 기준:** *Temporal, Airflow, CrewAI, LangGraph가 전부 이걸 뱉을 수 있는가.* 아니면 확장이다.

### 4. 확장은 `x-` 네임스페이스로

```jsonc
{
  "topologyVersion": "0.1",
  "graphs": [{
    "name": "issue-resolver",
    "structure": {},
    "completeness": {},
    "provenance": {},
    "x-cord": { "subject": {}, "approval": {}, "triggers": [] }
  }]
}
```

코어는 `x-*`를 절대 해석하지 않는다. 처음부터 확장 자리를 두는 것이 **남이 채택하는 통로**다.

### 5. 이름은 `agent-topology`, 파일은 `.manifest.json`

- **`topology`** — 연결 구조 그 자체를 뜻하고 방향에 중립적이다. `cascade`나 `flow`는 한 방향으로 흘러내리는 것을 뜻하는데, 담으려는 대상에는 루프와 조건부 분기와 병렬 팬아웃이 있다. 재시도 루프가 있는 그래프는 cascade가 아니다. 그리고 `cascade`는 우리 어휘에서 이미 Run 연쇄 깊이(`cord.cascade.depth`)를 뜻한다
- **`.manifest.json`** — 파일은 스펙이 아니라 스펙을 따르는 문서다. 스펙은 스키마이고 이건 인스턴스다. `package.json`을 `npm.spec.json`이라 하지 않는다
- **`agent-graph`가 아닌 이유** — RFC 8615가 일반 용어 선점을 말린다. 채택이 생긴 뒤에 제안한다

### 6. 서술적이고, 파생되고, 보완물이다

이 셋을 지키는 한 아무와도 경쟁하지 않는다. **이게 스펙이 살아남는 조건이다.**

| | |
|---|---|
| **서술적** | 그래프를 정의하지 않는다. 이미 컴파일된 것을 서술한다 |
| **파생됨** | 사람이 쓰지 않는다. 손으로 편집하면 그건 버그다 |
| **보완물** | A2A가 밖, 이것이 안 |

### 7. 적합성 픽스처가 스펙의 절반이다

```
conformance/fixtures/<case>/{fixture.json, expected.json}
```

**픽스처가 `fixture.json` 레시피다** — 언어별 러너가 그 레시피로 네이티브 프레임워크 객체를 만들고
같은 `expected.json`과 비교한다. 내 프로토타입은 `source.py`라 Python 전용이었다.
생산자가 여럿인 것이 목적이라면 픽스처도 언어 중립이어야 한다.

**생산자가 여럿인 것이 목적**이므로, 같은 픽스처를 모두가 돌려야 하나의 포맷으로 남는다. 픽스처는 테스트 코드가 아니라 데이터다 — 어떤 구현보다 오래 산다.

### 8. 릴리스는 채택을 따라간다

```
0.1   실제 그래프로 검증된 생산자 둘이 포맷을 잡은 뒤
0.9   두 번째 오케스트레이터가 코어 변경 없이 뱉을 때
1.0   두 소비자에서 6개월간 안정적일 때
그 후  스펙 문서 · IANA provisional 등록 · 그다음에 일반 이름 논의
```

**0.1 전에는 포맷이 바뀐다.** README에 명시한다.

---

## Options Considered

### Option A: cordboard 안에 둔다

**Pros:** 레포가 하나. 조율 비용이 없다. 필요할 때 바로 고친다.
**Cons:** cordboard가 포맷을 소유한다. 나중에 빼내려면 파괴적 변경. 그리고 **외부 가치가 가장 큰 조각이 외부 가치가 없는 물건 안에 갇힌다.**

### Option B: 스펙 문서를 먼저 쓰고 구현은 나중

**Pros:** 설계가 앞선다. 남들이 미리 읽고 의견을 준다.
**Cons:** **아무도 안 만들어 본 포맷은 픽션이다.** 그래프 하나로 상상한 스키마는 그 그래프의 모양일 뿐이다. 그리고 생성기 없는 스펙은 채택되지 않는다.

### Option C: 별도 레포, 생산자 먼저, 스펙은 나중 ← **채택**

**Pros:** 실제 출력에서 스키마를 뽑는다. cordboard가 첫 소비자로서 즉시 검증한다. 분리 비용이 지금은 0이다.
**Cons:** 레포가 둘이라 변경이 두 번. 그리고 draft 상태가 길어질 수 있다.

### Option D: LangChain에 기여한다

`get_graph().to_json()`을 확장하도록 upstream PR.

**Pros:** 채택이 보장된다. 유지보수를 넘긴다.
**Cons:** LangGraph 전용이 된다 — 다중 프레임워크가 이 포맷의 존재 이유인데 그게 사라진다. 그리고 upstream 일정에 종속된다.

---

## Trade-off Analysis

### 왜 생산자를 먼저 만들었나

실제로 만들어 보니 **추측이 세 군데 틀렸다.**

1. 정적 인터럽트가 `node.metadata`에 있다 — compiled 객체를 따로 안 봐도 된다
2. 조건부 라벨이 `edge.data`에 살아 있다 — 분기 이유를 표시할 수 있다
3. path map 없는 조건부 엣지는 목적지가 빠지는 정도가 아니라 **존재하지 않는 엣지를 만든다**

셋 다 스키마 모양을 바꿨다. Option B로 갔으면 세 번 고쳤을 것이다.

### LangChain이 이걸 자체 기능으로 넣으면?

가능하다. 그런데 **스펙은 제품이 아니라서 그게 손해가 아니다.** 그쪽이 나오면 채택하면 되고, cordboard는 이미 스펙을 소비하는 구조라 생성기만 갈아 끼운다. 잃는 것은 0이고, 오히려 우리 것이 그 논의의 참조가 된다.

Option A였다면 cordboard 안에 묻힌 코드를 들어내야 했을 것이다.

### 조율 비용은 실제로 얼마인가

레포가 둘이면 변경이 두 번이다. 그런데 이 경계는 **자연 경계**다 — 구조 파생과 업무 정책은 같이 바뀌지 않는다. 새 승인 정책을 추가할 때 파생 로직은 안 건드리고, LangGraph API가 바뀌어도 cordboard는 안 건드린다.

같이 바뀌는 것을 나눴다면 비용이 컸겠지만, 이건 그렇지 않다.

---

## Consequences

### 쉬워지는 것

- cordboard가 파생 품질을 책임지지 않는다 — 적합성 픽스처가 한다
- LangGraph API 변경이 cordboard에 안 닿는다
- 다른 오케스트레이터를 지원하려면 생산자만 추가하면 된다
- 외부 가치가 있는 조각이 독립적으로 살 수 있다
- `x-cord` 확장 자리가 생겨서 남도 자기 확장을 붙일 수 있다

### 어려워지는 것

- **레포가 둘이다.** 변경이 두 번, 릴리스가 두 번
- **draft 의존.** cordboard가 pre-0.1 라이브러리에 의존한다. 버전 고정으로 완화하되 포맷이 바뀌면 따라가야 한다
- **같은 사람이 둘 다 관리한다.** `redact-secret`과 같은 위험 — cordboard의 편의를 위해 스펙을 왜곡할 유혹. **규칙: cordboard는 공개 API만 쓰고, 스펙에 cordboard 개념을 넣지 않는다**
- **채택이 안 될 수 있다.** 그래도 cordboard에는 쓸모가 있으므로 손해는 레포 하나의 관리 부담뿐이다

### 다시 볼 조건

- **두 번째 생산자(LCEL 등)를 만들었는데 코어를 고쳐야 하면** → 포맷이 LangGraph 전용이었다는 뜻. `structure` 스키마 재설계
- **1년간 외부 채택이 0이면** → 스펙 문서·등록 계획을 접고 cordboard 전용 라이브러리로 강등. 코드는 그대로 쓴다
- **LangChain이 동등한 기능을 표준화하면** → 채택하고 우리 생산자를 어댑터로 축소
- **`x-cord` 외의 확장이 하나라도 생기면** → 확장 승격 절차를 정의한다

---

## Action Items

1. [x] 레포·조직 생성, README에 범위와 "무엇이 아닌가" 명시
2. [x] LangGraph(Python) 생산자 + LangGraph.js(TypeScript) 생산자
3. [x] 언어 중립 적합성 픽스처 8종 + 러너
4. [x] 정본 JSON Schema
5. [x] 0.1.0-beta.1 퍼블리시 (PyPI 2 · npm 2)
6. [ ] LangChain(LCEL) 또는 **구조적으로 다른** 생산자 — 지금 두 생산자는 같은 LangGraph 모델을 쓰므로 벤더 중립성이 아직 증명되지 않았다 (README가 직접 인정)
6. [ ] cordboard 쪽: `agent-topology`를 의존성으로 고정, 버전 명시
7. [ ] cordboard 쪽: `x-cord` 확장 스키마 정의 (ADR-0009 Action 1b)
8. [ ] **cordboard는 `agent-topology`의 공개 API만 쓴다**를 규칙으로 명시. 내부 경로 참조 금지


## 정정 — 교환원 경계 (2026-09-11, #7)

[ADR-0013](0013-switchboard-boundary.md)이 모델 소유권의 현재 결정이다.
`x-cord.tiers` 예제를 제거했다. Cordboard 확장은 연결·기록 정책을 담고,
모델 매핑이나 Tier 정책을 담지 않는다. upstream topology 형식은 변경하지 않는다.
