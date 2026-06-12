# Show HN — RootSign: tamper-evident audit trail for AI agents (Apache 2.0)

> **Status:** Draft. Founder fills the placeholder slots in Week 12 before posting.
> **Hard gate before publish:** `pytest tests/integration/test_show_hn_quickstart.py -v`
> passes in a clean venv with no manual setup beyond `docker-compose up -d db`
> (Sprint 4 DoD §5.4 item 12).

---

## Title

```
Show HN: RootSign — tamper-evident audit trail for AI agents (Apache 2.0)
```

> _Founder fill: HN title is capped at ~80 chars and the "Show HN: " prefix is mandatory. Above is 65 chars — fine. Alternates to A/B test mentally:_
> - _"Show HN: Cryptographic audit logs for LangGraph / CrewAI agents"_
> - _"Show HN: RootSign — `rootsign verify` for your agent's tool calls"_

---

## Body

```text
We kept running into the same problem instrumenting LangGraph pipelines:
when something went wrong, there was no way to prove what the agent did,
in what order, or whether the log had been modified.

RootSign adds a cryptographic hash chain to every tool call.
If any record is modified after the fact, `rootsign verify` detects it.

  pip install rootsign[langgraph]

  # Instrument with one line
  tools = rootsign.wrap_tools([send_invoice, log_payment], ctx=ctx)

  # Verify the chain
  $ rootsign verify 660e8400-...
  VALID ✓  —  47 records, chain intact

What it does:
- SHA-256 hash chain across every Action record in a session
- Human-in-the-loop checkpoints with Approval records
- PII redacted before hashing (StandardPIIConfig out of the box)
- Works with LangGraph and CrewAI — AutoGen coming soon
- Local first (Postgres + Timescale) — no cloud dependency

What it doesn't do (yet): compliance dashboard, cloud backend, policy
engine — all on the roadmap. Phase 1 is the SDK only.

github.com/Providex-AI/rootsign | Apache 2.0
```

> _Founder fill: the `47 records` line is a placeholder count — pick a number that matches a real run from your dev machine so a curious reader running the quickstart sees the same shape._

---

## Design partner quote slots

Two slots. Both populated from `docs/design-partner-feedback.md` Week 12.
Drop into the post body AFTER the github.com line, before any comments
land. If a third partner gives an excellent quote, hold it for the first
comment thread reply rather than expanding the OP — HN readers reward
short OPs.

### Quote 1 — Lead quote (most concrete outcome)

> **Source:** _Partner __ — design-partner-feedback.md Week 12 row__
>
> _"…"_
>
> — _attribution string_

Selection criteria: should name a specific compliance/audit outcome the
reader can map onto their own situation. "We replaced a manual SOC2
evidence collection workflow" beats "great library."

### Quote 2 — Technical quote (developer-experience signal)

> **Source:** _Partner __ — design-partner-feedback.md Week 12 row__
>
> _"…"_
>
> — _attribution string_

Selection criteria: should speak to how fast/easy it was to wire up, or
how rootsign integrates with their existing stack. The "ten minutes to
first valid chain" type of quote.

---

## Pre-publish checklist

- [ ] **GitHub Support has confirmed unreachable-object GC ran on the repo.** After the v0.1.0 launch cleanup we `git filter-repo`'d 10 internal docs out of history and force-pushed; orphaned commits remained accessible by direct SHA URL until Support purged them. Posting Show HN before Support confirms = spiking crawler traffic on URLs that still reach the orphaned content. Verify in an incognito browser that `github.com/Providex-AI/rootsign/commit/2c9a566` returns 404 before un-privating the repo. Memory: `feedback_filter_repo_github_gc`.
- [ ] **Repo flipped back to public** after the GC verification above.


- [ ] `python -m pytest tests/integration/test_show_hn_quickstart.py -v` passes in a clean venv. Exact recipe:
  ```bash
  rm -rf /tmp/rs_clean
  python3.12 -m venv /tmp/rs_clean
  source /tmp/rs_clean/bin/activate
  pip install -e '.[langgraph,crewai,test]'
  python -m pytest tests/integration/test_show_hn_quickstart.py -v
  deactivate && rm -rf /tmp/rs_clean
  ```
  Two non-obvious requirements: **(a)** Python 3.11 or 3.12 (crewai has no 3.13/3.14 wheels yet); **(b)** call pytest via `python -m pytest`, NOT bare `pytest` — Homebrew installs a system `pytest` that wins on PATH inside the venv and uses system Python, which has no rootsign installed.
- [ ] Both quote slots filled or explicitly dropped (don't ship "[Drop quote here]" placeholders).
- [ ] README quickstart manually walked end-to-end on the laptop you're posting from.
- [ ] GitHub repo is **public** and the `v0.1.0` tag is live with release notes.
- [ ] CrewAI 1.x matrix lane is green (CI dashboard).
- [ ] The `pip install rootsign[langgraph]` line in the post body actually installs without a `--pre` flag.
- [ ] Discord link removed if Discord isn't live yet (don't promise channels you can't honour).
- [ ] An open browser tab on the GitHub Issues page, ready to triage what HN surfaces in the first 2 hours.

---

## Comment thread playbook

HN's first 2 hours determine front-page placement. Reply to **every**
substantive comment in that window. Use these answer skeletons (fill in
specifics from the actual question):

### "How is this different from $existing_tool?"

> _Founder fill: name the one or two tools you keep getting compared to (LangSmith / Helicone / OpenTelemetry / OpenLLMetry — pick the live comparisons), name a concrete capability gap, link to a docs page if applicable._

Skeleton:
> RootSign isn't trying to be a tracing/observability product. The differentiator is that the **hash chain is verifiable** — a third party (auditor, your customer's compliance officer) can run `rootsign verify` and prove the log wasn't edited. {comparison_tool} captures the same span data but a malicious or careless DB write can't be detected after the fact.

### "Does this work with $framework?"

> _Founder fill: keep a running list as questions arrive. If $framework is on the Phase 2 list, say so + point at the relevant GitHub issue. If it's not on any roadmap, ask "what's the tool surface look like?" — the duck-typing approach from ADR-005 means many frameworks are 1-day ports._

### "Why a hash chain instead of $alternative (e.g. Merkle tree / git-style content addressing)?"

> _Founder fill: link [docs/adr/ADR-001-hash-canonical-spec.md](adr/ADR-001-hash-canonical-spec.md) and summarise: linear chain is the smallest surface that proves order + non-modification, and verification is one pass O(N). Merkle is the upgrade when partial-proof / random-access verification becomes a requirement (Phase 2 cloud)._

### "Postgres + Timescale feels heavy for a library."

> _Founder fill: point at the local JSONL path (`rootsign verify --local <path.jsonl>`) — no DB needed. The DB is the production deployment shape, not the demo shape._

### "Can I see a video / demo?"

> _Founder fill placeholder. If you record one before posting, drop the link here. If not, say "no video yet — `pytest tests/integration/test_show_hn_quickstart.py -v` reproduces the README quickstart end-to-end."_

### General response posture

- **Acknowledge first, then answer.** "Good question — {answer}" beats "{answer}".
- **Concede gaps openly.** "Yes, no cloud backend yet — Phase 2. Tracking issue: #__." Builds more trust than dodging.
- **Link to code, not docs, when possible.** HN respects a permalink to the actual file more than a docs page.
- **No marketing language.** "Tamper-evident provenance logging" is the maximum acceptable. Never "enterprise-grade", never "AI-native", never "next-generation".

---

## Post-publish follow-up

Within 24 hours of the post:

- [ ] Post in #langchain Discord channel with the HN link + screenshot of the verify output.
- [ ] Post in #crewai Discord channel with the same.
- [ ] File GitHub issues for every concrete bug/feature request surfaced in HN comments. Tag them `from-hn` so we can build a "things HN asked for" board.
- [ ] Reply to every email / Twitter mention. The reply itself is the conversion event.

Within 1 week:

- [ ] Ship one concrete improvement that came directly from a HN comment. Mention the commenter in the release notes. Closes the loop on "we listened."
- [ ] Update the Status table in README with whatever the community most asked about (probably AutoGen).

---

## Open items

> _Founder running list._

- [ ]
- [ ]
- [ ]
