# ADR-0003: Run을 묶는 것은 Subject이고, Thread는 플랫폼이 해석하지 않는다

**Status:** Accepted
**Date:** 2026-09-08
**Deciders:** 단독 (로컬 도구, 저자 = 운영자)
**관련:** ADR-0002 (어휘), ADR-0005 (기록의 원본)
**슬라이스:** 3 — 멱등성 관문이 이 결정 위에 선다

---

## Context

### 요구사항

같은 대상에 대한 재실행을 묶고 싶다. 이슈 #482를 세 번 돌렸으면 그 셋이 한 덩어리로 보여야 하고, 그래야 이런 질문에 답할 수 있다.

> 이 이슈, 몇 번 만에 통과했나?
> 우리 그래프는 보통 몇 번 만에 통과하나?

### 처음 떠올린 답과, 그것이 깨지는 이유

Agent Protocol에는 `thread`가 있고 Run이 Thread에 속한다. Langfuse에는 `session`이 있고 trace를 묶는다. 둘 다 "묶음"처럼 생겼다.

**그런데 `thread_id`는 라벨이 아니라 체크포인터의 기본 키다.** LangGraph가 슈퍼스텝마다 상태 스냅샷을 저장하는데 그 저장·조회의 키가 `thread_id`이고, 그게 없으면 인터럽트 후 재개가 불가능하다.

Thread를 공유한다는 것은 **상태를 공유한다는 뜻**이다. 그리고 상태는 누적된다. LangChain 문서는 한 thread에서 여러 agent를 돌리면 두 번째가 첫 번째의 체크포인트를 컨텍스트로 삼는다고 명시한다.

`add_messages`나 `operator.add` 같은 리듀서가 붙으면 더 심하다.

```python
class State(TypedDict):
    messages: Annotated[list, add_messages]   # 누적
    attempts: Annotated[int, operator.add]    # 누적
    diff:     str
```

| Run | 시작 상태 |
|---|---|
| 1 | `messages=[]`, `attempts=0`, `diff=""` |
| 2 | Run 1의 메시지 12개, `attempts=3`, **실패한 diff** |
| 3 | Run 1·2의 메시지 27개, `attempts=7`, 실패한 diff |

Run 3은 깨끗한 시도가 아니다. 앞선 실패한 커밋 초안을 컨텍스트에 안고 시작하고, 승격 로직이 참조할 `attempts`가 이미 7이라 오작동한다. **재실행의 목적이 "깨끗하게 다시"인데 정확히 그게 안 된다.**

실무 가이드도 관련 없는 작업 간에 `thread_id`를 공유하지 말라고 못 박는다. 재실행은 "관련 없는" 건 아니지만 **"독립적이어야 하는"** 것이고, Thread는 그 구분을 못 한다.

### 문제의 핵심

```
Thread = 묶음  +  공유되는 가변 상태
                  ↑ 이건 원하지 않았다
```

Thread는 둘을 분리해서 주지 않는다. 묶음을 얻으려면 상태 공유가 딸려 온다.

---

## Decision

### 1. Subject를 새 명사로 도입한다

> **Subject** — Run이 다루는 외부 대상. URI 문자열.

```
github:issue/482
file:~/notes/api.md
run:0193f7a3...
manual:2026-09-08T14:22
```

타입은 문자열이고 **플랫폼은 해석하지 않는다.**

### 2. Subject는 필수 필드다

값이 없는 Run이 하나라도 생기면 그 Run에 대해서는 묶음 질문이 답해지지 않는다. 트리거가 만든 Run은 Signal에서 뽑고, 수동 실행은 합성 값을 쓴다.

### 3. 1 Run = 1 Thread

```
Subject  1 ──── N  Run  1 ──── 1  Thread
"github:issue/482"      Run 1 → Thread A   failed   (격리)
                        Run 2 → Thread B   halted   (격리)
                        Run 3 → Thread C   passed   (깨끗한 시작)
```

- 묶음은 **Subject**가 한다
- 상태 격리는 **Thread**가 한다
- 두 관심사가 분리된다

### 4. Thread는 플랫폼의 도메인 명사가 아니다

체크포인트 범위는 Graph 내부 사정이고 Aegra API를 부를 때만 등장한다. `thread`는 금지어 목록에 들어간다 (ADR-0002).

비대화형 그래프의 기본 지침은 **Run마다 새 Thread**. 나중에 진짜 대화형 그래프가 Thread 하나에 Run을 여럿 담고 싶으면 그건 그 그래프의 자유이고, 우리 층은 여전히 Subject로만 묶는다.

### 5. 재개와 재실행은 다르다

| | Thread | Run | 상태 |
|---|---|---|---|
| **재개** (승인 후) | 같은 Thread | **같은 Run** | 이어받아야 맞다 |
| **재실행** | 새 Thread | 새 Run | 깨끗해야 맞다 |

인터럽트 후 재개는 멈췄던 Run이 이어지는 것이므로 상태를 물려받는 게 **정상**이다. Thread를 Run과 1:1로 두면 둘 다 자동으로 맞는다.

### 6. Langfuse `session_id`로 매핑한다

Langfuse의 session이 "관련된 trace들을 묶는 것"이고 Run = trace이므로 1:1로 맞는다. 우리가 만들 게 없다.

### 7. 멱등성 키로 재사용한다

`(Assistant, Subject)`가 동시성 판정 키가 된다 — "이미 돌고 있는 Run이 있으면 새로 시작하지 않는다". 웹훅 중복 발송이 흔하므로 실제로 필요하다.

---

## Options Considered

### Option A: Thread를 "같은 대상에 대한 재실행 묶음"으로 재정의

| 차원 | 평가 |
|---|---|
| 새 명사 | 없음 |
| 묶음 질문 | 답해짐 |
| 상태 격리 | **파괴됨** |

**Pros:** 프로토콜에 이미 있는 개념을 쓴다. 추가 필드가 없다.
**Cons:** Context에 적은 그대로 — 재실행이 앞선 실패를 물려받는다. 리듀서가 붙은 필드는 누적되고, 카운터 기반 로직이 오작동한다.

### Option B: Assistant마다 Thread 하나, 모든 Run을 거기에

| 차원 | 평가 |
|---|---|
| 새 명사 | 없음 |
| 묶음 질문 | **답 안 됨** — 대상 구분이 없다 |
| 상태 격리 | 최악 |

**Pros:** 구현이 가장 단순하다.
**Cons:** A의 문제를 더 크게 만든다. 그리고 원래 요구("같은 **대상**에 대한 재실행")를 아예 만족하지 못한다.

### Option C: Thread를 재사용하되 재실행 전에 상태를 비운다

| 차원 | 평가 |
|---|---|
| 새 명사 | 없음 |
| 묶음 질문 | 답해짐 |
| 상태 격리 | 조건부 |

**Pros:** 두 가지를 다 얻는 것처럼 보인다.
**Cons:** 셋이 걸린다.
- 리듀서가 붙은 필드를 부분 초기화하는 건 실수하기 쉽고, 그래프마다 상태 모양이 다르므로 **플랫폼이 대신 해 줄 수 없다**
- **비우는 순간 Run 1·2의 이력이 사라진다.** 묶어서 보려던 그 데이터가
- 체크포인터의 설계를 거스르면서 얻는 게 라벨 하나다

### Option D: 별도 라벨(Subject)을 우리 층에 만든다 ← **채택**

| 차원 | 평가 |
|---|---|
| 새 명사 | 하나 |
| 묶음 질문 | 답해짐 |
| 상태 격리 | **완전** |

**Pros:** 두 관심사가 분리된다. Langfuse session에 그대로 매핑된다. 멱등성 키가 공짜로 따라온다. 플랫폼이 그래프 내부(상태 모양)를 알 필요가 없다.
**Cons:** 필수 필드가 하나 늘고, 모든 Run이 값을 채워야 한다.

---

## Trade-off Analysis

### 원하던 것은 라벨이었는데 Thread는 저장소였다

A·B·C가 전부 같은 실수를 한다 — **묶음이라는 요구를, 묶음처럼 생겼지만 실은 저장소인 물건으로 해결하려는 것.**

Thread가 두 가지를 뭉치고 있다는 걸 인정하면 답이 자명해진다. 뭉쳐 있는 걸 갈라서 각자에게 맡기면 된다. 라벨이 필요하면 라벨을 만든다.

### C를 특히 조심해야 하는 이유

C는 가장 그럴듯하고 가장 위험하다. **작동하는 것처럼 보이다가 특정 상태 모양에서만 깨진다.** 그리고 깨질 때 조용하다 — 재실행이 통과했는데 왜 통과했는지가 앞선 Run의 잔여 상태 때문일 수 있다.

그리고 플랫폼이 상태를 비우려면 상태 모양을 알아야 하고, 그건 실패 모드 A(플랫폼이 그래프 내부를 안다)로 가는 직행 경로다.

### 필수 필드 하나의 비용

Subject를 선택 필드로 두면 값이 없는 Run이 생기고, 그러면 묶음 질문이 "일부 Run에 대해서만" 답해진다. 그런 데이터는 신뢰할 수 없어서 결국 안 쓰게 된다.

**부분적으로만 채워진 필드는 없는 필드보다 나쁘다** — 있다고 착각하게 만들기 때문이다. 그래서 필수로 하고, 합성 값(`manual:<타임스탬프>`)을 허용한다.

---

## Consequences

### 쉬워지는 것

- "이 대상, 몇 번 만에 통과했나"가 `GROUP BY cord.subject.id`로 답해진다
- 재실행이 항상 깨끗하다 — 상태 오염 경로가 하나 사라진다
- 멱등성 판정이 트리거 층에서 가능해진다 (슬라이스 3)
- Langfuse session에 매핑이 공짜
- 플랫폼이 그래프의 상태 모양을 몰라도 된다
- 체인된 Run이 Subject를 상속하면 계보 질의가 자연스러워진다

### 어려워지는 것

- **필수 필드가 하나 늘었다.** 모든 Rule이 Subject 추출식을 가져야 하고, 그 식이 틀리면 등록 시 경고 대상이다
- **합성 Subject는 묶이지 않는다.** `manual:<타임스탬프>`는 매번 다르므로 수동 실행끼리는 안 묶인다. 의도된 동작이지만 처음엔 혼란스러울 수 있다
- **Subject 형식이 강제되지 않는다.** URI 문자열이라고만 정했고 플랫폼이 검증하지 않는다. 그래프마다 다른 형식을 쓰면 가로지르는 질의가 안 된다 → 관례를 문서화하되 강제는 안 한다
- **Thread를 완전히 못 잊는다.** Aegra API를 부를 때는 여전히 등장하고, 디버깅할 때 두 개념을 다 알아야 한다

### 다시 볼 조건

- **진짜 대화형 그래프가 생기면** → 그 그래프는 Thread 하나에 Run을 여럿 담을 수 있다. 우리 층은 안 바뀌지만 뷰어가 그 경우를 표현해야 할지 판단
- **Subject 형식이 그래프마다 갈라지면** → 타입 접두사 규약을 강제할지 검토
- **합성 Subject가 실제로 불편하면** → 수동 실행에 세션 개념을 도입할지 (예: `manual:<사용자가 준 이름>`)
- **`(Assistant, Subject)` 멱등성 키가 부족하면** → 입력 해시를 키에 추가할지 검토

---

## Action Items

1. [x] ~~`cord.yaml`에 `subject: {type, from}` 스키마 정의~~ → ADR-0014로 폐기. 추출식은 Rule에 속한다 (슬라이스 3)
2. [x] ~~카탈로그 등록 검증에 Subject 추출식 오류 검사 추가~~ → ADR-0014로 폐기. 검사는 Rule 검증에서 한다
3. [x] `cord-runtime`이 Run 루트 Span에 `cord.subject.id`와 `cord.subject.type` 기록 (#5, #9 통합 검증)
4. [x] #10 Collector Langfuse 분기에서 `cord.subject.id` → `langfuse.session.id`, 공개 trace `sessionId` 검증
5. [ ] 수동 실행의 합성 Subject 생성 규칙 확정
6. [ ] Subject 타입 접두사 관례 문서화 (`github:` `file:` `run:` `manual:`) — 강제는 안 함
7. [ ] 슬라이스 3: `(Assistant, Subject)` 멱등성 관문 구현
8. [ ] 체인된 Run의 Subject 상속 규칙 — 기본 상속, Rule에서 덮어쓰기 가능
9. [ ] 승인/재개 통합에서 논리 Run과 복수 API Run의 매핑 및 trace 연속성 결정

## 정정 — Aegra의 API Run과 논리 Run (2026-09-11, #9)

Aegra API `0.10.4`의 공개 실행 계약을 확인했다. `POST /threads/{thread_id}/runs`는
매 제출마다 새 `run_id`를 발급한다. `command.resume`을 제출해도 기존 API Run을
다시 여는 것이 아니라 **같은 Thread에 새 API Run을 만든다**. 서버는
`configurable.run_id`와 `configurable.thread_id`를 자신이 정한 값으로 덮어쓴다.
따라서 Decision 3·5의 1:1을 Aegra API의 보장으로 읽던 해석을 철회한다.

- **독립 실행:** 새 Thread를 생성하고 새 API Run을 제출한다. #9에서는 이 범위만
  구현하며 `cord.run.id`에 API가 반환한 ID를 기록한다. 하나의 실행은 하나의
  trace이고 Subject는 여러 실행을 묶는 필수 불투명 문자열이다.
- **재개:** 같은 Thread를 사용하되 API Run은 새로 생긴다. 같은 **논리 Run**으로
  묶으려면 API 호출 ID와 논리 정체성 및 trace 연속성을 별도로 관리해야 한다.
  이는 승인/재개 통합에서 결정·검증할 후속 작업이며 #9 클라이언트는 재개를
  노출하지 않는다. 기존 span을 다시 부모로 붙이거나 이미 끝난 trace를 새로
  만든 것처럼 기록하지 않는다.

Subject는 형식 검사의 대상이 아니다. 이전 span 헬퍼의 콜론 필수 검사를 없애고
그래프와 동일하게 비어 있지 않은 문자열만 요구한다. `opaque subject label`을
그대로 기록하는 실제 Aegra/Collector 시험으로 확인했다. Subject가 합성 secret을
포함하면 저장 전 redaction은 그대로 적용된다.

실제 Postgres에서 같은 Subject의 5개 Run/Thread를 만들고 실패한 첫 실행의
체크포인트가 후속 실행에 섞이지 않으며 그대로 남는지 확인했다. API의 완료
`success`와 그래프의 `StepOutcome.failed`도 다르다: 결정적 검증 실패는 정상적으로
완료한 실행의 결과일 수 있다. [재현·증거·범위](../aegra.md)를 기준으로 삼는다.


## 구현 확인 — Langfuse Subject 매핑 (2026-09-11, #10)

Collector의 Langfuse 전용 분기에서 이미 처리된 Subject를 `langfuse.session.id`로
복사한다. self-hosted 3.225.7의 공개 trace 응답 `sessionId`와 아카이브의 Subject가
같은지 확인했다. Thread를 묶음 키로 쓰지 않으며 기존 아카이브는 바꾸지 않는다.
[공개 계약과 실제 검증](../langfuse.md)을 따른다.


## 정정 — Subject 추출의 위치 (2026-09-12)

Subject 추출식을 그래프마다 붙는 설정에 두던 계획(Action 1·2)을 폐기한다
([ADR-0014](0014-no-graph-descriptors.md)). 한 그래프를 여러 Signal이 시작할 수 있고, Signal마다
입력의 모양과 묶는 기준이 다르다. 추출식은 그래프가 아니라 **연결**, 즉 어떤 Signal이 어떤 Graph를
시작하는지 정하는 Rule에 속하며 슬라이스 3에서 Rule과 함께 정한다. 지금처럼 호출자가 실행을
시작할 때는 호출자가 Subject를 직접 넘긴다 (`execute(endpoint, assistant, subject, graph_input)`).
Subject가 필수 불투명 문자열이라는 결정은 그대로다.
