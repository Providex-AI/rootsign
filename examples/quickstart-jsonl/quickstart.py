"""RootSign zero-config quickstart — no Docker, no database, no network.

    pip install rootsign
    python quickstart.py

Everything below `main()` is your ordinary agent code. The RootSign part is
three calls: `init()` once at startup, a `session()` around the run, and
`@rootsign.trace()` on each tool you want on the hash chain.

Records land in `~/.rootsign/sessions/<session_id>.jsonl` (ADR-011). Point
`ROOTSIGN_DATA_DIR` somewhere else if you'd rather keep them with the project.

The script finishes by printing the two commands worth knowing:
`rootsign verify --local ...` proves the chain is intact, and
`rootsign export --local ...` turns it into an evidence bundle — a directory
with an HTML report a compliance officer can read and a SHA-256 per file.
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

    # Which commands to print depends on where the records actually went. A
    # `.env` or an exported ROOTSIGN_BACKEND can point this run at Postgres,
    # and printing a JSONL path in that case sends the reader after a file that
    # does not exist — with two commands that then fail.
    from rootsign.sdk.config import SDKSettings

    if SDKSettings().BACKEND == "jsonl":
        data_dir = Path(os.environ.get("ROOTSIGN_DATA_DIR", "~/.rootsign")).expanduser()
        target = data_dir / "sessions" / f"{ctx.session_id}.jsonl"
        verify, export = f"--local {target}", f"--local {target}"
        print(f"\n{ctx.current_sequence} actions recorded → {target}")
    else:
        target = f"the {SDKSettings().BACKEND} backend"
        verify = export = str(ctx.session_id)
        print(f"\n{ctx.current_sequence} actions recorded → {target}")

    print("\nVerify the chain:")
    print(f"    rootsign verify {verify}")
    # The last step is the one that leaves engineering: an evidence bundle is
    # what you hand to someone who will never open a JSONL file.
    print("\nBundle it for someone who doesn't read JSONL:")
    print(f"    rootsign export {export}")


if __name__ == "__main__":
    asyncio.run(main())
