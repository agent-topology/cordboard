# ADR-0002: 이름은 빌려 쓰고, 겹치는 곳은 매핑을 문서화한다

**Status:** Accepted
**Date:** 2026-09-10
**Deciders:** 단독 (로컬 도구, 저자 = 운영자)
**관련:** ADR-0003 (Subject), ADR-0005 (기록의 원본), ADR-0008 (Outcome 분리)
**보조 문서:** cordboard 용어 사전

> **정정 기록 세 건.**
>
> **하나 — 분류.** 처음에는 "어휘 결정이라 ADR 불필요"로 뒀다. ADR-0008을 쓰면서 틀렸다는 게 드러났다. 명사 하나(Outcome)의 정의가 아카이브의 물리적 행 구조를 결정했다.
>
> **둘 — 방향.** 첫 판의 제목은 *"겹치는 단어는 금지한다"* 였고, 금지어 목록이 Decision의 절반이었다. **그 결정이 틀렸다.** 이 문서는 금지를 걷어내고 매핑으로 대체한다.
>
> **셋 — Graph의 정의.** *"코드. 레포 하나"* 로 정의했는데, 그건 **패키징 관례를 도메인 정체성으로 착각한 것**이다. ADR-0003에서 Thread를 라벨로 착각한 것과 같은 종류의 실수다. 레포는 코드가 사는 곳이지 실행 단위가 아니고, 남의 컨테이너를 등록할 때 우리는 레포가 뭔지 알 수도 없다. Graph의 정체성은 **계약**에서 온다.

---

## Context

이 플랫폼은 아홉 개의 기존 이름 체계 위에 앉는다.

| 접두사 | 출처 |
|---|---|
| `otel.` | OpenTelemetry — SDK · Collector · semantic conventions |
| `lf.` | Langfuse |
| `lg.` | LangGraph |
| `lc.` | LangChain |
| `ap.` | Agent Protocol / Aegra |
| `da.` | DeepAgents |
| `llm.` | LiteLLM |
| `a2a.` | A2A Agent Card |
| `ss.` | redact-secret |
| `at.` | agent-topology (ADR-0012) |
| `wb.` | workbench (선례) |

이들이 같은 단어를 다르게 쓴다. 실제로 확인된 것만 스물넷이고, 그중 열하나는 같은 문맥에서 부딪힌다.

### 실제 충돌 사례 (전수 아님)

- **`Rule`** — 우리는 "Signal → Assistant 변환 선언". Langfuse는 "필터와 샘플링으로 observation을 골라 evaluator를 실행하는 설정". **양쪽 다 필터 + 트리거다**
- **`Span`** — OTel은 "부모를 가진 시간 구간". Langfuse는 그것을 **observation 타입 중 하나**로 좁혔다
- **`Agent`** — Langfuse의 observation 타입이자, DeepAgents의 산출물이자, A2A의 주소 단위이자, 일상어
- **`Graph`** — 우리는 코드 단위. LangGraph는 StateGraph. Langfuse의 "Agent Graph"는 **한 trace의 시각화 화면**
- **`Assistant`** — Agent Protocol의 등록 단위이자, Langfuse의 **제품 안 AI 도우미**
- **`Processor`** — OTel **자체가** 두 가지로 쓴다. SDK의 SpanProcessor와 Collector의 파이프라인 단계
- **`Signal`** — 우리는 외부 사건. OTel은 **텔레메트리 종류**(traces·metrics·logs)를 그렇게 부른다

### 첫 판이 틀린 이유

첫 판은 이 문제를 **금지어 목록**으로 풀려 했다. `event` `agent` `workflow` `task` `chain` `trace` `session` `thread` 등을 도메인 명사에서 추방하고 대체어를 지정했다.

그게 두 가지를 뭉갠 결과였다.

| | 정밀함이 값을 하는 곳 | 값을 하지 않는 곳 |
|---|---|---|
| 무엇 | 데이터에 굳는 이름 — 속성 키, 닫힌 목록의 값 | 사람이 말할 때 쓰는 단어 |
| 왜 | 아카이브가 append-only(ADR-0005). 잘못 쓰면 되돌리기가 비싸다 | 문맥이 해결한다 |
| 예 | `cord.chain.depth` → 잘못된 이름이 몇 주치 데이터에 굳는다 | "trace this run" → 무슨 뜻인지 아무도 안 헷갈린다 |

**첫 판은 왼쪽의 해법을 오른쪽에 확장했다.** 근거가 없었다.

그리고 실제 비용이 드러났다. `trace`를 금지어로 두니 "이 Run 좀 trace해봐"라는 자연스러운 문장에 대해 품사 판정과 대체 표현 표가 필요해졌다. 그건 도움이 아니라 마찰이다. **금지어는 이해를 개선하지 않고, 자연스러운 표현을 고쳐주려 드는 실패 모드를 만든다.**

### 무엇이 진짜 필요했나

필요한 것은 금지가 아니라 **"이 단어가 우리 문서에서 무슨 뜻이고, 우리가 otel·langfuse를 통해 그것을 어떻게 쓰는가"** 였다. Langfuse 자신의 글로서리가 정확히 그 형태다 — 금지 목록이 아니라 정의와 관계다.

---

## Decision

### 1. 명사 18개를 확정하고 각각 출처를 밝힌다

| 이름 | 출처 | 정의 |
|---|---|---|
| Graph | 우리 | **실행 가능한 오케스트레이션 단위 하나.** Manifest로 자기를 설명하고, Run을 받고, Span을 낸다 |
| Manifest | 우리 | `cord.yaml`(선언) + `at.structure`(파생). Graph의 정체성 그 자체 |
| Deployment | 우리 | Graph를 **하나 이상** 호스팅하는, 주소를 가진 프로세스 |
| Assistant | `ap.` | Graph + 고정 설정 |
| Run | `ap.` | 실행 하나. 모든 것의 단위 |
| Subject | 우리 | Run이 다루는 외부 대상 (ADR-0003) |
| Node *정적* | `wb.` | Graph 정의 안의 한 자리 |
| Step *동적* | 우리 | Node가 한 Run에서 실행된 것 |
| Attempt | `wb.` | Step 안의 시도 하나 |
| Tier | `wb.` | 모델 능력 등급의 이름 |
| Outcome | `wb.` | 처분. 두 층 (ADR-0008) |
| Verdict | `wb.` | 결정적 검증기의 판정 |
| Interrupt | `ap.` / `lg.` | 사람을 기다리며 멈춘 상태 |
| Approval | 우리 | Interrupt에 대한 답 |
| Rule | 우리 | Signal → Assistant 변환 선언 |
| Signal | 우리 | 외부에서 오는 사건 |
| Span | `otel.` | 노드마다 뱉는 기록 |
| Signal ID / **Cascade Depth** / Caused By | 우리 | 멱등성·연쇄 추적 |

### 2. 금지어 목록을 폐기한다

대신 **AWG Glossary**가 각 단어에 대해 세 가지를 적는다.

```
Trace
  otel.   같은 trace_id를 가진 span의 트리
  lf.     애플리케이션 요청 하나. 자체 필드를 가진 엔티티
  cordboard 문서에서 —
          한 Run이 남기는 span 전체. 1 Run = 1 trace이므로
          실무에서 둘은 같은 것을 가리킨다.
          데이터 모델을 논할 때는 Run, 화면·와이어·otel 얘기에는 trace.
```

**"trace this run"이 그대로 통한다.** 애매하면 매핑을 본다.

### 3. 정밀함은 두 곳에만 강제한다

**(a) `cord.*` 속성 키.** 데이터에 굳으므로 이름이 중요하다.

```
cord.run.id · cord.subject.id · cord.subject.type
cord.node.name · cord.step.attempt
cord.tier · cord.outcome
cord.signal.id · cord.cascade.depth · cord.caused_by.run_id
cord.redacted · cord.semconv.version
```

키 이름은 다른 층과 겹치지 않게 고른다. 겹쳐도 동작하지만, 6개월 뒤 질의를 읽을 때 `cord.chain.depth`가 LangChain 얘기인지 우리 연쇄인지 판단해야 한다.

**(b) 닫힌 목록의 값.** 질의가 이 문자열에 직접 걸린다.

```
Step Outcome     passed · repaired · failed · awaiting_approval · halted
Attempt Outcome  passed · failed · escalated
```

값 추가는 코드 변경이 아니라 어휘 변경이고 ADR이 필요하다.

### 4. 수식(`<source>.<word>`)은 필요할 때만

1. **글로서리의 충돌 항목이면** 문맥이 애매할 때 붙인다
2. **`cord.`는 기본 네임스페이스**라 우리 문서 안에서는 생략한다. 같은 문단에 남의 것이 나오면 붙인다
3. **충돌 없는 단어에는 붙이지 않는다.** `otel.Collector`는 과잉이다
4. **코드에서는 속성 접두사가 이미 그 일을 한다.** `cord.tier`에 또 붙이지 않는다

접두사는 신호이지 장식이다. 전부 붙이면 아무 의미도 없어진다.

### 5. 속성 네이밍 — 두 층

1. **`gen_ai.*`가 이미 이름을 가진 것은 그 이름을 쓴다.** 토큰·프로바이더·작업·모델
2. **없는 것은 `cord.*` 아래로**
3. **모든 Run 루트 Span에 `cord.semconv.version`을 기록한다.** `gen_ai.*`가 사전 통보 없이 바뀌어도 나중에 미스터리가 아니라 마이그레이션이 된다

`gen_ai.*`는 전부 Development 상태이고 사전 통보 없이 제거될 수 있다. **이름을 빌리는 것과 안정성을 가정하는 것은 다르다.**

### 6. 와이어 필드 이름은 손대지 않는다

`trace_id` · `traceparent` · `span_id` · `session_id` · `thread_id`는 그대로 쓴다. 이건 프로토콜이지 우리 도메인 명사가 아니다.

### 7. Node와 Step은 가른다

이건 유지한다. 뷰어가 **한 번도 실행되지 않은 그래프**도 그려야 하므로 "정의는 있고 실행은 없는" 상태가 정상 상태다. `wb.`는 그래프가 하나여서 이 모호성을 만난 적이 없다.

### 8. 레포는 도메인 명사가 아니다

허용해야 하는 조합은 이만큼이고, 어휘가 이걸 다 담아야 한다.

```
레포 1 : Graph 1          보통 · cord new 의 기본값
레포 1 : Graph N          모노레포
Deployment 1 : Graph 1    우리 기본 격리 모드 (ADR-0001)
Deployment 1 : Graph N    Aegra의 graphs 맵이 이미 이렇다
```

"한 레포 한 그래프"는 **권장 관례이지 계약이 아니다.** 그리고 이 다양성을 수용하기 때문에 오히려 계약이 필수가 된다 — 레포 구조로 정체성을 유추할 수 없으므로 Manifest가 유일한 정체성이다.

### 9. 범위 판정은 교환원 기준으로

이 플랫폼은 옛날 전화 교환원이다. **연결만 하고 통화 내용을 모른다.** 실패 모드 A를 부정형이 아니라 긍정형으로 말한 것이고, 범위 질문이 나올 때 판정 도구로 쓴다.

| 교환원이 하는 일인가 | |
|---|---|
| 누가 있는지 안다 — 카탈로그 | ✓ |
| 어떤 신호에 누굴 연결할지 안다 — Rule | ✓ |
| 통화 기록을 남긴다 — Span | ✓ |
| 통화 내용을 이해한다 | ✗ 실패 모드 A |
| 대신 말해준다 | ✗ 오케스트레이션 프레임워크 |

---

## Options Considered

### Option A: 금지어 목록 (첫 판)

**Pros:** 규칙이 명확하다. 위반이 눈에 보인다.
**Cons:** 자연스러운 표현을 막는다. 품사·문맥 판정이 필요해진다. 그리고 **이해를 개선하지 않는다** — 사람도 LLM도 "trace this run"을 문맥으로 이해한다. 순수한 마찰.

### Option B: 한 층의 어휘를 통째로 채택

**Pros:** 번역표가 없다.
**Cons:** 어느 층도 우리 문제를 다 덮지 못한다. `ap.`에는 Attempt도 Tier도 Verdict도 없고, `wb.`에는 Assistant도 Subject도 없다.

### Option C: 전부 새로 짓는다

**Pros:** 충돌이 원천 봉쇄.
**Cons:** 경계에서 매번 번역해야 한다. 외부 문서·SDK·커뮤니티가 전부 다른 말을 쓴다. 혼자 쓰는 도구에서 순손실.

### Option D: 이름은 빌리고 겹치는 곳은 매핑을 문서화 ← **채택**

**Pros:** 남들이 이미 아는 단어를 그대로 쓴다. 새 단어는 진짜 필요한 곳(Subject, Step)에만 생긴다. 글로서리가 경찰이 아니라 참조 문서가 된다. **6개월 뒤의 자신이 자연스럽게 말하고, 애매하면 찾아본다.**
**Cons:** 강제되지 않는다. 규율이 아니라 참조에 의존한다.

---

## Trade-off Analysis

### 금지가 아니라 매핑인 이유

금지어의 값은 "쓰면 안 되는 걸 안 쓰게 만든다"인데, 그 값이 성립하려면 **잘못 쓰는 것이 실제로 비용을 발생시켜야** 한다.

- **속성 키를 잘못 쓰면** → 데이터에 굳고 질의가 애매해진다. **비용이 있다**
- **대화에서 "trace"라고 하면** → 상대가 이해한다. **비용이 없다**

두 번째에 규칙을 씌우면 순비용이다. 그래서 첫 번째만 강제하고 두 번째는 정의만 둔다.

### Assistant를 참고 쓰는 이유

솔직히 나쁜 단어다. 이슈 해결 그래프는 assistant가 아니다. 대안(`Registration`, `Variant`)을 검토했다.

**경계에서 이름이 갈리는 비용이 어색한 단어의 비용보다 크다.** Aegra의 `/assistants`, LangGraph SDK, 관련 UI가 전부 이 단어를 쓴다. 우리만 다르면 모든 문서에 번역표가 붙고 디버깅할 때마다 머릿속 변환이 생긴다.

첫 판에서는 "API 경계에서만 쓴다"고 제한했는데, **그 제한도 완화한다.** 사람이 읽는 곳에서 "등록된 그래프"라고 쓰는 것을 권장하되 강제하지 않는다.

### Span을 새 이름으로 감싸지 않는 이유

`wb.`는 "공유 이벤트 스키마"라 불렀고 우리도 `Record` 같은 이름을 붙일 수 있었다.

안 붙인 이유는 **우리가 실제로 내보내는 게 OTel 스팬이기 때문**이다. 새 이름을 만들면 개념 층이 하나 늘고, 디버깅할 때 "우리 Record가 span으로 어떻게 매핑되지"를 매번 생각해야 한다. **같은 것에는 같은 이름을 쓴다.**

### Cascade Depth로 개명하는 이유

`cord.chain.depth`는 우리가 통제하는 속성 키이고, `chain`은 LangChain의 핵심 개념이자 Langfuse의 observation 타입이다. 데이터에 굳는 이름이므로 여기서는 정밀함이 값을 한다. **`cord.cascade.depth`** — `cascade`는 아홉 출처 어디에서도 안 쓴다.

### Signal은 개명하지 않는다

OTel이 traces·metrics·logs를 "signals"라 부르는 것을 첫 판에서 놓쳤다. 그럼에도 유지하는 이유:

- otel의 용법은 스펙·아키텍처 층에서 나오고 일상 코드에는 거의 안 나온다
- 우리 Signal은 도메인 명사이고 `cord.signal.id`로 이미 네임스페이스가 붙는다
- 개명 비용(ADR 셋, 설계 문서, 글로서리)이 충돌 빈도에 비해 크다

**대신 글로서리 충돌 목록에 올린다.** 텔레메트리 파이프라인을 논할 때는 수식한다.

---

## Consequences

### 쉬워지는 것

- **자연스럽게 말할 수 있다.** "trace this run", "이 노드에 tool 붙여" 가 그대로 통한다
- 새 의존성을 채택할 때 글로서리에 충돌 항목만 추가하면 된다
- Aegra·Langfuse 문서를 읽을 때 번역이 필요 없다
- 표준 OTel 도구가 우리 아카이브를 부분적으로 읽는다

### 어려워지는 것

- **강제되지 않는다.** 규율이 아니라 참조에 의존한다. 글로서리를 안 읽으면 값이 없다
- **글로서리가 살아 있어야 한다.** 의존성이 늘 때마다 충돌 항목을 점검해야 한다 — 안 하면 첫 판의 세 catch 같은 게 또 쌓인다
- **애매한 순간이 남는다.** "이건 우리 Rule인가 Langfuse Rule인가"를 그때그때 판단한다. 문맥이 대부분 해결하지만 전부는 아니다
- **속성 키와 대화 어휘가 갈릴 수 있다.** `cord.cascade.depth`인데 대화에서는 "체인 깊이"라 부르게 된다. 수용한다 — 데이터가 맞으면 된다
- **`gen_ai.*` 변경 시 마이그레이션.** 스탬프가 가능하게 할 뿐 없애지 않는다

### 다시 볼 조건

- **글로서리를 만들고도 같은 혼선이 반복되면** → 그 단어만 개명을 검토. 목록 전체를 되살리지는 않는다
- **`gen_ai.*`가 Stable로 승격되면** → 그 이름으로 정렬하고 `cord.semconv.version`을 올린다
- **새 의존성이 들어올 때** → 채택 전에 글로서리와 대조. 충돌이 많으면 그 자체가 도입 판단의 재료
- **속성 키 충돌이 실제 질의에서 문제를 내면** → 그 키만 개명 (Cascade Depth의 선례)

---

## Action Items

1. [ ] **cordboard 용어 사전를 금지 중심에서 정의 중심으로 다시 만든다.** 각 항목에 "cordboard 문서에서의 뜻"을 넣는다
2. [ ] `cord.chain.depth` → `cord.cascade.depth` 개명. 명사도 Cascade Depth
3. [ ] 설계 문서(HTML 아티팩트)의 금지어 섹션을 글로서리 링크로 교체
4. [ ] Outcome 두 목록을 `cord-runtime`에 enum으로 정의. 서로 섞이면 타입 오류 (ADR-0008 Action 1과 동일)
5. [x] #5의 `cord_runtime.execution.run`이 Run 루트에 `cord.semconv.version=0.1.0` 기록 (2026-09-11)
6. [ ] `OTEL_SEMCONV_STABILITY_OPT_IN` 설정을 고정하고 값을 문서화
7. [ ] 새 의존성 도입 시 글로서리 대조를 도입 필터의 항목으로 추가
