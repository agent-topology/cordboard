# cordboard — 첫 고객 요구사항

`cordboard`가 `agent-topology`와 `redact-secret`의 첫 소비자로서 겪은 것.
**cordboard가 우회할 목록이 아니라 업스트림에 올릴 목록이다.** 우회를 만들면 스펙이 성숙하지 않는다.

검증 환경 — `agent-topology-langgraph` 0.1.0b2, LangGraph 1.2.11, Python 3.12.

---

## agent-topology

### AT-1 · `depth≥1`에서 부모 노드가 노드 목록에서 사라진다 🔴

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

---

### AT-6 · LangGraph 지원 범위가 좁다 🟡

현재 1.2.10–1.2.11. 벗어나면 그래프를 보기 전에 `UnsupportedLangGraphVersionError`.

cordboard는 "그래프마다 자기 의존성"을 원칙으로 두는데(ADR-0001), 이 제약이 그 약속에 구멍을 낸다 —
파이썬 버전은 달라도 되지만 LangGraph 버전은 공통 범위 안에 있어야 한다.
매니페스트가 없으면 등록도 안 되기 때문에 우회가 없다.

**필요한 것:** 지원 범위 확장 정책. 새 LangGraph 마이너가 나올 때 얼마나 빨리 따라가는지.

---

### 잘 되어 있는 것 — 기록해 둔다

- **beta.1 → beta.2에서 같은 그래프의 `structureHash`가 동일했다.** 포맷 안정성의 실측 증거
- `completeness.gaps`(그래프별)와 `producerLimitations`(생산자 전체) 분리. 항상 존재하는 한계가
  모든 문서를 incomplete로 만들지 않는다
- `edges[].kind`와 `nodes[].interrupts`가 코어에 있어, 거부 규칙이 확장을 읽지 않고 판정된다
- `agt`의 종료 코드 8종이 구분돼 있어, 호출자가 원인별로 다르게 대응할 수 있다

---

## redact-secret

### RS-1 · OTel SpanProcessor 통합 경로 🟡

cordboard의 배치 지점은 OpenTelemetry `SpanProcessor.on_end()`이고, 대상은 **span attribute 맵의
문자열 값들**이다 — 하나의 긴 텍스트가 아니라 짧은 문자열 다수.

지금 API로는 값마다 `scanAndRedact`를 부르면 된다. 다만 값이 수십 개인 span이 배치로
수백 개 들어오므로, **호출 단위가 적절한지 확인이 필요하다.**

**필요한 것:** 짧은 문자열 다수를 처리하는 권장 패턴. 또는 값 목록을 받는 API가 의미 있는지 판단.
incremental은 이 모양에 안 맞는다 — 값들이 서로 이어진 스트림이 아니다.

---

### RS-2 · `block` 처리의 호출자 계약 🟢

cordboard는 `block`을 **그 span 자체를 내보내지 않음**으로 해석한다. README의 서버 예제가
`findings.some(f => f.action === "block")`로 판단하므로 같은 방식이면 된다.

**확인만 필요:** `scanAndRedact`의 결과에서 `block` 범위도 치환된 텍스트로 돌아오는가,
아니면 호출자가 텍스트를 버려야 하는가. 후자라면 문서에 명시가 있으면 좋다.

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

### RS-5 · 릴리스 가용성 🟡

레지스트리 설치는 승인된 릴리스 이후에만 가능하다. cordboard 슬라이스 0은
**마스킹 없이 시작**하기로 했으므로 블로커는 아니지만, 0.1.0-beta.1이 올라오면
그 시점에 ①계층을 붙인다.

---

## 이 문서를 쓰는 이유

두 프로젝트가 cordboard보다 오래 살 가능성이 높다. cordboard는 특정 조합에 용접된 개인 도구이고,
저 둘은 생태계의 빈자리를 채우는 물건이다.

그래서 **cordboard가 우회를 만들면 두 번 손해다** — 우회 코드가 남고, 스펙은 그 요구를 못 듣는다.
첫 고객의 역할은 문제를 안고 가는 게 아니라 **문제를 보고하는 것**이다.
