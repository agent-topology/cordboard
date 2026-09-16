# ADR-0009: 자기소개는 두 문서로 나누고, 목록은 항상 배열이다

**Status:** Accepted
**Date:** 2026-09-10
**Deciders:** 단독 (로컬 도구, 저자 = 운영자)
**관련:** ADR-0001 (격리 수준), ADR-0002 (어휘), ADR-0011 (Manifest 파생), ADR-0012 (포맷 분리)

> **정정 기록.** 첫 판은 `agent-topology.manifest.json` 포맷을 **cordboard가 정의한다**는 전제로 썼다. ADR-0012에서
> 그 포맷을 `agent-topology`라는 독립 프로젝트로 분리하기로 했으므로, **cordboard는 정의자가 아니라
> 소비자이자 첫 생산자**다. 문서 이름과 구조 절이 그에 맞게 바뀐다.

---

## Context

플랫폼이 그래프 소스를 import하지 않는다면, 그래프가 **스스로 말해야** 한다. 무엇을 말하고 어디에 두느냐가 이 결정이다.

### 이미 있는 표준 — A2A Agent Card

Google이 2025년 4월에 공개하고 그해에 Linux Foundation에 기증했다. 1년 만에 150개 이상 조직이 참여했고 스펙은 1.0에 도달했다. 핵심은 모든 에이전트가 `/.well-known/agent-card.json`에 자기 설명을 올린다는 것이고, RFC 8615 well-known URI 규약을 따른다. LangGraph·CrewAI·LlamaIndex·Semantic Kernel·AutoGen이 네이티브 지원을 넣었다.

"자기 설명을 표준 위치에 올린다" — 우리가 필요한 것과 같아 보인다.

### 그런데 정반대의 계약이다

Agent Card는 **불투명(opacity) 계약**이다. 스펙의 설계 원칙이 "동료는 에이전트의 계약을 볼 뿐 내부 툴셋은 보지 않는다"이다. 카드에 담기는 것은 이름·설명·엔드포인트·버전·capabilities·skills·인증 방식 — **바깥에서 부르는 데 필요한 것만**이다.

우리는 정확히 반대가 필요하다. **Node 목록을 내놔야 뷰어가 그래프 모양을 그린다.** 성공 기준 B("뷰어 소스에 특정 그래프의 Node 이름이 없다")가 성립하려면 그 이름이 런타임에 데이터로 와야 하는데, Agent Card는 바로 그걸 감추는 게 목적이다.

### 그리고 오리진 스코핑 문제

`/.well-known/*`는 오리진(스킴+호스트+포트)당 하나다. Agent Card는 "에이전트 하나 = 서버 하나"를 전제한다.

우리는 그렇지 않다. ADR-0002에서 확정했듯 **Deployment 하나가 Graph를 여럿 호스팅할 수 있다** — Aegra의 `graphs` 맵이 이미 그 모양이고, 남이 만든 컨테이너를 등록할 때 우리가 그 안에 몇 개가 있는지 통제할 수 없다.

### 그리고 이름을 함부로 지으면 안 된다

RFC 8615는 새 well-known URI를 만들려면 **반드시 등록해야 한다**고 규정한다. 등록 정책은 "Specification Required"라 형식과 미디어 타입을 정의하는 안정적 문서가 필요하고 지정 전문가의 검토를 거친다. Standards Track이 아닌 것은 `provisional`로 등록된다.

그리고 이 조항이 결정적이다 — **특정 애플리케이션용 이름은 그에 걸맞게 구체적이어야 하며 일반 용어를 "선점(squatting)"하는 것은 권장되지 않는다.** RFC가 직접 든 예시가 `metadata`가 아니라 `example-metadata`다.

---

## Decision

### 1. 두 문서를 낸다. 역할이 다르다

```
/.well-known/agent-card.json              바깥에서 이걸 어떻게 부르나  — A2A 스펙 그대로
/.well-known/agent-topology.manifest.json 안이 어떻게 생겼나        — 독립 스펙 + 우리 확장
```

**둘은 경쟁하지 않는다.** 같은 서버가 둘 다 낸다.

### 2. Agent Card는 스펙 그대로 낸다. 확장하지 않는다

우리 필드를 하나도 넣지 않는다. `cord.*`를 카드에 밀어넣는 순간 다른 A2A 파서가 깨지거나 무시하고, 그러면 표준을 따르는 값이 사라진다.

공개된 카드 다수가 스펙을 안 지키는 상황이라 **정확히 내는 것 자체가 기여**가 된다.

### 3. 목록은 항상 배열이다

하나뿐이어도 배열이다. 이건 `agent-topology` 스펙이 정한 것이고 우리는 따른다.

```jsonc
{
  "topologyVersion": "0.1",
  "graphs": [
    {
      "name": "issue-resolver",
      "version": "0.3.0",

      // ── 스펙 소관. agent-topology 라이브러리가 파생한다 ──
      "structure":    { "nodes": [], "edges": [], "branches": [], "cycles": [] },
      "completeness": { "complete": false, "gaps": [] },
      "provenance":   { "structureHash": "sha256:…", "derivedAt": "…" },

      // ── 우리 확장. cord.yaml 에서 온다 ──
      "x-cord": { "subject": {}, "approval": {}, "triggers": [] }
    }
  ]
}
```

**`structure` 절에 cordboard의 개념이 하나도 없어야 한다.** Cordboard 승인 정책이 거기 들어가면 그건 표준이 아니라
cordboard 포맷이다. 우리 것은 전부 `x-cord` 아래로 간다.

> **2026-09-12 정정.** `x-cord` 절은 두지 않는다. cordboard는 이 문서에 아무것도 더하지 않는다
> ([ADR-0014](0014-no-graph-descriptors.md)). 위 예제의 `x-cord` 줄은 역사적 기록이다.

모드에 따라 모양이 바뀌면 소비자가 두 경우를 다 처리해야 한다. **한 그래프짜리 Deployment도 배열로 답한다.**

### 4. 이름은 `agent-topology`다. `agent-graph`가 아니다

`agent-graph`는 RFC가 말리는 일반 용어다. LangGraph도 CrewAI도 Temporal도 "agent graph"를 가진다. 스키마 하나가 그 이름 전체를 가져가는 것은 생태계 기여의 반대다 — 남들이 쓸 자리를 먼저 밟는 것이다.

`topology`를 고른 이유는 방향 중립성이다. cascade나 flow는 한 방향으로 흘러내리는 것을 뜻하는데, 담으려는 대상에는 **루프와 조건부 분기와 병렬 팬아웃**이 있다. 재시도 루프가 있는 그래프는 cascade가 아니다.

**이름은 얻는 것이지 잡는 것이 아니다.** 쓸모가 증명되면 스펙을 쓰고 provisional로 등록하고, 커뮤니티가 모이면 그때 승격을 제안한다.

### 5. 필드 이름은 Agent Card와 맞춰 둔다

`name` · `version` · `description`을 A2A의 대응 필드와 같은 이름으로 쓴다. 나중에 A2A로 외부 노출하고 싶어지면 `agent-topology.manifest.json`에서 Agent Card를 **파생**시키면 된다 — `derived` 절만 빼고. 지금 아무 비용도 안 들고 나중에 길이 안 막힌다.

### 6. 두 문서 다 생성물이다

`structure` 절은 `agent-topology` 라이브러리가, `x-cord` 절은 `cord.yaml`이, Agent Card는 둘의 교집합이 만든다. `cord up`이 합쳐서 Aegra 설정의 `http.app` 커스텀 라우트로 마운트한다. **그래프 저자가 할 일은 0이다.**

### 7. 우리가 아닌 Deployment도 등록할 수 있다

남이 만든 컨테이너가 `agent-topology.manifest.json`을 내면 등록된다. 안 내면 등록되지 않는다 — Manifest가 정체성이므로(ADR-0002) 없으면 등록할 게 없다.

> **2026-09-12 정정.** 매니페스트를 내지 않는 Deployment도 연결된다
> ([ADR-0015](0015-never-block-connection.md)). 매니페스트는 정체성이 아니라 뷰어의 선택적 입력이다.
> 내지 않으면 그림 없이 연결·기록된다.

---

## Options Considered

### Option A: Agent Card 하나만 쓴다

**Pros:** 표준 하나. 문서 하나. 이미 여러 프레임워크가 지원.
**Cons:** 불투명 계약이라 Node 목록이 없다. **뷰어를 만들 수 없다.** 성공 기준 B가 원리적으로 달성 불가.

### Option B: Agent Card를 확장한다

`x-cord-nodes` 같은 필드를 카드에 추가.

**Pros:** 문서 하나로 끝난다.
**Cons:** A2A 파서가 깨지거나 무시한다. 그리고 카드의 설계 원칙(불투명)을 정면으로 위반하므로, 스펙이 확장 규약을 정비하면 우리 필드가 불법이 된다. **공개된 카드 다수가 스펙을 안 지키는 원인의 상당 부분이 이 "내 필드 하나만 더"다.**

### Option C: 우리 문서만 쓴다. Agent Card는 무시

**Pros:** 우리 필요에 정확히 맞는다. 제약이 없다.
**Cons:** A2A 생태계와 단절된다. 나중에 외부 노출이 필요하면 처음부터 다시. 그리고 표준을 낼 수 있는데 안 내는 건 순손실이다 — 비용이 파일 하나다.

### Option D: 두 문서, 배열, 이름은 네임스페이스 ← **채택**

**Pros:** 불투명과 투명이 각자 자기 문서를 갖는다. 카드가 오염되지 않는다. 오리진에 그래프가 몇 개든 한 모양. 이름을 선점하지 않는다.
**Cons:** 문서가 둘이라 생성 로직이 둘. 그리고 `agent-topology`는 아무도 모르는 이름이라 발견성이 없다.

---

## Trade-off Analysis

### 왜 하나로 합칠 수 없는가

제가 앞선 검토에서 "Agent Card는 불투명 계약이라 안 맞는다, 따라서 채택하지 않는다"고 결론 냈다가 정정했다. **어느 한쪽을 골라야 한다는 전제가 틀렸다.**

둘은 다른 질문에 답한다.

| | 질문 | 독자 |
|---|---|---|
| `agent-card.json` | 바깥에서 이걸 어떻게 부르나 | 다른 에이전트, A2A 클라이언트 |
| `agent-topology.manifest.json` | 안이 어떻게 생겼나 | 우리 카탈로그와 뷰어 |

같은 서버가 둘 다 내면 된다. 합치려는 시도가 Option B이고, 그게 표준을 깨는 유일한 선택지다.

### 배열이 단일 객체보다 나은 이유

한 그래프짜리 Deployment에서 배열은 군더더기처럼 보인다. 그런데 **소비자 코드가 분기하지 않는 값이 더 크다.**

```python
# 배열이면
for g in doc["graphs"]: register(g)

# 모양이 바뀌면
if "graphs" in doc: ... else: ...   # 매번, 모든 소비자에서
```

그리고 우리 격리 모드(ADR-0001)가 그래프별 설정이라 같은 워크스페이스 안에서 두 경우가 공존한다. 분기가 실제로 필요해진다.

### 이름을 선점하지 않는 것의 값

당장은 아무 값도 없다. `agent-topology.manifest.json`은 우리만 읽는다.

값은 나중에 생긴다 — 이 형식이 쓸모 있는 것으로 판명되면 스펙을 쓰고 등록하고, 그때 `agent-graph`를 제안할 자격이 생긴다. 반대로 지금 `agent-graph`를 쓰면 등록도 안 한 채 이름만 점유한 상태가 되고, 나중에 진짜 표준이 그 이름을 쓰려 할 때 충돌한다.

**규정을 따르자는 원칙을 끝까지 적용하면 파일 이름이 달라진다.** RFC가 말리는 게 정확히 우리가 하려던 일이었다.

---

## Consequences

### 쉬워지는 것

- 뷰어가 Node·Edge를 데이터로 받는다 — 성공 기준 B가 가능해진다
- A2A 클라이언트가 우리 그래프를 표준 방식으로 발견할 수 있다
- 남이 만든 Deployment도 문서만 내면 등록된다
- 오리진에 그래프가 몇 개든 소비자 코드가 하나
- 나중에 A2A 노출을 늘려도 필드 이름이 이미 맞다

### 어려워지는 것

- **문서가 둘이라 생성 로직이 둘.** 그리고 둘이 어긋날 수 있다 — 같은 소스(`cord.yaml` + 파생)에서 만들어 완화
- **`agent-topology`는 아직 아무도 모른다.** 발견성이 없다. 우리만 쓰는 동안은 문제가 아니지만 공개하려면 스펙이 필요하다
- **A2A 스펙 변경을 따라가야 한다.** 경로가 `/.well-known/agent.json`에서 `agent-card.json`으로 바뀐 전례가 있고, 오래된 샘플을 복사한 구현들이 조용히 발견에 실패하고 있다
- **배열이 한 그래프일 때 군더더기로 보인다.** 리뷰에서 "이거 왜 배열이지"가 나온다 → 이 문서가 답이다

### 다시 볼 조건

- **A2A가 확장 규약을 정비하면** → 두 문서를 하나로 합칠 수 있는지 재검토. 다만 불투명 원칙이 유지되는 한 합쳐지지 않는다
- **`agent-topology.manifest.json`이 다른 프로젝트에도 쓸모 있어지면** → 스펙을 쓰고 IANA provisional 등록, 그다음 일반 이름 승격 제안
- **A2A 경로가 또 바뀌면** → 카탈로그가 구·신 경로를 다 시도하도록
- **등록할 Deployment가 우리 것뿐이라면** → Agent Card를 계속 낼 값이 있는지 재평가. 비용이 파일 하나라 유지 쪽으로 기운다

---

## Action Items

1. [ ] `structure` 절은 `agent-topology`가 정의한다 — cordboard는 스키마를 소유하지 않는다
1b. [x] ~~`x-cord` 확장 스키마 정의~~ → ADR-0014로 폐기
2. [ ] Agent Card 생성기 — A2A 스펙 필드만. 우리 필드 0개
3. [x] ~~Aegra 설정의 `http.app`으로 두 라우트 마운트하는 것을 `cord up`이 자동 처리~~ → ADR-0016: publication은 Entity 책임
4. [ ] 카탈로그가 두 문서를 긁는 수집기. ~~`agent-topology.manifest.json`이 없거나 `structure` 절이 없으면 등록 거부~~ → 없으면 "그림 없음" 상태, 스키마 위반이면 경고 (ADR-0015)
5. [x] ~~`cord.yaml`의 `name`/`version`/`description`을 A2A 대응 필드와 같은 이름으로 정의~~ → ADR-0014로 폐기. 이름은 그래프의 `compile(name=...)`
6. [ ] Agent Card 스펙 준수 검증 — 우리 카드가 표준 파서에 통과하는지 확인
7. [ ] 슬라이스 1 이후: `agent-topology.manifest.json` 스펙 초안을 쓸지 판단. 쓴다면 IANA provisional 등록 검토


## 정정 — 교환원 경계 (2026-09-11, #7)

[ADR-0013](0013-switchboard-boundary.md)이 모델 소유권의 현재 결정이다.
`x-cord`의 `tiers` 계획을 철회한다. 모델·Tier 정책·공급자 키는
발견 문서의 Cordboard 계약이 아니다. 본문 예제와 Action 1b에서 이를 제거했다.


## 정정 — cordboard 확장 폐기 (2026-09-12)

[ADR-0014](0014-no-graph-descriptors.md)가 매니페스트 안의 cordboard 확장을 폐기했다.
`agent-topology.manifest.json`은 그래프의 생산자가 낸 문서 **그대로**다. 결정 3의 "`structure`
절에 cordboard의 개념이 없어야 한다"는 더 강해진다 — 문서 어디에도 cordboard 절이 없다.

결정 6의 조립 설명 "`x-cord` 절은 `cord.yaml`이, Agent Card는 둘의 교집합이 만든다"도 철회한다.
Agent Card의 이름은 그래프의 `compile(name=...)`에서 오고, 카드는 등록 필수가 아니다.
Subject·승인·트리거는 그래프 문서의 절이 아니라 연결 설정이며, 필요한 슬라이스에서 보드 단위로 정한다.


## 정정 — 매니페스트는 연결의 전제조건이 아니다 (2026-09-12)

[ADR-0015](0015-never-block-connection.md)가 결정 7의 "Manifest를 안 내면 등록되지 않는다"를
대체한다. 연결에는 주소가 있는 Deployment와 호출자가 고른 그래프만 필요하고, 기록에는 span만
필요하다. #7과 #9의 실행이 매니페스트 없이 연결·기록됐다.

두 문서의 모양, 배열 규약, Agent Card에 cordboard 필드를 넣지 않는다는 결정은 그대로다. 바뀌는
것은 두 문서가 **있을 때 쓰는 것**이 되었다는 점이다. 매니페스트가 없으면 뷰어는 그림 없이 실행
기록만 보여주고, 스키마를 어기면 그 문서를 쓰지 않고 경고한다.

## 정정 — publication은 Entity 책임 (2026-09-16)

[ADR-0016](0016-control-plane-and-testbed.md)에 따라 topology 생성·HTTP publication은
Entity가 소유한다. Cordboard는 Graph를 import하거나 Entity의 HTTP app을 조립하지 않고
발행된 문서를 읽는다. A2A는 선택적 backend이며 Agent Card는 core의 필수 계약이 아니다.
기존 topology discovery 경로와 ADR-0015의 선택적 입력 원칙은 유지한다.
