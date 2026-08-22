# Quickstart — zero config, no database

The smallest complete RootSign app. **No Docker, no PostgreSQL, no API key, no
network.** Records go to an append-only JSONL file on your laptop and
`rootsign verify --local` proves the chain is intact.

```bash
pip install rootsign
python quickstart.py
```

```text
invoice sent to acme-corp for $1,500.00
payment of $1,500.00 recorded for acme-corp

2 actions recorded → ~/.rootsign/sessions/f758a636-….jsonl

Verify the chain:
    rootsign verify --local ~/.rootsign/sessions/f758a636-….jsonl

Bundle it for someone who doesn't read JSONL:
    rootsign export --local ~/.rootsign/sessions/f758a636-….jsonl
```

Run the command it prints:

```text
VALID ✓  —  2 records, chain intact
  Session:  f758a636-7bcd-4f96-8940-eff7d80e760a
```

## Prove it's tamper-evident

Open the session file, change any character inside any record — an amount, a
hash, a timestamp — and verify again:

```text
TAMPERED ✗  —  chain broken at record #1
  Detail:   self_hash mismatch
  Session:  f758a636-7bcd-4f96-8940-eff7d80e760a
WARNING: This session log may have been tampered with.
```

Exit code is `0` for VALID, `1` for TAMPERED (a record was altered) and `2` for
INCOMPLETE (a record is missing), so this drops straight into CI or a cron
audit — and a script can tell an edit from a gap.

## Hand it to someone who doesn't read JSONL

`verify` answers a developer's question. `export` answers everyone else's:

```bash
rootsign export --local ~/.rootsign/sessions/f758a636-….jsonl
```

```text
VALID — 2 records, chain intact
  Bundle:   ./evidence-f758a636-7bcd-4f96-8940-eff7d80e760a
            redaction.json
            report.html
            report.md
            timeline.json
            verification.json
            manifest.json

  manifest.json SHA-256:  a1d61338d0f793ea03ac472863fadea5660e1e94846913ce7613d8c117d67cc5
  Record that hash outside the bundle — a ticket, an email, a chain-of-custody log.
  It is what proves a bundle you receive later is the one that was generated.
```

Open `report.html` in a browser: the verdict first, then the chain, then every
field of every event. The JSON files next to it are the same facts in
machine-readable form — the report is rendered from them, so it cannot claim
anything they do not contain.

Note the hash it prints and keep it somewhere the bundle isn't. Anyone who
receives the directory can run `rootsign export --check <dir>`, which re-hashes
every file **and** prints that same digest. Re-hashing alone only proves the
bundle is internally consistent; comparing the manifest hash against the value
you recorded is what proves it is the bundle you generated.

Sending it outside the company? `--redact-previews` withholds the stored
payload content — previews, the context a human saw at an approval, captured
decision summaries — and names what it withheld. The hashes and the verdict are
unaffected, so the chain still verifies.

## What the three RootSign calls do

| Call | When | What it does |
|---|---|---|
| `rootsign.init(...)` | Once, at startup | Validates config and stores it. No I/O — safe at module scope and inside a running event loop. |
| `async with rootsign.session(...)` | Around a run | Get-or-creates the agent on first entry, emits SESSION_OPEN/CLOSE, and publishes the context so tools find it implicitly. |
| `@rootsign.trace()` | On each tool | Emits one hash-chained `ACTION_RECORD` per call. |

Nothing else is required — no `SessionContext`, no ingest client, no `ctx=` /
`client=` plumbing. Those remain public for tests and multi-agent processes;
see "Advanced" in the top-level [README](../../README.md).

## Where the data lives

`~/.rootsign/sessions/<session_id>.jsonl`, one JSON object per record.
`~/.rootsign/agents.jsonl` holds the agent registry. Both are plain text —
`cat` them.

Override with `ROOTSIGN_DATA_DIR`:

```bash
ROOTSIGN_DATA_DIR=./audit python quickstart.py
```

## When to graduate to Postgres

The JSONL backend is a single-process writer with no cross-process approval
flow. Switch to `ROOTSIGN_BACKEND=postgres` when you need multiple writer
processes, cross-process human-in-the-loop (`rootsign approve` from another
terminal), or the Phase 2 hosted dashboard. See the graduation table in the
top-level [README](../../README.md) — the code above does not change.
