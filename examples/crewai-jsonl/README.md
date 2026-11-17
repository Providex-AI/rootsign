# CrewAI, no database, no API key

A CrewAI crew whose tool calls land on the RootSign hash chain, with nothing
to set up. Records go to a local JSONL file and `rootsign verify --local`
checks them.

```bash
pip install 'rootsign[crewai]'
python crew.py
```

```text
crew: Invoice sent and payment recorded for acme-corp.

2 actions recorded → ~/.rootsign/sessions/<session_id>.jsonl

Verify the chain:
    rootsign verify --local ~/.rootsign/sessions/<session_id>.jsonl
```

Run the command it prints:

```text
VALID ✓  —  2 records, chain intact
```

CrewAI also prints its own progress output, and on the first run it may ask
whether you want to view execution traces. Both come from CrewAI, not
RootSign.

## What is real and what is scripted

The crew is a normal CrewAI crew: an `Agent` with two tools, a `Task`, and
`Crew.kickoff_async()` running CrewAI's own agent loop. That loop is where
RootSign's interception happens, so this exercises the same path as a
production crew.

The LLM is scripted. `ScriptedLLM` replays three replies: call
`send_invoice`, call `log_payment`, then give the final answer. That keeps the
example offline and deterministic. To use a real model, pass your usual `llm=`
to the `Agent` instead. The RootSign lines (`init()`, `session()`,
`wrap_crewai_tools()`) do not change.

## Versions

The example needs CrewAI 0.120 or later, the first release that exports
`BaseLLM`, which the scripted LLM subclasses. RootSign's CrewAI integration
itself is tested from 0.95.x.

CI runs this example on the CrewAI lines in the contract matrix that have
`BaseLLM`: 0.203.x and 1.x (`tests/contract/crewai/test_docs_and_example.py`).
It has also been run on 0.120.1 and 1.6.1.
