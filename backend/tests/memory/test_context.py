from dataclasses import dataclass

from backend.memory.context import fit_to_budget, render_system_prompt
from backend.memory.tokens import estimate_tokens


@dataclass
class FakeMessage:
    """Stand-in for models.Message — fit_to_budget only touches token_count."""

    token_count: int
    label: str = ""


@dataclass
class FakeFact:
    content: str


@dataclass
class FakeSummary:
    content: str


def test_estimate_tokens_uses_chars_over_four():
    assert estimate_tokens("a" * 40) == 10


def test_estimate_tokens_never_returns_zero_for_nonempty_text():
    assert estimate_tokens("hi") == 1


def test_fit_to_budget_keeps_everything_under_budget():
    messages = [FakeMessage(token_count=10, label=str(i)) for i in range(5)]
    kept, tokens_used, dropped = fit_to_budget(messages, budget=1000)
    assert kept == messages
    assert tokens_used == 50
    assert dropped == 0


def test_fit_to_budget_drops_oldest_first_and_keeps_chronological_order():
    # Oldest (label "0") through newest (label "4"), 10 tokens each, budget for 3.
    messages = [FakeMessage(token_count=10, label=str(i)) for i in range(5)]
    kept, tokens_used, dropped = fit_to_budget(messages, budget=30)
    assert [m.label for m in kept] == ["2", "3", "4"]
    assert tokens_used == 30
    assert dropped == 2


def test_fit_to_budget_empty_input():
    kept, tokens_used, dropped = fit_to_budget([], budget=1000)
    assert kept == []
    assert tokens_used == 0
    assert dropped == 0


def test_render_system_prompt_persona_only():
    prompt = render_system_prompt("persona text", facts=[], summary=None)
    assert prompt == "persona text"


def test_render_system_prompt_includes_summary_when_present():
    prompt = render_system_prompt("persona", facts=[], summary=FakeSummary(content="prior discussion"))
    assert "prior discussion" in prompt
    assert prompt.startswith("persona")


def test_render_system_prompt_includes_facts_as_bullet_list():
    facts = [FakeFact(content="prefers concise answers"), FakeFact(content="works in fintech")]
    prompt = render_system_prompt("persona", facts=facts, summary=None)
    assert "- prefers concise answers" in prompt
    assert "- works in fintech" in prompt


def test_render_system_prompt_omits_absent_sections():
    prompt = render_system_prompt("persona", facts=[], summary=None)
    assert "Conversation summary" not in prompt
    assert "Known about the user" not in prompt
