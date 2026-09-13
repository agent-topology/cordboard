# Agent instructions

## Required context

1. Read [ARCHITECTURE.md](ARCHITECTURE.md), [CONVENTIONS.md](CONVENTIONS.md), and
   [docs/decisions/DECISIONS.md](docs/decisions/DECISIONS.md) before making material changes.
2. Read the specific ADRs relevant to the change. For upstream integration work,
   also read [cordboard-upstream-requirements.md](docs/decisions/cordboard-upstream-requirements.md).

Project-wide ADRs live under `docs/decisions`. Follow their existing Markdown
metadata and update the index when adding or changing a decision. There is no
`scope: workspace` front-matter convention in this repository.

## Project context

Cordboard is a local tool for connecting agent graphs and recording their
execution. Implementation has begun: the `cord_runtime` package under `src/`
with tests under `tests/`, on Python 3.12 and uv (`uv run --locked pytest`).
Some integration tests require Docker or pinned external binaries; see the
markers declared in `pyproject.toml`.

[ARCHITECTURE.md](ARCHITECTURE.md#status-and-source-of-truth) states what is
implemented, what is verified, and what is still planned. Read it instead of
inferring status from the design documents, and keep it current when a slice
lands. Design material describes target interfaces — the `cord` lifecycle
commands in particular do not exist yet — so do not cite it as evidence that
something works, and do not record an unrun check as passed.

- The platform consumes manifests, execution APIs, and spans; graph business
  logic and graph-specific state stay inside the graph.
- The caller selects the Graph/Assistant. Models, provider keys, SDKs, gateways,
  and Tier policies belong to that graph. Do not require model configuration to
  connect it. ADR-0013 supersedes ADR-0004; optional LiteLLM code is an example.
- Use public `agent-topology` and `redact-secret` APIs. Their formats and
  detectors belong upstream.
- Cordboard connects graphs and records runs; it does not describe a graph.
  There is no per-graph configuration file and no Cordboard extension inside a
  graph's manifest. Before adding any setting, ask whether it describes one
  graph or a connection; only connections belong to Cordboard (ADR-0014).
- Explicit ADR corrections supersede older examples and HTML artifacts. Check
  the open documentation issues in `ARCHITECTURE.md` before implementing an
  affected contract. Do not silently turn conflicting notes into decisions.
- Preserve existing user work. Verify a documentation change with reference and
  consistency checks; do not write application code to satisfy a documentation
  task, and do not extend the implementation beyond the current slice.

## 일 좀 똑바로 하자

- 5분 이상 걸리는 명령을 제안하기 전에, 그 입력을 먼저 읽어서 검증한다. 검증 비용이 실행 비용보다 두 자릿수 작으면 무조건 먼저 검증한다.
- 시험/검증 작업에서는 "무엇을 측정하는가"와 "무엇이 입력으로 필요한가"를 분리한다. 입력은 측정 대상을 만족하는 최소 크기여야 한다.
- 기존 자산(이슈, 브랜치, 파일)에서 고르는 것이 유일한 선택지라고 가정하지 않는다. 새로 만드는 쪽이 더 싸면 그쪽을 먼저 제안한다.
- 반론이 들어오면, 내가 답하기 쉬운 반론이 아니라 실제로 제기된 반론에 답한다.

<!-- graft:start -->
## Graft — repo context graph

This repo is indexed in `graft/`: small linked markdown nodes that explain each
system and carry exact file:line spans, kept in sync with the code through git.

For ANY task here — understanding how something works, finding where code lives,
or scoping a change — get context from the graph before grepping or opening
source files. Re-ask freely (it's cheap) and reuse literal identifiers you
already have (symbol, error string, file name) as the query. New to this repo?
Run `graft map` first — a token-budgeted orientation (dir clusters, hubs,
hotspots), no LLM, no key.

- Run `graft ask "<your question>" --source` → ranked nodes with the relevant
  code spans inlined (each hit's ≤8-line crux by default; `--full` for whole
  definitions when the crux isn't enough). Match the tool to the task shape:
  for understanding or editing, the top node IS the answer — cite its
  `covers:` file:line spans and edit straight from `--source`. For
  exhaustive tasks ("every occurrence / every caller of this pattern"), ranked
  results are top-N, not complete — run `graft grep "<literal>"` instead
  (exhaustive over indexed files, grouped by enclosing symbol), falling back
  to raw `grep -rn` only for unindexed files.
- `graft skeleton <file>` → every definition's signature + span, ~10× cheaper
  than reading the file; use it to skim an API surface.
- `graft callers <symbol>` gives precomputed, exact edges — who calls this.
  Add `--direction out` for what it calls, or `--depth N` to walk
  transitively for the full blast radius. For structural questions, skip
  ranking and use this directly.
- Or browse: `graft/INDEX.md` lists every node; follow the links.
- Monorepos and folders of multiple repos rank fairly across sub-projects —
  hits carry `[scope/]` labels naming which one they're from. Narrow with
  `graft ask "<task>" --in <scope>/` once you know where you're working.

If a returned span is truncated ("+N more lines"), open the file at that exact
range before finalizing. Only open source files when a node genuinely lacks a
needed detail, and then at the exact file:line the node points to — never
re-read whole files.

After big code changes, refresh the graph with `graft build` (deterministic,
no API key, $0).
<!-- graft:end -->

## Tool precedence — graft first, rtk second

They answer different questions; do not let one stand in for the other.

- **Where is the code / who calls it / what does this change break** → graft
  (`graft ask`, `graft grep`, `graft skeleton`, `graft callers`). Query it
  before any `grep`, `rtk grep`, or whole-file read.
- **Compressing the output of a command you are already running** (build, test,
  git, gh, package managers) → prefix it with `rtk`. See `RTK.md`.

`rtk read` / `rtk grep` / `rtk find` are the fallback for files graft has not
indexed, or for opening the exact `file:line` graft already pointed you at.

## Issue workflow

Follow [Issue planning](docs/issue-planning.md) when creating, refining, or
preparing issues for implementation. Write all issue and milestone content in
English; keep ADRs in Korean for now. Use Feature issues directly when one PR is
sufficient, and add Tasks only for distinct implementation boundaries.

No local feature-gate, issue-resolution, or PR-review workflow skills are
installed in this repository. Do not assume another project's branch naming,
commit-subject rules, or PR automation applies. Follow the current task's scope
and authorization; establish project-specific automation only when it is added.

## Working from a GitHub issue

An issue URL carries no code vocabulary, so graft's prompt hook has nothing to
match on and injects nothing. Before touching source, read the issue body and
run `graft ask "<the issue title or the symbols it names>" --source` yourself.
A fresh worktree also has no `graft/` (it is gitignored). When source files exist,
run `graft build` once before source discovery. If the task only concerns
unindexed documents, inspect those documents directly after checking the graph.

@RTK.md
