"""Shared helpers for the eval suite (plan.md §8): recall/update/irrelevance probes and
cost-latency tracking. These hit a *live* backend over real HTTP (not an in-process ASGI
transport) — the thing under test is the actual async background-task timing (summarizer,
fact extraction firing after the response is already sent), which only a real client/server
round trip exercises honestly. Requires `uvicorn backend.main:app` already running on
EVAL_API_BASE and a real OPENAI_API_KEY in backend/.env — see the `integration` marker in
pytest.ini.
"""
import asyncio
import os
import time

import httpx

from backend.llm import complete_utility

API_BASE = os.environ.get("EVAL_API_BASE", "http://localhost:8000")


def create_session(client: httpx.Client, user_id: str, title: str) -> str:
    r = client.post("/sessions", json={"title": title}, headers={"X-User-Id": user_id})
    r.raise_for_status()
    return r.json()["id"]


def send_turn(client: httpx.Client, user_id: str, session_id: str, message: str) -> str:
    """POSTs one chat turn and consumes the SSE stream synchronously, returning the full
    assistant reply. Mirrors app.js's streamAssistantReply() frame parsing (blank-line-
    separated frames, `event: `/`data: ` lines) since this is exercising the same endpoint."""
    full_text = ""
    frame_lines: list[str] = []

    def handle_frame(lines: list[str]) -> None:
        nonlocal full_text
        event = "message"
        data = ""
        for line in lines:
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: "):
                data += line[len("data: "):]
        if event == "error":
            raise RuntimeError(f"chat stream error for session {session_id}: {data}")
        if event == "message":
            full_text += data

    with client.stream(
        "POST",
        f"/sessions/{session_id}/chat",
        json={"message": message},
        headers={"X-User-Id": user_id},
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if line == "":
                if frame_lines:
                    handle_frame(frame_lines)
                    frame_lines = []
            else:
                frame_lines.append(line)
        if frame_lines:
            handle_frame(frame_lines)

    if not full_text:
        raise RuntimeError(f"chat turn for session {session_id} produced no text")
    return full_text


def get_memory(client: httpx.Client, user_id: str, session_id: str) -> dict:
    r = client.get(f"/sessions/{session_id}/memory", headers={"X-User-Id": user_id})
    r.raise_for_status()
    return r.json()


def wait_for_compression(
    client: httpx.Client, user_id: str, session_id: str, timeout: float = 60, interval: float = 2
) -> dict:
    """Rolling-summary compression runs as a fire-and-forget background task after the chat
    response is already sent (see backend_progress.md's Phase 3 entry) — polls /memory rather
    than assuming it's done by the time send_turn() returns, same reasoning as the frontend's
    pollForTitleUpdate()."""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = get_memory(client, user_id, session_id)
        if last.get("summary_version"):
            return last
        time.sleep(interval)
    return last


def judge_yes_no(question: str) -> bool:
    """Cheap-model-as-judge, per plan.md §8. Reuses complete_utility() (gpt-4o-mini) rather
    than a second OpenAI client — same model this project already uses for summarization/fact
    extraction, so the eval suite's grading cost stays in the "cheap" tier plan.md intends."""
    system = "Answer strictly with a single word: YES or NO. No explanation, no punctuation."
    answer = asyncio.run(complete_utility(system, question))
    return answer.strip().upper().startswith("Y")
