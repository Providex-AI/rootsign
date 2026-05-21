"""End-to-end demo of AC-2.6: hash chain detects tampering.

  1. Resets providex_dev to a clean schema
  2. Creates an agent + session + 5 actions via create_with_hash
  3. Runs verify_chain → expects valid=True, record_count=5
  4. Corrupts action #3's self_hash directly via SQL
  5. Runs verify_chain → expects valid=False, first_invalid_sequence=3

Run with: .venv/bin/python scripts/demo_verify_chain.py
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from providex import crud
from providex.config import settings
from providex.models.action import Action
from providex.schemas import (
    ActionAuthorizationStatus,
    ActionCreate,
    AgentCreate,
    AgentEnvironment,
    AgentFramework,
    AgentRiskTier,
    SessionCreate,
    SessionStatus,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _reset_dev_db() -> None:
    print("Resetting providex_dev (init --reset)...")
    subprocess.run(
        [str(REPO_ROOT / ".venv/bin/providex-core"), "init", "--reset"],
        cwd=REPO_ROOT,
        check=True,
        stdout=subprocess.DEVNULL,
    )


async def run() -> None:
    engine = create_async_engine(settings.DATABASE_URL)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        # 1. Seed: agent + session + 5 actions.
        agent = await crud.agent.create(
            db,
            obj_in=AgentCreate(
                name="demo-agent",
                owner="ops",
                environment=AgentEnvironment.DEVELOPMENT,
                risk_tier=AgentRiskTier.LOW,
                framework=AgentFramework.CUSTOM,
            ),
        )
        s = await crud.session.create(
            db,
            obj_in=SessionCreate(agent_id=agent.agent_id, status=SessionStatus.RUNNING),
        )

        print(f"\nAgent:   {agent.agent_id}")
        print(f"Session: {s.session_id}")
        print("\nInserting 5 actions via create_with_hash...")
        for i in range(1, 6):
            a = await crud.action.create_with_hash(
                db,
                obj_in=ActionCreate(
                    session_id=s.session_id,
                    tool_name=f"step_{i}",
                    input_hash="a" * 64,
                    output_hash="b" * 64,
                    authorization_status=ActionAuthorizationStatus.AUTO_AUTHORIZED,
                ),
                session_obj=s,
            )
            print(
                f"  seq={a.sequence_number}  "
                f"prev={(a.prev_action_hash or '<none>')[:12]}...  "
                f"self={a.self_hash[:12]}..."
            )
        await db.commit()

    # 2. Verify — fresh session.
    async with Session() as db:
        print("\n--- verify_chain (clean) ---")
        result = await crud.action.verify_chain(db, session_id=s.session_id)
        print(result)
        assert result["valid"] is True
        assert result["record_count"] == 5

    # 3. Corrupt self_hash on sequence_number=3. 
    async with Session() as db:
        print("\n--- corrupting self_hash on sequence_number=3 ---")
        await db.execute(
            update(Action)
            .where(Action.session_id == s.session_id)
            .where(Action.sequence_number == 3)
            .values(self_hash="0" * 64)
        )
        await db.commit()
        print("  done.")

    # 4. Verify again — should now detect tampering at sequence 3.
    async with Session() as db:
        print("\n--- verify_chain (corrupted) ---")
        result = await crud.action.verify_chain(db, session_id=s.session_id)
        print(result)
        assert result["valid"] is False
        assert result["first_invalid_sequence"] == 3
        print("\n✓ Demo passed: AC-2.6 contract holds end-to-end.")

    await engine.dispose()


if __name__ == "__main__":
    _reset_dev_db()
    asyncio.run(run())
