# 이슈 계획 방법론

Cordboard는 1인 개발자가 Sonnet과 GPT 5.5에 구현을 맡기는 프로젝트다.
이 문서는 이슈를 작성하고 준비하고 완료하는 공통 방법을 정한다.
제품의 경계는 [아키텍처](../ARCHITECTURE.md), 결정의 근거는
[ADR 인덱스](decisions/DECISIONS.md)를 따른다.

## 언어

- 모든 이슈의 제목, 본문, 수락 기준, 체크리스트는 **영어**로 작성한다.
  Epic, Feature, Task에 예외가 없다. 계획자가 남기는 진행·완료 댓글도 영어로 쓴다.
- 마일스톤 제목과 설명도 영어로 작성한다.
- ADR은 당분간 **한국어**로 유지한다. 이슈에는 관련 ADR의 정확한 절을 연결하고
  이번 구현에 필요한 결정만 영어로 요약한다.
- 이 방법론 문서는 한국어로 유지한다. 기존 문서를 일괄 번역하지 않는다.

## 계층과 책임

| 단위 | 의미 | 실행 및 완료 |
| --- | --- | --- |
| Milestone | 한 슬라이스가 증명할 결과 | 날짜보다 종료 기준을 명시한다. 하위 이슈 수가 아니라 실제 검증 증거로 닫는다. |
| Epic | 여러 기능과 슬라이스에 걸친 제품 성과 | 직접 구현하지 않는다. 여러 마일스톤에 걸치므로 마일스톤을 지정하지 않는다. |
| Feature | 사용하거나 독립적으로 검증할 수 있는 기능 | 한 PR로 끝나면 직접 실행한다. 여러 PR 경계가 필요할 때만 Task를 둔다. |
| Task | Feature 안의 독립적인 실행 단위 | 별도 PR 또는 별도 계약 검증 산출물이 필요한 경우에만 만든다. |

GitHub의 실제 Issue Type과 native 부모·자식 관계를 사용한다.
Feature는 Epic의 자식, Task는 Feature의 자식이며, Feature와 Task에는 해당
슬라이스 마일스톤을 지정한다. 선행 관계는 본문에 실제 이슈 번호로 연결한다.
부모·자식 관계는 포함 관계이고 선행 관계는 실행 순서이므로 둘을 혼동하지 않는다.

## 너무 잘게 나누지 않는다

- **한 번에 리뷰 가능한 PR**을 기본 실행 크기로 삼는다. 파일 수나 모델의 예상
  작업 시간, 컨텍스트 크기만으로 이슈를 나누지 않는다.
- 구현·테스트·관련 문서 갱신은 같은 이슈에 포함한다. 각 항목을 별도 Task로 만들지 않는다.
- 한 PR로 끝나는 Feature에 형식적인 Task 하나를 만들지 않는다.
- Task를 가진 Feature는 통합 수락 기준을 소유한다. 부모를 위한 중복 구현이나 별도 PR은 없다.
- 외부 계약을 알아야 구현 범위를 정할 수 있을 때만 검증 Task를 둔다.
  공개 문서로 확인하거나 기능 안에서 짧게 검증할 수 있는 사항은 Feature 내부 작업으로 둔다.
- 모든 미래 작업을 미리 Task로 분해하지 않는다. 선행 구현의 결과가 경계를 바꾸면
  해당 Feature를 상세화할 때 나눈다.

## 전체 방향은 먼저, 실행 상세는 가까운 것부터

1. 각 슬라이스를 마일스톤으로 만들고 관찰 가능한 종료 기준을 쓴다.
2. Epic과 Feature를 결과 중심으로 정의하고 선행 관계를 연결한다.
3. 현재 슬라이스만 실행 가능한 본문으로 상세화한다. 뒤 슬라이스는 목표·범위·근거·수락 기준을 적는다.
4. 선행 PR이 합쳐지면 실제 파일, 공개 인터페이스, 테스트 명령, 확정 계약을 후속 이슈에 반영한다.
5. 마일스톤 종료 시 검증 결과를 보고 다음 슬라이스를 상세화한다. 새로 확인된 제약을 반영하고 불필요한 일을 줄인다.

고정 스프린트나 스토리 포인트는 도입하지 않는다. 예정일이 필요해지기 전에는
마일스톤 날짜를 설정하지 않는다. 일정과 구현이 아직 확정되지 않은 기능을 완료된 것처럼 쓰지 않는다.

## 실행 이슈 양식

Sonnet과 GPT 5.5에 같은 명세를 제공한다. 어느 모델도 이전 대화나 부모 이슈를
읽었을 것이라고 가정하지 않는다. 부모를 링크하더라도 작업에 필요한 계약은 본문에 요약한다.

```markdown
## Outcome

Describe the problem and the observable behavior after this issue is complete.

## Context and decisions

- Link the relevant ADR sections and summarize the decisions in English.
- Distinguish confirmed contracts from facts that still require verification.

## Dependencies and readiness

- Parent: #...
- Depends on: #...
- State whether this is executable now or needs refinement after its prerequisites.

## Scope and contracts

- Describe this PR's responsibilities, inputs, outputs, and failure behavior.
- Include documentation updates and tests with the implementation.

## Out of scope

- Name adjacent behavior reserved for later work.

## Acceptance criteria

- [ ] State three to six observable criteria with definite expected results.

## Verification

Describe the smallest fixture, its expected results, and the integration checks.
Use actual commands once they exist; do not invent existing paths or scripts.

## Blockers and handoff

State what to stop if a prerequisite is missing, what evidence to record,
and what a subsequent implementer must receive.
```

Epic에는 성과, 경계, 자식 Feature 목록, 종료 증거를 쓴다. Task가 있는 Feature에는
자식 목록과 통합 수락 기준을 쓴다. 이후 슬라이스의 Feature에는 구체화가 필요한 계약을
표시하되, 그것을 구현자가 알아서 결정하라는 실행 지시로 사용하지 않는다.

## 준비 상태와 실행

| 표시 | 의미 |
| --- | --- |
| `planning:ready` | 선행 조건, 필요한 결정, 범위와 수락 기준이 갖춰진 leaf 이슈 |
| `planning:backlog` | 선행 결과 반영이나 추가 상세화가 필요한 leaf 이슈 |

Epic과 Task를 가진 Feature에는 실행 준비 라벨을 붙이지 않는다.
라벨은 준비 상태이며 GitHub의 open/closed 상태를 대체하지 않는다.

**구현 중인 이슈는 한 개로 제한한다.** 하나를 실행·검증·리뷰한 뒤 다음으로 넘어간다.
차단되면 원인과 증거를 본문에 기록하고 독립 작업으로 이동할 수 있다.
이슈가 상세하더라도 선행 작업이 끝나지 않았으면 `planning:ready`로 표시하지 않는다.

다음 조건을 확인한 뒤 backlog를 ready로 바꾼다.

- 필요한 선행 작업과 ADR 결정이 확정되어 있다.
- 관련 문서가 충돌하면 이슈에 채택된 결론과 정정할 문서를 명시했다.
- 필요한 외부 API·버전·실행 환경을 확인했다. 자격증명 값 자체는 이슈에 넣지 않는다.
- 구현자가 범위나 제품 정책을 다시 결정하지 않아도 된다.
- 최소 검증 입력과 기대 결과가 있다.

첫 코드 이슈에는 아직 없는 경로 대신 책임과 산출물을 적는다. 이후에는 실제 구현 위치와
실행 명령을 연결한다. upstream의 공개 API가 요구사항을 충족하지 않으면 재현과 제약을
남기며, 임시 detector나 내부 API 우회를 자동으로 만들지 않는다.

## 검증과 완료

**무엇을 측정하는지와 입력으로 무엇이 필요한지를 분리한다.** 입력은 측정하려는 성질을
구분할 수 있는 최소 크기로 만든다. 8주 질의 검증에는 고정 시각과 경계 안팎의 소량 데이터면 된다.
실제 8주간 실행하거나 대규모 데이터를 만들 필요가 없다. 실제 통합 검증은 별도로 수행한다.

5분 이상 걸릴 명령을 제안하기 전에 입력을 먼저 읽어 검증한다. 확인 비용이 실행 비용보다
두 자릿수 작으면 반드시 선행한다. 이미 있는 거대한 자산을 쓰는 것보다 작은 fixture를 새로
만드는 편이 싸면 새로 만든다.

완료 시 이슈에 다음을 남긴다.

- 변경된 동작과 연결된 PR 또는 산출물.
- 실행한 검증, 입력, 기대값과 실제 결과.
- 수락 기준 충족 여부와 남은 제한.
- 후속 이슈에 반영할 실제 경로·명령·계약.

실행하지 않은 검증을 통과로 표시하지 않는다. Task를 모두 닫았더라도 통합 수락 기준이
충족되지 않으면 Feature는 열어 둔다. 마일스톤도 마지막 Feature의 검증 결과에 종료 증거를
연결하며, 이를 위해 형식적인 검증 전용 이슈를 추가하지 않는다.

## 생성 후 확인

이슈 생성 전에 같은 계획의 이슈가 이미 있는지 조회한다. 생성 후 타입, 부모·자식,
마일스톤, 선행 번호, 영어 본문과 링크를 다시 조회해 확인한다. 선행 관계에는 순환이 없어야 한다.
일부 생성 후 실패하면 기존 결과를 기준으로 이어서 처리하며 중복 생성하지 않는다.

이슈 작성 방법론은 브랜치 이름, 커밋 제목, PR 자동화 규칙을 새로 정하지 않는다.
별도 요청 없이 프로젝트 보드, 스프린트 자동화, 모델별 할당 체계를 추가하지 않는다.
