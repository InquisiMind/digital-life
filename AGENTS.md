# Repository Guidelines

This is the concise AI-agent entry point for Digital Life.

Chinese version: [AGENTS.zh.md](AGENTS.zh.md). Detailed Claude guidance: [CLAUDE.md](CLAUDE.md).

## Quick Context

Digital Life is an event-driven autonomous LLM agent runtime. The system turns human messages, timers, routines, energy changes, and initiative triggers into lifecycle events so an agent can continue work across wakeups.

## Progressive Loading

- Start with [CLAUDE.md](CLAUDE.md) for hard rules and commands.
- Follow [docs/development/development-workflow.md](docs/development/development-workflow.md) for every implementation task.
- Apply [docs/development/spec-kit-policy.md](docs/development/spec-kit-policy.md) before implementation.
- Use [docs/ai/context-loading-guide.md](docs/ai/context-loading-guide.md) to choose task-specific docs.
- Use [docs/architecture/current-system.md](docs/architecture/current-system.md) for current module responsibilities.
- Use [docs/development/commands-and-testing.md](docs/development/commands-and-testing.md) for commands and tests.
- For Python changes, load [docs/development/python-coding-standards.md](docs/development/python-coding-standards.md) and [docs/development/python-testing-and-review.md](docs/development/python-testing-and-review.md).
- Use [docs/operations/instances.md](docs/operations/instances.md) for instance operations.

## Before Implementation

Declare `Spec Kit Mode: full | lightweight | none` and a reason. Load
`specs/{feature}/` only for the active `full` or `lightweight` task. Those modes
authorize the official numbered Spec Kit branch; ordinary tasks do not create
branches unless explicitly requested.

For Python implementation or refactoring, use the project `python-development`
workflow. For Python review, use `python-review` and keep the review findings-first.

## Architecture Snapshot

- `gateway/` — runtime entry and instance supervisor.
- `interfaces/` — CLI, Feishu ingress, tools, skills, employee-console frontend.
- `application/` — use cases, normalized message workflow, console APIs, ingress checks, event service.
- `domain/` — lifecycle, memory, orchestration, execution semantics, feedback, identity, projects, vital simulation, flow logs.
- `infrastructure/` — AI runtime, HTTP, persistence, scheduler, config, filesystem, observability.
- `apps/{id}/` — per-instance persona, config, memories, and runtime data.

## Commands

```bash
digital-life start
digital-life restart
digital-life status
digital-life logs -f
digital-life stop
python3 -m pytest
```

Do not use legacy Hermes gateway commands for this repository.

<!-- SPECKIT START -->
For active `full` or `lightweight` Spec Kit work, read the current feature plan
after applying the Spec Kit gate. Do not load feature plans for ordinary tasks.

Active feature plan: [specs/002-unified-memory/plan.md](specs/002-unified-memory/plan.md)
(unified memory system; branch `002-unified-memory`; status: planning complete, ready to task-generate)
<!-- SPECKIT END -->
