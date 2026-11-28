"""Generate the session-seal golden vectors in tests/fixtures/session-seal/v1/.

The vectors pin `rootsign.seal.compute_seal` (ADR-016) the way
tests/fixtures/hash_vectors.json pins the action hash (ADR-001). Commit the
output and treat it as frozen: a change to it is a change to the seal
specification, which needs a new ADR.

Run with: .venv/bin/python scripts/generate_seal_vectors.py
"""

from __future__ import annotations

import json
from pathlib import Path

from rootsign.seal import SessionFacts, compute_seal

OUT = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "session-seal"
    / "v1"
    / "vectors.json"
)

SESSION = "6f1d0c62-2c4b-4c8e-9a51-0d6c7f3b2a10"
AGENT = "1b7e9f40-5a8d-4e2b-b3c1-9e0f6d4a8c27"
ACTION_1 = "a1000000-0000-4000-8000-000000000001"
ACTION_2 = "a1000000-0000-4000-8000-000000000002"
DECISION_1 = "d1000000-0000-4000-8000-000000000001"
APPROVAL_1 = "e1000000-0000-4000-8000-000000000001"
HASH_1 = "3745dbde0686b4fb8fb8c37b254990ead28001ca63ecfcfa804473307a2ef61a"
HASH_2 = "2bbb2d12194daa3ace9f7e9ced7974e534100990e001bae43a372c5c8fc7367b"


def _action(action_id: str, sequence: int, self_hash: str, **extra: object) -> dict:
    return {
        "action_id": action_id,
        "sequence_number": sequence,
        "authorization_status": "auto_authorized",
        "decision_id": None,
        "self_hash": self_hash,
        **extra,
    }


CASES: list[dict] = [
    {
        "description": "a session with no actions and no side records",
        "facts": {
            "session_id": SESSION,
            "agent_id": AGENT,
            "objective": "nothing to do",
            "user_id": None,
            "status": "completed",
        },
    },
    {
        "description": "two actions; the first follows a decision",
        "facts": {
            "session_id": SESSION,
            "agent_id": AGENT,
            "objective": "invoice ACME",
            "user_id": "finance-bot",
            "status": "completed",
            "decisions": [
                {
                    "decision_id": DECISION_1,
                    "selected_action": "send_invoice",
                    "confidence": 0.97,
                    "reasoning_depth": "summary",
                    "reasoning_summary": "Amount within policy; recipient verified.",
                    "alternatives_considered": [],
                    "inputs_summary": None,
                }
            ],
            "actions": [
                _action(ACTION_1, 1, HASH_1, decision_id=DECISION_1),
                _action(ACTION_2, 2, HASH_2),
            ],
        },
    },
    {
        "description": "a held call a person rejected, with the context they were shown",
        "facts": {
            "session_id": SESSION,
            "agent_id": AGENT,
            "objective": "pay supplier",
            "user_id": None,
            "status": "completed",
            "approvals": [
                {
                    "approval_id": APPROVAL_1,
                    "action_id": ACTION_1,
                    "decision": "rejected",
                    "decision_reason": "Above the unattended mandate",
                    "approver_id": "cli:operator",
                    "approver_type": "human",
                    "context_presented": {
                        "tool_name": "wire_transfer",
                        "input": {"account": "[REDACTED]", "amount": 250000.0},
                    },
                    "response_latency_ms": 4210,
                    "parent_approval_id": None,
                },
                # A second held call, approved. Approval ids run opposite to
                # their actions': approvals sort by approval_id.
                {
                    "approval_id": "e1000000-0000-4000-8000-000000000000",
                    "action_id": ACTION_2,
                    "decision": "approved",
                    "decision_reason": "Verified with the supplier by phone",
                    "approver_id": "cli:operator",
                    "approver_type": "human",
                    "context_presented": {"tool_name": "wire_transfer"},
                    "response_latency_ms": 980,
                    "parent_approval_id": None,
                },
            ],
            "actions": [
                _action(ACTION_1, 1, HASH_1, authorization_status="human_rejected"),
                _action(ACTION_2, 2, HASH_2, authorization_status="human_approved"),
            ],
        },
    },
    {
        "description": "the SDK counted more actions than the writer holds",
        # Ids in the opposite order to the sequence numbers: actions sort by
        # sequence_number, which this vector tells apart from sorting by id.
        "facts": {
            "session_id": SESSION,
            "agent_id": AGENT,
            "objective": "batch run",
            "user_id": None,
            "status": "failed",
            "actions": [_action(ACTION_2, 1, HASH_1), _action(ACTION_1, 2, HASH_2)],
        },
        "final_sequence": 3,
    },
    {
        "description": "non-ASCII objective and the float forms a second implementation must match",
        "facts": {
            "session_id": SESSION,
            "agent_id": AGENT,
            "objective": "Rechnung für ACME, Q3 ✓",
            "user_id": None,
            "status": "completed",
            "decisions": [
                # Ids in the opposite order to selected_action: decisions sort
                # by decision_id.
                {
                    "decision_id": "d1000000-0000-4000-8000-000000000002",
                    "selected_action": "hold",
                    "confidence": 1.0,
                    "reasoning_depth": "full",
                    "reasoning_summary": "Zahlung zurückgestellt",
                    "alternatives_considered": ["pay", "escalate"],
                    "inputs_summary": "amount 1e-05 below the noise floor",
                },
                {
                    "decision_id": DECISION_1,
                    "selected_action": "pay",
                    "confidence": 1e-05,
                    "reasoning_depth": "minimal",
                    "reasoning_summary": None,
                    "alternatives_considered": None,
                    "inputs_summary": None,
                },
            ],
        },
    },
]


def main() -> None:
    vectors = []
    for case in CASES:
        facts = case["facts"]
        seal = compute_seal(
            SessionFacts(
                session_id=facts["session_id"],
                agent_id=facts["agent_id"],
                objective=facts["objective"],
                user_id=facts["user_id"],
                status=facts["status"],
                decisions=tuple(facts.get("decisions", ())),
                approvals=tuple(facts.get("approvals", ())),
                actions=tuple(facts.get("actions", ())),
            ),
            final_sequence=case.get("final_sequence"),
        )
        vectors.append({**case, "expected": seal})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps({"seal_version": 1, "vectors": vectors}, indent=2, ensure_ascii=False) + "\n"
    )
    print(f"wrote {len(vectors)} vectors to {OUT}")


if __name__ == "__main__":
    main()
