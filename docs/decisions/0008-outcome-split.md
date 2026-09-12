# ADR-0008: Outcome을 Step용과 Attempt용으로 분리하고, 승격은 선언한다

**Status:** Accepted
**Date:** 2026-09-08
**Deciders:** 단독 (로컬 도구, 저자 = 운영자)
**관련:** ADR-0004 (모델 게이트웨이), ADR-0005 (기록의 원본)
**슬라이스:** 0 — 작업 3(관문)이 정확히 이 결정을 시험한다

> **분류 정정.** 이 결정은 처음에 "어휘 결정이라 명사표가 곧 근거이므로 ADR 불필요"로 분류했다. 틀렸다. 이건 **아카이브의 물리적 구조를 정하는 결정**이고, 데이터가 쌓인 뒤에는 되돌리는 비용이 급격히 오른다.

---

## Context

`workbench`의 이벤트 스키마에는 결과값이 하나의 닫힌 목록이었다.

```
passed · repaired · failed · escalated · awaiting_approval · halted
```

그래프가 하나였을 때는 문제가 없었다. 우리는 셋을 동시에 만나면서 이 목록이 두 가지를 뭉개고 있다는 게 드러났다.

**하나 — 승격 후 통과한 Step은 무엇인가.**
`draft_commit`이 fast에서 두 번 실패하고 deep에서 통과했다. 이 Step의 결과는 `passed`인가 `escalated`인가? 둘 다 맞고 둘 다 부족하다. Step은 결국 통과했고, 승격은 그 과정에서 일어난 일이다.

**둘 — 사다리 없는 노드가 있다.**
ADR-0004에서 Tier를 선택적으로 만들었다. `embed`나 `vision` 노드는 사다리가 없고 승격이 원리적으로 불가능하다. 그런데 목록에 `escalated`가 있으면 "이 노드에서 왜 안 나오지"라는 질문이 생긴다.

**셋 — 성공 기준 C가 이 구조에 직접 걸린다.**

> 지난 8주간 모든 그래프에서 **Tier가 올라간 Node** 상위 10개

"올라갔다"를 무엇으로 판정하는가. 형제 Attempt들의 tier를 비교해서 추론할 것인가, 아니면 그래프가 직접 말할 것인가. 이 선택이 아카이브의 행 구조와 질의의 복잡도를 동시에 결정한다.

---

## Decision

### 1. 목록을 둘로 가른다

```
Step Outcome     passed · repaired · failed · awaiting_approval · halted
Attempt Outcome  passed · failed · escalated
```

- **Step Outcome** — 이 Node 실행의 최종 처분
- **Attempt Outcome** — 이 시도 하나의 처분

`escalated`는 종결 상태가 아니라 **"이 시도가 실패했고 다음 시도가 Tier를 올렸다"** 는 뜻이다. Step에는 나타나지 않는다.

### 2. Attempt는 자식 span이다 — span event가 아니다

```
span C  step:draft_commit          cord.outcome=passed
├ span D  attempt  attempt=1  tier=fast  outcome=failed
├ span E  attempt  attempt=2  tier=fast  outcome=escalated
└ span F  attempt  attempt=3  tier=deep  outcome=passed
  └ span F' chat (LiteLLM 프로세스)  gen_ai.request.model=claude-opus-5
```

| | span event | 자식 span ← 채택 |
|---|---|---|
| 지속시간 | 없음 | **있음** |
| 자식을 가질 수 있나 | 못 함 | **가능** (F' 프록시 span) |
| 집계 | 불편 | 그냥 행 |

Attempt는 시간이 걸리고 모델 호출을 자식으로 갖는다. 순간이 아니라 구간이므로 span이다.

### 3. 승격은 추론하지 않고 선언한다

그래프가 다음 Attempt를 더 높은 Tier로 보내기로 결정하는 순간, 직전 Attempt에 `outcome=escalated`를 직접 찍는다.

```sql
-- 성공 기준 C
SELECT graph_id, node, count() FROM attempts
WHERE outcome = 'escalated'
  AND end_ts > :reference - INTERVAL 8 WEEK AND end_ts <= :reference
GROUP BY graph_id, node ORDER BY 3 DESC, graph_id, node LIMIT 10
```

술어 하나, GROUP BY 하나.

### 4. `repaired`가 실제로 발생하는 경로를 유지한다

목록에 있는데 데이터에 한 번도 안 나타나는 값은 검증되지 않는다. 슬라이스 0의 규칙표에 **결정적으로 수리 가능한 규칙 하나**(R5: 마침표 제거)를 항상 켜 둔다. 수리 후 통과하면 Step은 `repaired`다. 원래 모델 출력의 검증이 실패한 Attempt는
`failed`로 남기고 위반 규칙 R5를 보존한다. 결정적 수리는 새 모델 Attempt를 만들지 않는다.

이로써 슬라이스 0이 Outcome 중 넷을 실제로 만들어 낸다 — `passed`, `repaired`, `failed`, 그리고 Attempt의 `escalated`.

### 5. 재시도와 승격을 구분할 수 있게 시도 정책을 짠다

```
attempt 1   tier=fast   outcome=failed       ← 다음도 fast: 같은 Tier 재시도
attempt 2   tier=fast   outcome=escalated    ← 다음은 deep: 승격
attempt 3   tier=deep   outcome=passed
```

> **2026-09-11 정정 (#4).** 앞선 예제의 첫 Attempt를 `escalated`에서 `failed`로
> 고쳤다. 같은 Tier 재시도는 승격이 아니므로 위 SQL이 이 시퀀스에서 세는 행은
> 정확히 1개다. 모두 실패하면 마지막 deep Attempt는 `failed`, Step도 `failed`다.
> [최소 실행 예제와 테스트](../minimal-graph.md)가 이 순서와 수리 경로를 검증한다.
> 여기의 실행 기록은 메모리 내 계약이며 span 전송·저장은 후속 작업이다.

2번을 같은 Tier로 두지 않으면 "재시도가 도왔나 승격이 도왔나"를 영원히 구분할 수 없다.

---

## Options Considered

### Option A: 단일 목록 유지 (workbench 그대로)

| 차원 | 평가 |
|---|---|
| 어휘 크기 | 작음 |
| 모호성 | **있음** — 승격 후 통과한 Step |
| 질의 복잡도 | 높음 |

**Pros:** `workbench`와 완전히 같다. 명사가 하나 적다.
**Cons:** Step과 Attempt가 같은 목록을 쓰면 어느 층의 값인지 매번 문맥으로 판단해야 한다. 그리고 사다리 없는 노드에서 `escalated`의 부재가 정보인지 결함인지 알 수 없다.

### Option B: 단일 목록 + 형제 tier 비교로 승격 추론

```sql
-- 창 함수로 이전 Attempt의 tier와 비교
LAG(tier) OVER (PARTITION BY step_id ORDER BY attempt)
```

| 차원 | 평가 |
|---|---|
| 그래프 부담 | **없음** — 아무것도 선언 안 함 |
| 질의 복잡도 | **높음** |
| 정확성 | **취약** |

**Pros:** 그래프가 승격을 의식하지 않아도 된다.
**Cons:** 세 가지가 깨진다.
- **한 노드가 여러 모델을 부르면** — `embed` 다음에 `deep`을 부른 것이 승격으로 오판된다. Tier와 능력은 다른 축이다(ADR-0004)
- **같은 Tier 안의 로드밸런싱** — `fast` 별칭 아래 haiku와 gpt-mini가 번갈아 나가면 모델 문자열은 바뀌지만 승격이 아니다
- **Tier 순서를 질의가 알아야 한다** — `fast < standard < deep`이라는 지식이 질의에 박히고, 그래프마다 사다리가 다르면 무너진다

### Option C: 목록 분리 + 승격 선언 ← **채택**

| 차원 | 평가 |
|---|---|
| 어휘 크기 | 목록 하나 추가 |
| 그래프 부담 | 헬퍼 호출 하나 |
| 질의 복잡도 | **최저** |
| 정확성 | 그래프가 아는 것을 그대로 기록 |

**Pros:** 질의가 술어 하나. 다중 모델과 로드밸런싱에 영향받지 않는다. 사다리 없는 노드에서 `escalated` 부재가 자연스럽다.
**Cons:** 그래프가 선언해야 한다 — `cord-runtime` 헬퍼로 완화하되, 안 찍으면 데이터가 빈다.

---

## Trade-off Analysis

### B를 버리는 결정적 이유

B는 **파생 데이터를 저장하지 않는다**는 점에서 원칙적으로 우아하다. 하지만 승격은 파생 가능한 사실이 아니다 — **그래프의 의도**다.

같은 두 Attempt(`fast` → `deep`)가 승격일 수도 있고 아닐 수도 있다. 앞의 것이 규칙표를 못 넘어서 올린 것이면 승격이고, 노드가 원래 두 모델을 순서대로 쓰는 것이면 아니다. **span만 보고는 구분할 수 없다.** 의도를 아는 건 그래프뿐이다.

ADR-0004에서 "승격은 그래프가 소유한다"고 정한 것의 데이터 층 대응이다. 소유자가 결정한다면 기록도 소유자가 한다.

### 선언이 빠지면 어떻게 되는가

`escalated`를 안 찍으면 성공 기준 C가 그 그래프에 대해 0을 답한다. **조용한 실패다.** 완화 둘:

- `cord-runtime`의 승격 헬퍼가 Attempt span 생성과 `escalated` 마킹을 **같이** 한다. 따로 호출할 일이 없다
- 뷰어의 누락 경고는 명시적 승격 결정과 Attempt 기록의 불일치를 대상으로 한다.
  **2026-09-11 정정:** "Attempt 2개 이상 + 승격 0개"만으로 경고하면 정상적인
  같은 Tier 재시도를 오탐한다. Tier나 모델 문자열 비교로 승격 의도를 추론하지 않는다

두 번째는 ADR-0006의 관문과 같은 발상이다 — 관측층이 자기 결함을 스스로 찾는다.

### 왜 이게 슬라이스 0의 관문인가

아카이브가 append-only(ADR-0005)라 **구조를 바꾸면 이전 데이터가 새 질의로 안 읽힌다.** 데이터가 다섯 줄일 때 확정해야 하고, 그래서 슬라이스 0 작업 3이 "Attempt 계층을 한 번 일부러 잘못 만들어 보고 질의가 깨지는 걸 확인한다"까지 포함한다.

---

## Consequences

### 쉬워지는 것

- 성공 기준 C가 술어 하나 + GROUP BY 하나
- Tier 순서에 대한 지식이 질의에 안 들어간다 — 그래프마다 사다리가 달라도 된다
- 다중 모델 노드가 질의를 흔들지 않는다
- Attempt마다 지속시간·토큰·비용이 개별 행으로 남는다
- Attempt 아래에 프록시 span이 자연스럽게 붙는다

### 어려워지는 것

- **명사가 하나 늘었다.** Outcome이 문맥에 따라 두 목록을 가리킨다 → 어휘표에서 층을 명시
- **그래프가 선언해야 한다.** 헬퍼로 완화하되 부담은 남는다
- **span 개수가 늘어난다.** Attempt가 2~3개면 Step당 span이 3~4개. 로컬 규모에서 무시 가능
- **두 목록이 나중에 어긋날 수 있다.** 한쪽에만 값을 추가하는 실수 → 둘 다 닫힌 목록이고 값 추가는 어휘 변경이라는 규칙 유지

### 다시 볼 조건

- **`repaired`가 슬라이스 0 이후로 한 번도 안 나오면** → 수리 경로가 실제로 쓸모 있는지, 아니면 목록에서 뺄지 재검토
- **Attempt 아래에 또 층이 필요해지면** (예: 도구 호출 단위) → 그때 세 번째 목록이 필요한지, 아니면 span 트리 깊이만 늘리면 되는지 판단
- **`escalated` 누락 경고가 자주 뜨면** → 헬퍼 API가 잘못 설계된 것. 선언을 잊을 수 없는 형태로 바꾼다

---

## Action Items

1. [x] 두 Outcome 목록을 `cord-runtime`에 enum으로 정의하고 헬퍼에서 혼용 거부 (#5)
2. [ ] 승격 헬퍼 — Attempt span 종료 + `escalated` 마킹 + 다음 Attempt 시작을 한 호출로
3. [x] #6: Attempt의 부모를 형제 Attempt로 바꾼 픽스처가 오류로 거부됨
4. [x] 최소 예제 규칙표에 R5(마침표 제거)를 항상 켜 둔다 — `repaired` 경로 검증 (#4)
5. [x] 최소 예제의 시도 정책을 `fast → fast → deep`으로. 승격 1회 및 같은 Tier 재시도 검증 (#4)
6. [ ] 뷰어에 명시적 승격 결정과 Attempt 기록의 불일치 경고 (슬라이스 1). 같은 Tier 재시도는 경고하지 않는다
7. [ ] 설계 문서 어휘표에 Outcome이 두 층임을 명시

최소 예제는 두 enum과 런타임 혼용 거부를 구현했다 (#4). 별도 `cord-runtime`의
span 헬퍼와 enum은 #5에서 구현했다. 두 실행 경로는 #8에서 연결했다. Action 2의 원자적
승격 헬퍼는 아직 후속 작업이다.


## 정정 — Graph별 집계와 계층 검증 (2026-09-11, #6)

위 SQL은 설명용이며 실제 명령은 `archive-escalations`다. Node만 묶던 예제를
Graph ID와 Node의 복합 정체성으로 정정했다. 부모 Step 및 Run 루트, trace와
Graph/Run/Subject/Node 일치를 검증한 뒤 명시적 Attempt 승격만 센다.
[고정 시각 경계 및 실제 아카이브 검증](../archive-query.md)이 이 계약을 확인한다.


## 구현 확인 — 실제 모델 경계 (2026-09-11, #8)

최소 그래프는 모델 호출부터 검증까지 하나의 열린 Attempt span으로 감싼다.
`fast/failed → fast/escalated → deep/passed`가 실제 LiteLLM과 Collector를 거쳐
저장되며 질의 결과는 승격 1회다. 모델 문자열 교체는 Outcome 판단에 쓰지 않는다.
Run별 config로 계측 객체를 전달해 병렬 실행의 부모와 상태를 분리한다.
기본 고정 응답 실행은 그대로 서비스 없이 동작한다.
[검증 명령](../model-proxy.md).


## 정정 — 교환원 경계 (2026-09-11, #7)

[ADR-0013](0013-switchboard-boundary.md)이 모델 소유권의 현재 결정이다.
Attempt는 모델 호출 여부와 관계없는 실행 시도다. `cord.tier`는 선택적이며
모델 없는 Attempt와 Tier를 공개하지 않은 승격 선언도 유효하다. 집계는 여전히
명시된 Outcome만 세며, 모델 수·이름·Tier 사다리를 해석하지 않는다.
프록시 자식 span은 예제의 계측 선택이며 모든 Attempt의 필수 자식이 아니다.
