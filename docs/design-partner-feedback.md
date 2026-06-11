# Design Partner Feedback Program

> **Purpose.** Four weeks of structured, low-effort feedback from Phase 1
> design partners. Each week's question set takes ≤10 minutes to answer.
> Responses drive the post–Show HN roadmap and the Phase 2 backlog.

## Cohort

Target: 5 active partners (10 invited, 5 instrumenting real pipelines).

| Partner | Framework | Pipeline focus | Active since | Status |
|---|---|---|---|---|
| _Partner 1_ | _LangGraph / CrewAI_ | _e.g. invoicing, support, deploys_ | _YYYY-MM-DD_ | _Active / setup / dropped_ |
| _Partner 2_ |  |  |  |  |
| _Partner 3_ |  |  |  |  |
| _Partner 4_ |  |  |  |  |
| _Partner 5_ |  |  |  |  |

> _Founder note: fill in the table as partners onboard. Anonymise externally; keep real names here for the internal source of truth._

---

## Week 9 — Setup

Goal: get to first instrumented tool call. Measure friction.

### Questions

1. How long did it take from `pip install` to your first `Action` record landing in the database?
2. Which framework are you instrumenting? (LangGraph / CrewAI / other — name it)
3. Was there any step where you were stuck for more than 5 minutes? What was it?
4. In one sentence, what does your pipeline do? (we may want to mention this in the Show HN post if it's compelling)

### Responses

#### Partner 1
- **Time-to-first-record:** _e.g. "8 minutes" / "45 minutes — got stuck on docker-compose"_
- **Framework:** _e.g. LangGraph 0.2.x_
- **Stuck point:** _e.g. "wasn't clear `rootsign-admin init` had to run before the first tool call"_
- **Pipeline (one sentence):** _e.g. "Automated SOC2 evidence collection across our SaaS vendors."_
- **Quote (anonymised, ok to use externally):** _e.g. "Setup took ten minutes and we already had a verifiable audit log."_

#### Partner 2
- Time-to-first-record:
- Framework:
- Stuck point:
- Pipeline:
- Quote:

_(repeat block per partner — add as the cohort fills in)_

### Aggregate observations
> _Founder fill: themes across partners, e.g. "3/5 hit the docker-compose hurdle — fix in README §1"._

---

## Week 10 — First records

Goal: validate the verify CLI and surface PII patterns design partners need.

### Questions

1. Open your DB or run `rootsign verify <session_id>` on your first real session. Did the `VALID ✓` output make sense at a glance?
2. What PII patterns do you need in `RedactionConfig` that aren't covered by `StandardPIIConfig` / `FinancialPIIConfig` / `HealthcarePIIConfig`?
3. Which of your tool calls are you NOT instrumenting yet, and why? (e.g. "the LLM call itself", "the embedding lookup", "the third-party API we don't own")
4. If `rootsign verify` came back TAMPERED, what would your next step be? Walk us through.

### Responses

#### Partner 1
- **VALID output legible:** _Y/N + qualitative comment_
- **PII patterns needed:** _e.g. "internal employee IDs match `EMP\d{6}`"_
- **Not-instrumenting:** _e.g. "we don't wrap the OpenAI call yet because cost — same hash twice doesn't help us"_
- **TAMPERED response plan:** _e.g. "ping me on Slack, I'd rerun verify locally and grep the audit log"_
- **Quote:** _placeholder_

#### Partner 2
- VALID output legible:
- PII patterns needed:
- Not-instrumenting:
- TAMPERED response plan:
- Quote:

### Aggregate observations
> _Founder fill: which `extra_rules` come up repeatedly → candidates for a v0.2.0 built-in config._

---

## Week 11 — Compliance

Goal: pressure-test whether RootSign answers a real compliance question. The pricing-validation question is here on purpose.

### Questions

1. If a compliance officer asked you to prove what your agent did last week, what would you hand them today (with rootsign)?
2. What's missing from the current record set that would make that handoff stronger?
3. Would you pay $299/month for a hosted dashboard that:
   - Showed every action across every session in a queryable UI
   - Let your compliance officer self-serve "show me every action that timed out" / "show me every approval rejection" / "show me every action that touched customer X"
   - Provided exportable reports for SOC2 / HIPAA / GDPR audits
   - (Reasoning: this is the Phase 2 hosted backend.)
4. What's the highest-stakes action your agent takes today that you'd want gated behind a HiTL checkpoint? (helps us prioritise the HiTL UX surface in Phase 2)

### Responses

#### Partner 1
- **What you'd hand them today:** _e.g. "a session URL + the verify output as a screenshot — but they'd want a CSV"_
- **What's missing:** _e.g. "no per-tool-call cost field; no link to the original LLM transcript"_
- **$299 dashboard — would pay?:** _Y/N/Maybe + qualitative_
  - If no/maybe: _what price would you pay, what feature would close the gap?_
- **Highest-stakes action:** _e.g. "auto-refunds over $500"_
- **Quote:** _placeholder_

#### Partner 2
- What you'd hand them today:
- What's missing:
- $299 dashboard:
- Highest-stakes action:
- Quote:

### Aggregate observations
> _Founder fill: pricing distribution; top 3 missing fields; HiTL surface priority list._

---

## Week 12 — Launch

Goal: secure design partner quotes for the Show HN post; close the loop.

### Questions

1. We're posting RootSign to Hacker News this week. Can we quote you?
   - [ ] Yes, name + company are fine
   - [ ] Yes, anonymous ("a Fortune 500 fintech team", "a healthtech CTO")
   - [ ] No, prefer not to be quoted
2. In one sentence, what problem does RootSign solve for you?
3. What's the next thing you'd want from RootSign in the first month after Show HN?
4. Anything you want us to NOT say about your usage publicly?

### Responses

#### Partner 1
- **Quote permission:** _named / anonymous / no_
- **Attribution string (if named):** _e.g. "— Jane Doe, Head of Platform, Acme"_
- **Attribution string (if anonymous):** _e.g. "— Engineering lead at a Series-C fintech"_
- **One-sentence problem solved:** _placeholder_
- **Next thing wanted:** _placeholder_
- **Do-not-say:** _placeholder_

#### Partner 2
- Quote permission:
- Attribution string:
- One-sentence problem solved:
- Next thing wanted:
- Do-not-say:

### Selected quotes for Show HN

> _Founder fill: copy the 1–2 best quotes here in the exact form they'll appear in the post. Confirm attribution string with each partner BEFORE Week 12 Tuesday so the post can ship without delay._

| Slot | Quote | Attribution | Partner approved? |
|---|---|---|---|
| 1 | _"…"_ | _— …_ | [ ] |
| 2 | _"…"_ | _— …_ | [ ] |

---

## Process notes

- **Cadence:** send question sets on Monday of each week; nudge non-responders by Wednesday.
- **Format:** Notion form / Google Doc / email reply — whichever each partner prefers. Don't force a tool.
- **Synthesis:** after each week, write the "Aggregate observations" subsection above. These three paragraphs become the Phase 2 sprint planning input.
- **Privacy:** real names stay in this file (internal). Anything posted externally uses the partner-approved attribution string from Week 12.

## Open items
> _Founder running list — strike through as resolved._

- [ ]
- [ ]
- [ ]
