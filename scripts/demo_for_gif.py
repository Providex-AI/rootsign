"""Self-contained happy-path demo, used to record docs/demo.gif via vhs.

No LLM, no API key, no database — the v0.2.0 quickstart exactly:
  1. rootsign.init() once
  2. open a session
  3. call three @rootsign.trace-decorated tools
  4. close the session
  5. write the session file path to /tmp/rs_demo_session for the tape's
     `rootsign verify --local` and `rootsign export --local` steps

Records land under /tmp/rs_demo_data (wiped on each run) so the GIF's record
count is deterministic and the developer's own ~/.rootsign is left alone.

The output is intentionally terse and friendly so the GIF reads cleanly.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

DATA_DIR = Path("/tmp/rs_demo_data")
SESSION_FILE = Path("/tmp/rs_demo_session")
BUNDLE_DIR = Path("/tmp/rs_demo_bundles")

# Set before importing rootsign so init() picks these up.
shutil.rmtree(DATA_DIR, ignore_errors=True)
# Export refuses to overwrite an existing bundle, so a re-record would fail on
# the second take without this.
shutil.rmtree(BUNDLE_DIR, ignore_errors=True)
os.environ["ROOTSIGN_BACKEND"] = "jsonl"
os.environ["ROOTSIGN_DATA_DIR"] = str(DATA_DIR)

import rootsign  # noqa: E402  (must follow the env setup above)

rootsign.init(agent="demo-invoice-agent", risk_tier="high")


@rootsign.trace()
async def send_invoice(customer_id: str, amount: float) -> str:
    """Send an invoice."""
    return f"sent: {customer_id} owes {amount:.2f}"


@rootsign.trace()
async def log_payment(transaction_id: str, amount: float) -> str:
    """Log a payment."""
    return f"logged: tx={transaction_id} amount={amount:.2f}"


@rootsign.trace()
async def notify_customer(customer_id: str, message: str) -> str:
    """Notify a customer."""
    return f"notified: {customer_id}"


async def main() -> None:
    async with rootsign.session(objective="invoice acme and confirm payment") as ctx:
        print(f"session opened: {ctx.session_id}")

        await send_invoice("acme", 1500.00)
        print("  action 1: send_invoice    → recorded")

        await log_payment("tx_001", 1500.00)
        print("  action 2: log_payment     → recorded")

        await notify_customer("acme", "Invoice sent")
        print("  action 3: notify_customer → recorded")

    SESSION_FILE.write_text(str(DATA_DIR / "sessions" / f"{ctx.session_id}.jsonl"))
    print("session closed. 3 actions on the hash chain.")
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
