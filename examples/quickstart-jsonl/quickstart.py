"""RootSign zero-config quickstart — no Docker, no database, no network.

    pip install rootsign
    python quickstart.py

Everything below `main()` is your ordinary agent code. The RootSign part is
three calls: `init()` once at startup, a `session()` around the run, and
`@rootsign.trace()` on each tool you want on the hash chain.

Records land in `~/.rootsign/sessions/<session_id>.jsonl` (ADR-011). Point
`ROOTSIGN_DATA_DIR` somewhere else if you'd rather keep them with the project.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import rootsign

rootsign.init(agent="quickstart-agent", risk_tier="high")


@rootsign.trace()
async def send_invoice(customer_id: str, amount: float) -> str:
    """Pretend to send an invoice."""
    return f"invoice sent to {customer_id} for ${amount:,.2f}"


@rootsign.trace()
async def log_payment(customer_id: str, amount: float) -> str:
    """Pretend to record a payment."""
    return f"payment of ${amount:,.2f} recorded for {customer_id}"


async def main() -> None:
    async with rootsign.session(objective="invoice ACME and record payment") as ctx:
        print(await send_invoice("acme-corp", 1500.00))
        print(await log_payment("acme-corp", 1500.00))

    data_dir = Path(os.environ.get("ROOTSIGN_DATA_DIR", "~/.rootsign")).expanduser()
    session_file = data_dir / "sessions" / f"{ctx.session_id}.jsonl"

    print(f"\n{ctx.current_sequence} actions recorded → {session_file}")
    print("\nVerify the chain:")
    print(f"    rootsign verify --local {session_file}")


if __name__ == "__main__":
    asyncio.run(main())
