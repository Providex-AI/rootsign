"""Live 2–3 minute demo: instrument → verify → tamper → export.

Built for talking over. Each beat prints a heading, shows the command, runs
the real CLI, then pauses so you can narrate before the next one.

No Docker, no database, no network, no API key — the JSONL backend has been
the default since v0.2.0, which is the point of the first beat. Everything
lands in a scratch directory that is wiped on each run, so re-running is safe
and your own ~/.rootsign is never touched.

    .venv/bin/python scripts/demo_live.py              # Enter advances each beat
    .venv/bin/python scripts/demo_live.py --auto       # hands-off timed pacing
    .venv/bin/python scripts/demo_live.py --fast       # no pauses, for testing
    .venv/bin/python scripts/demo_live.py --receipts   # + the v0.3.1 beat

Default is Enter-to-advance: the script never gets ahead of you, and a
question mid-demo costs nothing. Script time is ~30s of command execution —
your narration is what fills 2–3 minutes.

If a beat fails it stops immediately with a visible error rather than
limping on — better to find that now than in front of an audience.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENV_BIN = REPO / ".venv" / "bin"
SCRATCH = Path("/tmp/rootsign_demo")
DATA_DIR = SCRATCH / "data"
BUNDLE_DIR = SCRATCH / "bundles"

# Advance mode. STEP is the default on purpose: in front of an audience you
# want to control when the next beat lands — a question mid-demo should not
# race a timer. --auto is the hands-off pace; --fast is for testing.
PACE = 1.0
STEP = True

# ANSI, kept minimal so it reads on any projector.
BOLD, DIM, GREEN, RED, YELLOW, CYAN, RESET = (
    "\033[1m",
    "\033[2m",
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[36m",
    "\033[0m",
)


def pause(seconds: float) -> None:
    if STEP:
        return
    time.sleep(seconds * PACE)


def wait(prompt: str = "next") -> None:
    """Hold until the presenter is ready. No-op outside step mode."""
    if not STEP:
        return
    try:
        input(f"{DIM}    [{prompt}]{RESET}")
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)


def beat(number: int, title: str, subtitle: str = "") -> None:
    print()
    print(f"{BOLD}{CYAN}──  {number}. {title}{RESET}")
    if subtitle:
        print(f"{DIM}    {subtitle}{RESET}")
    print()
    pause(0.8)


def run(argv: list[str], *, echo: str | None = None, expect_code: int | None = 0) -> str:
    """Run a real command, echoing it first the way a demo should."""
    print(f"{BOLD}$ {echo or ' '.join(argv)}{RESET}")
    pause(0.6)
    proc = subprocess.run(argv, cwd=REPO, capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    print(out.rstrip())
    if expect_code is not None and proc.returncode != expect_code:
        print(
            f"\n{RED}DEMO ABORTED{RESET}: expected exit {expect_code}, "
            f"got {proc.returncode} from: {' '.join(argv)}"
        )
        sys.exit(1)
    return out


# ---------------------------------------------------------------------------
# The instrumented agent — written out so beat 1 can show it, then executed.
# ---------------------------------------------------------------------------

AGENT_SOURCE = """
import asyncio, rootsign

rootsign.init(agent="invoice-agent", risk_tier="high")          # 1. once

@rootsign.trace()                                                # 2. per tool
async def lookup_customer(customer_id: str) -> str:
    return f"ACME Corp ({customer_id})"

@rootsign.trace()
async def send_invoice(customer_id: str, amount: float) -> str:
    return f"invoice sent to {customer_id} for ${amount:,.2f}"

@rootsign.trace()
async def log_payment(customer_id: str, amount: float) -> str:
    return f"payment recorded: ${amount:,.2f}"

async def main():
    async with rootsign.session(objective="invoice ACME") as ctx:  # 3. per run
        await lookup_customer("CUST-4417")
        await send_invoice("CUST-4417", 12500.00)
        await log_payment("CUST-4417", 12500.00)
        print(f"SESSION_ID={ctx.session_id}")

asyncio.run(main())
"""


def session_file() -> Path:
    files = sorted((DATA_DIR / "sessions").glob("*.jsonl"))
    if not files:
        print(f"{RED}DEMO ABORTED{RESET}: no session file was written under {DATA_DIR}")
        sys.exit(1)
    return files[-1]


def main() -> None:
    global PACE
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true", help="no pauses at all (for testing)")
    ap.add_argument("--auto", action="store_true", help="timed pacing instead of Enter-to-advance")
    ap.add_argument("--receipts", action="store_true", help="include the v0.3.1 sync-receipt beat")
    args = ap.parse_args()
    global STEP
    if args.fast:
        STEP, PACE = False, 0.0
    elif args.auto:
        STEP, PACE = False, 2.0

    shutil.rmtree(SCRATCH, ignore_errors=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    env_note = f"ROOTSIGN_DATA_DIR={DATA_DIR}"
    os.environ["ROOTSIGN_BACKEND"] = "jsonl"
    os.environ["ROOTSIGN_DATA_DIR"] = str(DATA_DIR)

    print()
    print(f"{BOLD}RootSign — tamper-evident provenance for AI agents{RESET}")
    print(f"{DIM}pip install rootsign · no Docker · no database · no API key{RESET}")
    pause(1.5)

    # -- 1 -------------------------------------------------------------------
    beat(1, "Instrument the agent", "Three lines. That is the whole integration.")
    for line in AGENT_SOURCE.strip().splitlines():
        print(f"  {line}")
    pause(4.0)
    wait("run it")

    # -- 2 -------------------------------------------------------------------
    beat(2, "Run it", "Every tool call is recorded as it happens.")
    agent_py = SCRATCH / "agent.py"
    agent_py.write_text(AGENT_SOURCE)
    out = run([str(VENV_BIN / "python"), str(agent_py)], echo=f"{env_note} python agent.py")
    session_id = next(
        (ln.split("=", 1)[1].strip() for ln in out.splitlines() if ln.startswith("SESSION_ID=")),
        "",
    )
    path = session_file()
    records = len([ln for ln in path.read_text().splitlines() if ln.strip()])
    print()
    print(f"  {GREEN}{records} records{RESET} written to {DIM}{path}{RESET}")
    pause(2.5)
    wait("verify")

    # -- 3 -------------------------------------------------------------------
    beat(3, "Verify the chain", "Each record's hash is recomputed and re-linked.")
    run([str(VENV_BIN / "rootsign"), "verify", "--local", str(path)])
    pause(2.5)
    wait("export")

    # -- 4 -------------------------------------------------------------------
    beat(4, "Export the evidence bundle", "What you hand an auditor.")
    BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
    run(
        [str(VENV_BIN / "rootsign"), "export", "--local", str(path), "--out", str(BUNDLE_DIR)],
        echo=f"rootsign export --local <session>.jsonl --out {BUNDLE_DIR}",
        expect_code=None,
    )
    bundle = next(iter(sorted(BUNDLE_DIR.glob("evidence-*"))), None)
    if bundle:
        manifest = json.loads((bundle / "manifest.json").read_text())
        print()
        print(f"  verdict recorded in the bundle: {BOLD}{manifest['verdict']}{RESET}")
        print(f"  {DIM}every file hashed, so the bundle proves its own integrity{RESET}")
    pause(3.0)
    wait("now tamper with it")

    # -- 5 -------------------------------------------------------------------
    beat(
        5,
        "Now tamper with it",
        "Edit one stored record — what an insider or a compromised process would do.",
    )
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    target = next(
        i for i, ln in enumerate(lines) if json.loads(ln).get("event_type") == "ACTION_RECORD"
    )
    record = json.loads(lines[target])
    before = record.get("tool_name")
    record["tool_name"] = "transfer_funds"
    lines[target] = json.dumps(record)
    path.write_text("\n".join(lines) + "\n")
    # Deliberately no record number here: `verify` numbers by action sequence,
    # and a second numbering in the narration only invites a question.
    print(f"  the first recorded action: {DIM}{before}{RESET} → {YELLOW}transfer_funds{RESET}")
    print(f"  {DIM}the file still parses; the record still looks complete{RESET}")
    pause(2.5)
    wait("re-verify")
    print()
    run([str(VENV_BIN / "rootsign"), "verify", "--local", str(path)], expect_code=1)
    print()
    print(f"  {RED}Caught.{RESET} The chain names the exact record that changed.")
    print(f"  {DIM}exit code 1 — this is CI-gateable{RESET}")
    pause(3.0)
    wait("wrap up")

    # -- 6 (optional) --------------------------------------------------------
    if args.receipts:
        beat(
            6,
            "v0.3.1 — sync receipts",
            "When records are uploaded, the outcome of handling them is recorded too.",
        )
        print("  A rejected upload used to live only in terminal scrollback.")
        print("  Now every replay attempt leaves a hash-bound record:")
        print()
        print(f"    {DIM}spool/receipts/<session_id>.sync.json{RESET}")
        print("      accepted / duplicate / rejected / not_attempted")
        print("      + the SHA-256 of the exact bytes replayed")
        print()
        print(f"  {DIM}INCOMPLETE = a record is missing. TAMPERED = one was altered.{RESET}")
        print(f"  {DIM}The receipt adds the third: observed, and refused.{RESET}")
        pause(3.0)

    # -- close ---------------------------------------------------------------
    print()
    print(f"{BOLD}{GREEN}Recorded. Verified. Tamper-evident. Exportable.{RESET}")
    print(f"{DIM}  session {session_id}{RESET}")
    print(f"{DIM}  scratch dir {SCRATCH} — delete it whenever{RESET}")
    print()


if __name__ == "__main__":
    main()
