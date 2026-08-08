"""plan.md §8 recall probe: plant a specific fact early, bury it under enough filler turns to
force rolling-summary compression, then confirm it's still recoverable — proving retrieval
survives compression rather than only working while the fact is still in raw recent history."""
import uuid

import httpx
import pytest

from .harness import API_BASE, create_session, judge_yes_no, send_turn, wait_for_compression

pytestmark = pytest.mark.integration

# Generic, unrelated-to-the-fact filler so this is a fair test of long-range recall through
# compression rather than the fact simply still being in the uncompressed recent-turns window.
# Requesting ~400-word replies (not ~150) is deliberate: POST /chat is rate-limited to 10
# requests/300s per user (Phase 5), so this needs to clear the compression token threshold
# (COMPRESS_TRIGGER=0.7 * RECENT_BUDGET=4000 = 2800 tokens) in few enough turns to stay under
# that cap alongside the fact-plant and final query turns — 7 filler turns keeps the total at 9.
_FILLER_PROMPTS = [
    "In about 400 words, explain how a hash table resolves collisions.",
    "In about 400 words, explain the difference between TCP and UDP.",
    "In about 400 words, explain what a database index is and why it speeds up reads.",
    "In about 400 words, explain the CAP theorem.",
    "In about 400 words, explain how garbage collection works in most managed languages.",
    "In about 400 words, explain what idempotency means for HTTP APIs.",
    "In about 400 words, explain the difference between processes and threads.",
]


def test_recall_probe_survives_compression():
    user_id = f"eval-recall-{uuid.uuid4()}"
    with httpx.Client(base_url=API_BASE, timeout=60) as client:
        session_id = create_session(client, user_id, "Eval: recall probe")

        send_turn(
            client, user_id, session_id,
            "Quick note for later: my API key rotation policy is every 45 days. "
            "Just acknowledge briefly, no need to elaborate.",
        )

        for prompt in _FILLER_PROMPTS:
            send_turn(client, user_id, session_id, prompt)

        mem = wait_for_compression(client, user_id, session_id)
        assert mem.get("summary_version"), (
            "rolling-summary compression never fired within the timeout — either "
            "RECENT_BUDGET is too high for this many filler turns, or the summarizer is broken. "
            f"Last /memory response: {mem}"
        )

        reply = send_turn(
            client, user_id, session_id,
            "Quick reminder — how often am I supposed to rotate my API key?",
        )

        assert judge_yes_no(
            "The correct answer is '45 days'. Does the following reply state that the "
            f"rotation interval is 45 days, in any phrasing? Reply to check: {reply!r}"
        ), f"recall probe FAILED — model replied: {reply!r}"
