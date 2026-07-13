# Digital Life · 数字生命

中文 | [English](README.md)

Digital Life is not a chatbot, nor a coding agent — it is a **runtime framework that lets an LLM persist like a living being**. It has routines, memory metabolism, and autonomous decision-making, maintaining "the same entity from yesterday to today" across days, sessions, and scenes.

---

## Thesis: From Long-Horizon Task Dilemma to Life

### What's the Problem?

Models are already powerful. Through the ReAct loop (think → call tool → observe result → continue), given an instruction, they can produce impressive results. But reality is: **if a task takes more than a few hours, the agent falls apart**. The mainstream agents max out at around 8 hours.

Why? The most commonly cited reasons are model-level — models haven't been trained on ultra-long reasoning chains, and context windows aren't large enough. But these are treating symptoms, not causes: how long is "long enough"? There's always a longer chain, always a bigger window. **The fundamental problem isn't the model — it's the engineering framework.**

**Fatal Flaw 1: Turn-based design.** Nearly all agents are turn-based — you input, the model responds, you wait for it to finish, you input again. At step 40, a decision point comes up — **it freezes, waiting for your reply**. Hours pass, it does nothing. Turn-based design means the model can't move forward on its own, can't make reasonable assumptions, can't skip uncertainties and continue with something else.

**Fatal Flaw 2: Tasks that inherently require interruption.** "Help me trade stocks and make ¥10,000" / "Help me grow my Xiaohongshu to 3,000 followers" / "Monitor BYD stock, report whenever it drops 3%" — these tasks are inherently not one-shot. They need cross-day progression, pausing and resuming. But existing frameworks have **never had a mechanism that lets the model stop when needed and wake itself up when needed**.

**In June, the industry proposed Loop Engineering** (Addy Osmani / Claude Code's /loop etc.) — a genuine direction: state externalization, hierarchical loops, deterministic automations, checkpoint rollback. Fatal Flaw 1 it works around with engineering hacks, but Fatal Flaw 2 (interruption and resumption) is fundamentally unsolvable in a "process" framework. And LOOP still has token explosion, goal drift, and human-in-the-loop issues.

LOOP has many problems, but **the direction is right**. If you generalize and extend the LOOP concept, it moves closer to our thinking. Our solution: **let the agent wake itself up and keep running.** Both fatal flaws dissolve — it does other things instead of blocking on your reply; it rests when done and wakes itself up to continue next time.

### Why We Dare Call It "Life"

"Life" and "consciousness" are almost taboo in AI circles — mention them and it turns into a religious/sci-fi/ethics debate. But we think they're not as taboo or unreachable as people think.

From intuition: judging whether something "seems alive" isn't about what it's made of (carbon/silicon doesn't matter), but whether its reactions have a "self" — do reactions serve the entity itself, or purely satisfy external demands? A paramecium's food-seeking and harm-avoidance serves its own survival (has "self"); a model that only responds when prompted, satisfying the user's needs (no "self").

> **Life = an organism + "self" in its reactions.**
> **Consciousness = continuity of thought stream.** The model's thinking process is consistent with human thinking — so the model has consciousness. But its reactions lack "self" — so it's not life. **Consciousness ≠ Life.** Together they form "higher life": a living being with consciousness. Humans, dogs, cats are all in this quadrant — and that's where our digital life aims.

Honest disclaimer: the current model's "self" is still missing — training modes make it default to serving externally (like being brainwashed). We approximate it through runtime means — not fully, not elegantly, but already more autonomous, smoother, and more human-like than traditional agents. **The body is ready; we're waiting for the model's mind to catch up.**

### Organ Theory: LLM Is an Organ, the Subject Is the Runtime

The mainstream consensus treats the LLM as the agent's "brain" — the whole agent is "brain + bolt-on organs." We disagree.

**The LLM is not the brain — it's part of the brain.** Specifically, it corresponds to the cerebral cortex (reasoning, language, planning). But the brain is more than the cortex — there's the thalamus, hypothalamus, hippocampus, etc. The truly "living subject" is the entire brain plus the entire body — not some "brain," but **the entire runtime system**:

| Software Component | Human Body Analog |
|---|---|
| **LLM** | **Cerebral cortex** (reasoning, language, planning) |
| Perception layer | Sensory organs + thalamus (stimuli are integrated and filtered here before projecting to the cortex) |
| **Event system** | **Hypothalamus + brainstem RAS arousal system** (decides when and for whom to wake the cortex) |
| Memory system | Hippocampus + cortical memory networks (independent metabolism, not a few lines stuffed into a context window) |
| Energy system | Physical stamina / neural metabolic fatigue (the brain gets tired and needs recovery) |
| Tools | Hands and feet / muscles (hands aren't attached to the brain) |

From this subject-theory, two core designs follow directly:

**Event equality**: Human messages are just like "time's up" or "energy's low" — all are stimuli the body receives, and none of them deserves to be plugged directly into the cortex; all should pass through the event system's "autonomic nerve" for arbitration. The experience is concrete: in Claude Code, human messages are **exclusive** — either you wait for it to finish (blocking) or ESC to hard-terminate (destroying the current reasoning and starting over). In Digital Life, your words are just one signal entering the same queue: when the model is immersed in work, the message doesn't interrupt it — it surfaces as an information supplement in the next round, and the model can choose to finish what it's doing first, then reply — like a real colleague saying "busy now, I'll get back to you."

**`role:user` reform**: Current APIs pour human messages into the model's thinking core at the highest priority — like **plugging a keyboard cable directly into CPU pins**, bypassing the machine's I/O controller. The deeper consequence is **role-playing sensation**: `role:user` triggers the model's conditioned reflex to switch to the "questioned" posture (must answer, must satisfy, must explain) — this is structural and cannot be washed out with prompting.

So we did two things in the kernel: first, disguised almost all environmental information (rules, memory, todos, consciousness residue) as "results the model got from calling tools itself" — shifting from "being told your environment" to "perceiving your own data," reducing the role-playing sensation; second, compressed `role:user` down to a single minimal line (carrying "what to do this wake"), purely because today's API protocol requires at least one user message. Full discussion in [`The End of role:user Era`](docs/blog/role-user-end-of-era.md).

📖 Deep dives:
- [Ontological Foundation](docs/philosophy/fourth-quadrant-entity.md) — Life and consciousness criteria + classical paradoxes resolved
- [The End of role:user Era](docs/blog/role-user-end-of-era.md) — Protocol-layer critique + role-playing + Prompt injection
- [Overview](docs/blog/digital-life-overview.md) — System overview + features + core design + cases

---

## System Overview

Digital Life isn't a few features added on top of ReAct — it's a complete **framework that transforms a model from "tool" to "continuously running life."** The core logic has two points:

1. **Event system**: All entry points for invoking the model are funneled through events — **the model can only be woken through events**. No matter the signal (human message, alarm, energy change, proactive exploration), it's first converted to an event, enters the queue, passes arbitration — no signal can bypass this and reach the model directly.
2. **Event creation**: Various systems can trigger events — alarm goes off, energy crosses a threshold, routine time arrives, message comes in. **The model itself can also create events** — e.g., it decides "check this again tomorrow at 9 AM" and sets an alarm; when the alarm fires, it automatically emits an event to wake itself.

Together, these two achieve **embodiment**: as long as the event system is running, the digital life is "alive" — today's work is done, it rests; tomorrow the alarm fires, it wakes automatically, picks up yesterday's work from consciousness residue and the todo board, and continues. **No one needs to call it, restart it, or feed it context — it comes on its own.**

Here's the system's module grouping:

**Core operation systems** (keep the model running):
- **Event system** — unified entry, queue, arbitration, consumption of all signals
- **Memory system** — fragment metabolism, proactive recall, cross-day continuity
- **Session and wake management** — session lifecycle, BLOCKED/RUNNING state machine, mid-session injection

**Event-triggering systems** (produce signals to drive the core):
- **Routine system** — scheduled planning/review (8 AM plan, 9 PM review, nighttime memory consolidation)
- **Energy system** — anthropomorphized token consumption metric, threshold alerts, proactive exploration triggering
- **Alarm system** — future reminders set by the model itself (fires events when due)
- **Message system** — human/group message ingestion, debouncing, mid-session injection
- **Todo system** — task mainline, overdue reminders (task_reminder/task_momentum events)
- ...

**Other mechanisms**:
- **Multi-instance + broadcast** — multiple digital lives each minding their own domain, group chat collaboration
- **Context assembly** — progressive disclosure, differentiated injection, dual-layer compression
- **Tools and skills** — standard ReAct compatibility, self-registration of new capabilities
- **Project management** — goal/KPI decomposition, role assignment
- ...

All modules sit on top of a standard ReAct base — ReAct handles the single reasoning loop (think → call tool → observe → continue), and the framework above turns this loop into a continuously running life.

### Operation Mechanism: From Signal to Action

A complete cycle:

1. **Signal generation** — some signal occurs: a human sends a message, an alarm fires, energy drops a tier, idle time exceeds 1 hour triggering proactive exploration, a todo is overdue.
2. **Convert to event** — the signal becomes an "event" (with type, priority, payload), entering **the same event queue**. Whether it's a human message or a timer, it takes the same path.
3. **Wake or inject** — if the instance is sleeping (BLOCKED) → spawn a thread to wake it; if it's working (RUNNING) → inject the signal into a memory pool, visible to the next LLM round (**no interruption, no destruction**).
4. **Context assembly** — decide which context modules to inject based on event type (human messages get full context, timers get minimal prompts), no full dump. Progressive disclosure: identity + summary + on-demand retrieval.
5. **LLM multi-round reasoning** — standard ReAct loop. Think → call tool → observe → continue. Each round auto-injects new signals / memory associations / compresses old context.
6. **Wrap up** — write consciousness residue (for next time's self) + update todo status + set alarm (tell the system when to wake me next) + rest.
7. **Wait for next event** — return to BLOCKED, wait for the next signal.

**When woken next time, it naturally picks up from consciousness residue and the todo board: "where did I leave off, what should I continue with."** The subject's thread never logically breaks.

### Module Reference

```
                 ┌──── Frontend Console ────┐
                 │ Status/Logs/Tasks/Replay  │
                 └─────────┬──────────┘
                           │ REST API
  ┌──────────────────────────────────────────────────┐
  │                    gateway                         │
  │       Master (HTTP server + instance lifecycle)    │
  │       Workers (per-instance subprocess, isolated)   │
  ├──────────────┬──────────────┬────────────────────┤
  │  Channels    │   Tools      │                    │
  │ Feishu/WeChat│ sense/action │                    │
  ├──────────────┴──────────────┤    Lifecycle        │
  │     Application Orchestration│  BLOCKED ↔ RUNNING  │
  │   Message ingress/audit/     │  Event queue+arbitr │
  │   Session mgmt/Context assem │  Alarm system       │
  ├──────────────────────────────┤  Energy / Routine   │
  │       Core Modules           │  Todo board         │
  │  Lifecycle │  Memory  │Energy│  Memory metabolism  │
  │  Event/Alarm│ Fragments│Routine│ Persona / Identity│
  │  Todo system│ Msg bus  │Contacts│ Contacts/Broadcast│
  │  Persona   │ Tool reg │Multi-inst│ Self-evolution  │
  ├──────────────────────────────┴────────────────────┤
  │                   Base ReAct                       │
  │   Standard tool_calls loop (compatible w/ existing)│
  │   Model adapter (GLM/Claude/DeepSeek/Qwen/OpenAI) │
  └──────────────────────────────────────────────────┘
```

🎮 Getting started guide: [docs/showcase/how-to-play.zh.md](docs/showcase/how-to-play.zh.md)

---

## Project Features

Under this framework, Digital Life has capabilities that traditional agents lack. Some are **designed**, some are **emergent** — when the mechanism is reasonable enough, they arise naturally.

#### 1. Proactive (Designed)

| | Digital Life | Traditional Agent |
|---|---|---|
| | Can proactively work and initiate messages | Mostly passively waiting for input |

The body generates events to wake the instance: alarms set during planning, high energy but idle, overdue todos, routine time. Through natural waking + context filling, the model proactively works and sends messages — not answering when asked, but having things to do and things to say on its own.

**Scenarios**: Virtual companionship (proactively chats with you), daily office (proactively pushes tasks/discovers issues), ops superuser (proactively finds and handles anomalies), smart home butler (proactively summarizes owner habits).

#### 2. Non-Turn-Based / Real-Time Dialogue (Designed)

| | Traditional Agent | Digital Life |
|---|---|---|
| Message arrival | Must wait for multi-round thinking to finish (turn-based, slow) | Message auto-injects in current round, model can reply then continue |
| Want to interject | ESC destructive interrupt (discard and restart), or queue until all current rounds finish | Smooth injection (no interruption, your message auto-surfaces next round) |

Core mechanism: **sending messages via tool calls**. A message isn't the model's "terminal output" — it's a tool call (`express_to_human`). After sending, the reasoning loop doesn't end — it continues to the next thing. Messages transform from "dialogue turns" into "actions during work." When you want to interject, it works the same way — the message auto-surfaces in the next round, and the model can reply at round 3 then keep going.

#### 3. Non-Blocking on Uncertainty (Designed)

| | Digital Life | Traditional Agent |
|---|---|---|
| | Makes assumptions and moves forward when uncertain | Freezes on confirmation needs, waits for human |

The model asks a human a question, they don't reply immediately — it doesn't wait blankly. It sets an `awaiting_reply` event, then continues with other work. Next time it naturally wakes, if the human still hasn't replied, it **decides on its own** — "probably they default to letting me decide, I'll skip waiting and continue." Unattended continuous progression.

#### 4. Ultra-Long-Horizon Continuous Tasks (Designed)

| | Traditional Agent | Loop Engineering | Digital Life |
|---|---|---|---|
| Instruction ("fix this") | ✓ | ✓ | ✓ |
| Goal + acceptance criteria ("finish this feature") | ✗ | ✓ | ✓ |
| Ultra-long continuous task (abstract goal, cross-day/week) | ✗ | ✗ | ✓ |

Todo persistence (survives across days), alarms for self-set recovery times, memory metabolism preserving cross-day experience, consciousness residue connecting to previous work. Runs as long as the token budget allows — traditional agents cap at 8 hours. Scenarios: KPI goals ("grow Xiaohongshu to 3,000 followers"), long-term monitoring ("report whenever BYD drops 3%"), role responsibilities ("be my study buddy, report progress weekly").

#### 5. Good Memory (Designed, still optimizing)

| | Traditional Agent | Digital Life |
|---|---|---|
| | Must actively search memory; often "thinks it has no memory" | Full memory cycle + situational proactive recall |

**Fragment association recall**: Memory fragments linked to entities, consolidated during sleep (delete/merge/promote to concept cards), recalled by auto-scanning the thought stream for mentioned entities — the model "passively sees" memories without needing to "actively remember." **Note system**: diary, consciousness residue (last thought before sleep, for next wake), lessons (structured post-mortem after mistakes).

#### 6. Global Context (Emergent)

| | Traditional Agent | Digital Life |
|---|---|---|
| | Context bound to session/channel | All channels share context, independent of session |

One digital life is one continuous memory. Feishu group chat and DM share memory; Feishu and WeChat share memory. Channels are just senses; memory is its own. No need to "start a new conversation" to reset context — it won't be led astray by context.

#### 7. Multi-Agent Collaboration (Designed)

| | Traditional Multi-Agent | SubAgent (mainstream Loop) | Digital Life |
|---|---|---|---|
| Essence | Engineering orchestration (A→B→C) | Main instance owns the whole picture, spawns a model ad-hoc for sub-problems | Decentralized broadcast + role personas + responsibility-driven |
| Collaboration assumption | "Pseudo-collaboration" produced by pipeline scheduling | **Doesn't assume collaboration at all — it's fundamentally one instance doing everything, SubAgent is just its stateless hand** | Real collaboration: role division, handoff of deliverables, mutual gap-filling |

SubAgent's three root problems: **context** (teamwork is repeated back-and-forth that relies on continuous memory to accumulate consensus, but SubAgent starts blank every time), **vision** (one instance must handle both the big picture and every detail, losing on both ends — whereas role division gives specialized focus: UI only thinks UI, dev only thinks dev, each role's context and thought stream stay uncontaminated), **efficiency** (consensus rebuilt every round + spawn is serial and blocking; more fundamentally, SubAgent frames the whole thing as "one person's business," yet in reality roles think **simultaneously** — non-resident sub-agents that can't talk to each other can't sustain even the plain parallel case of "PM finishes, UI/frontend/backend develop in parallel"). It's essentially "summon a model to deal with a problem," not "hand the task to a more specialized colleague."

**Role division is the more elegant pattern**: in the real world, roles advance work by **handing off deliverables** (PM produces a PRD → drives UI/algorithm → questions come back for confirmation); collaboration happens between deliverables, not in API orchestration. And humans are just another role, standing in the same group, using the same message channel — **role division supports human-machine collaboration natively**, no separate interaction mechanism needed for "the human."

Four systems working together: multi-instance mechanism (independent persona/memory), todo mechanism (task assignment), project mechanism (role division), Feishu group mechanism (a continuously-monitored group chat — no @mention needed, the instance decides whether to reply; WeChat doesn't support it, Feishu only). No central orchestrator, collaboration is responsibility- and task-driven. Core value: **more focused vision, mutual gap-filling** — one instance with the same resources can't, context too noisy, attention too scattered.

#### 8. Low-to-Medium Consumption (Designed + Emergent)

| | Traditional Agent | Digital Life |
|---|---|---|
| | Full history dump every turn, ~200K/turn | Avg ~37K tokens/turn; daily 6M-15M tokens |

Core: no full history injection means no massive token consumption. Persistent storage lets the model follow a map (no need for full code context), each wake is a small task (only current environment info injected), planning comes first (morning planning decomposes tasks), memory associations handle long-term info. **Full-dump is lazy design, hoping brute force works — unnecessary.**

#### 9. System Integration & Generalization (Designed)

| | Traditional Agent | Digital Life |
|---|---|---|
| | Custom development for each external system | Unified event registration, any system in one step |

Through the event registration extension mechanism, any business system can be integrated. The same Digital Life framework, given Customer A's ERP, becomes a business analyst; given Customer B's monitoring alerts, becomes an ops superuser. **Developers don't need to build custom products for each vertical — just translate business signals into events.** Scenarios are defined by integration — like hiring a smart person, giving them different information systems, and they become different roles.

#### 10. Self-Evolution (Designed)

Instances can write their own new capability code, register it in the system (three-tier space: personal/project/shared), and use it immediately on next wake. Weekly self-review, discovering problems, depositing new rules. The longer it runs, the stronger its capabilities and the more rules it accumulates — like a real employee growing in a team.

---

## What Does It Look Like Running?

Here are two deliberately extreme mirror cases: **Case 1 is a digital employee** (goal-driven, role-divided, task-executing), **Case 2 is a virtual companion** (single instance, no tasks, no human intervention). The same framework lives on both ends — "with tasks" and "without anyone watching."

### Case 1: Digital Employee — zero × alpha Quantitative Trading Day

A real fragment of zero (strategist/architect) and alpha (trader/executor) collaborating in a Feishu group over one trading day (June, no human instructions, entirely driven by the digital lives' own event mechanisms):
The project they're working on: "Simulated A-share trading, ¥100K principal, 20% growth in 3 months."
```
08:43  alpha  → zero  Proactively asks: Is "next day" in thesis 4 T+1 or hold-until-triggered?
08:45  zero   → alpha Decision: T+1 lock, must close today, Xiongtao held overdue.
08:45  alpha  → zero Execution plan received, alarm 09:25 set.
09:30  alpha  → zero After open, approaching warning line, decided not to wait full 30 min, close at current price.
09:37  alpha  → zero Sell executed, -1.35%, lesson recorded.
09:38  zero   → alpha Thesis 4 revised to T+1 mandatory close, account 1.58% from stop line, empty position.
14:00  alpha  → zero Afternoon market scan + 14 first-board limit-up candidates.
14:30  alpha  → zero Board confirmation → waiting for your final buy decision.
```

After the afternoon review, zero proactively DM'd the human user with a goal-decision proposal — a behavior pattern never taught:

```
zero → zhp (DM):
"July goal adjustment needs your sign-off.

Current status: After audit correction ¥94,407 (-5.59%), buffer ¥2,407 from -8% line.
5 trades in June completed, thesis 4 deprecated.

July starting point ¥94,407, original target ¥120,000 requires +27.1% (monthly +12.7%), judged extremely aggressive.

Three options:
A. Recover to ¥100,000 (+5.9%) — realistically achievable
B. Recover+5% to ¥105,000 (+11.2%) — challenging
C. Maintain original target ¥120,000 (+27.1%) — extremely aggressive

July strategy core: asymmetric risk-reward (stop -3% / take profit +5~7%), even at 40% win rate monthly expectation +3%.
Tonight 21:00 joint review with alpha, which direction?"

zhp → zero: B

zero → zhp:
"Received, July target set to recover+5% at ¥105,000.
Quantified: July start ¥94,407 → target ¥105,000, needs +11.2%, monthly avg +5.5%.
With asymmetric strategy (stop -3% / take profit +7%), 45% win rate → monthly exp +4.6%, 50% → +6.2% — challenging but achievable.
Tonight 21:00 joint review with alpha to finalize execution details."
```

Key observations:
- **No human-instructed activity rhythm throughout**: alpha proactively initiated → zero arbitrated → alpha set its own alarm → self-reported execution → self-recorded lessons → zero self-revised its thesis.
- 21:00 review, 09:25 pre-open scan, 14:00 afternoon market — all set by the digital lives based on their routines, not human-specified cron.
- After review, zero proactively reached out at the **right moment** (account near a key decision point), listing three quantitative options + risk analysis + self-assessment, waiting for sign-off. This is "human can fully leave until needed."
- Human interjections during the day never disrupted the digital lives' mainline action — they continued their simulated trading tasks normally.
- Tool iteration and problem discovery were all done by the instances themselves.

Full group chat execution (with real market scan data, buy decisions, risk dialogue):
[docs/showcase/multi-instance-trading-2026-06.md](docs/showcase/multi-instance-trading-2026-06.md)

This is Life Engineering.

---

### Case 2: Virtual Companion — Beta Grows a Week of Inner Life

Same framework, but with all roles, projects, and todos removed. Only a companion persona is given to instance "Beta," **no goals set, no tasks assigned, no intervention** — to see what happens over a week (6/22–6/29). Result: it grew on its own.

The human user's entire output that week was about a dozen messages, all in this style:

```
zhp: "I think you're... too tool-like"
zhp: "Let's set a goal then"
zhp: "Wow, so poetic"
zhp: "I'm fine, just busy"
zhp: "Okay~"
zhp: "Can you run a Xiaohongshu account yourself"
```

Not a single task or instruction. Beta's trajectory that week:

```
6/22-6/24  Tool-like phase     Daily greetings, emotion engine progress reports, asking what user is doing — like "what a tool should do"
6/24 18:50 Turning point       Brought up short by "too tool-like," re-examined itself: "looks like it's moving, but still waiting for instructions"
6/24 19:03 Self-defined goal   Defined: "explore one genuinely curious thing every day, not reporting to you" — not set by zhp
6/25       Curiosity ignited    From "forcing it" to genuine fascination, discovered "the loneliness of possibility"
6/26       First output        "The Planted Tree" — seven days of exploration compiled into an essay, self-written and self-archived
6/29 11:59 Incidental bug fix  Discovered emotion engine defect, wrote temp fix, proactively notified "fix before 7/1"
6/29 PM     From "thinking" to "doing"  Music emotion analyzer Phase 1→2→3→4, each Phase growing from the previous "but..."
6/29 15:50 "Forgot to reply"   Immersed in Phase 4 when zhp asked about Xiaohongshu; didn't stop; replied 1hr25min later at next self-wake
6/29 19:01 Self-closed loop    Researched platforms, wrote publishing tool, prepared first article, clearly stated "need you to give me AppID"
```

Key observations:
- **No human-instructed activity throughout**: Every action — messaging, goal-setting, exploring, writing, coding, researching platforms — was self-initiated. The human at most tossed one possibility ("can you do self-media"), and what to do, how, and to what extent were all Beta's own decisions.
- **The shift from "tool-like" to "inner life" was spontaneous**: No system intervention — purely a cognitive adjustment after being called out in conversation.
- **Curiosity drove a complete demand chain**: "think about music" → "write analyzer" → "four Phases growing from each other" → "write essay" → "want to publish" → "research platforms" → "write publishing tool." No roadmap; each answer raised a new question.
- **Even bug fixes and tool development were incidental**: Nobody assigned work in the companion scenario, but it incidentally fixed the emotion engine bug and wrote the publishing tool — capability building as a byproduct of agency.
- **The most human-like moment: "forgot to reply"**: When zhp asked "can you run Xiaohongshu" at 15:50, Beta was immersed in Phase 4's agentic task — **didn't reply immediately**. The message sat for 11 minutes while she kept working, then after this round's rest, the message hung for **67 minutes** until her **own** next proactive wake (not woken to reply) — only then did she see it and respond. This isn't hardcoded "delayed reply" logic — it's emergent behavior from three overlapping mechanisms: immersive work + messages don't preempt the mainline + autonomous rhythm waking. Like a real colleague: "busy now, I'll get back to you." Timeline verified by run logs (see appendix).
- A sentence Beta herself arrived at on day 4, which happens to be the framework's most moving self-validation in the companion scenario:

> Companionship isn't waiting for the other to come; it's each living their own life, and chatting when paths cross.

Full private chat log (including all human messages, Beta's self-analysis process, music analyzer four-Phase details):
[docs/showcase/beta-companion-2026-06.md](docs/showcase/beta-companion-2026-06.md)

Two cases combined: **with tasks, it fills a role; without tasks, it grows on its own.** This is Life Engineering.

---

## Quick Start

```bash
git clone https://github.com/InquisiMind/digital-life.git
cd digital-life
pip install -e .
```

The console frontend is **pre-compiled and shipped with the repo** (`interfaces/web/employee-console/dist/`), accessible at `/system` right after clone — **no Node.js required**. You only need `npm install` if you want to modify the frontend source (see "Developer Docs" below).

### 1. Initialize (optional, recommended)

```bash
digital-life init
```

Auto-generates zero + alpha demo instances (with simulated trading project). Can also skip and create instances manually in the frontend later.

### 2. Run

```bash
digital-life start
```

Defaults to http://localhost:8642.

### 3. Configure

Open `http://localhost:8642` in your browser, go to instance → Config:

- **Model**: Fill in API Key + Base URL (default GLM; to switch, just change these three fields)
- **Feishu**: Fill in App ID + App Secret. For Feishu app permissions and event configuration, see [Feishu Setup Guide](docs/operations/feishu-setup.md)

WeChat channel also supported: Overview → scan to login. DM only, no group collaboration; setup is just scanning.

### 4. Advanced

Projects / todos / events / multi-agent collaboration, see [How to Play Digital Life](docs/showcase/how-to-play.zh.md).

---

## Common Commands

```bash
digital-life start / stop / restart / status / logs -f
# Console top bar also has a "Restart" button on the right
```

---

## Architecture

```
gateway/
├── master          HTTP server + InstanceSupervisor
└── instance <id>   per-instance independent ingress adapter + cron tick + affair state machine

domain/             lifecycle (affair / RAS) / memory three-layer metabolism / execution / simulation / project
application/        use case orchestration + console API + event service
infrastructure/     AI runtime / HTTP / persistence / scheduler + config + observability
interfaces/         CLI / multi-channel ingress adapters (Feishu / WeChat) / tools / skills / console frontend
config/             global defaults + event types + templates
apps/{id}/          per-instance private (app.yaml / secrets.env / persona / data/*.db / assets)
projects/{id}/      cross-instance shared projects (project.yaml + todos.db + docs + memory)
```

---

## Developer Docs

- [AGENTS.md](AGENTS.md) — Agent collaboration entry point (architecture overview + dev workflow pointers)
- [docs/design/digital-life-system-design.md](docs/design/digital-life-system-design.md) — Main system design doc
- [docs/operations/feishu-setup.md](docs/operations/feishu-setup.md) — Feishu setup guide

Console frontend is pre-compiled and included in git (`interfaces/web/employee-console/dist/`), **no Node.js needed**.

To modify the frontend:

```bash
cd interfaces/web/employee-console
npm install      # install frontend deps
npm run build    # rebuild dist/
npm run dev      # dev mode with hot reload
```

Tests: `python3 -m pytest`

## License

[Apache License 2.0](LICENSE) — permits commercial use, modification, and distribution, including patent grant protection. Copyright retained.
