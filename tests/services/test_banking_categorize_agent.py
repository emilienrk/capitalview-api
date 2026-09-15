"""
AI categorisation (services/ai/agents/categorize_agent.py), behind a fake
provider: no network call is ever made.
"""
import asyncio
import json
from typing import Any, Callable

import pytest
from sqlmodel import Session

from dtos.banking import CategoryNature, CategoryOrigin, RuleSource
from services.ai.agents.categorize_agent import CategorizeAgent, run_ai_categorization
from services.ai.providers.base import AIProvider, ModelCapability
from services.banking.categories import create_category, load_categories, load_rules
from services.banking.flows import assign_category, uncategorized_groups
from tests.services.test_banking_category_filing import _month, _ops
from tests.services.test_banking_flows import USER

CURRENT = "current"
MERCHANTS = ("CARREFOUR", "PHARMACIE", "SNCF", "NETFLIX", "BOULANGERIE")


class FakeProvider(AIProvider):
    """Answers with whatever `answer` builds from the request payload."""

    def __init__(self, answer: Callable[[dict], Any]):
        self.answer = answer
        self.calls: list[dict] = []

    def capabilities(self) -> ModelCapability:
        return ModelCapability.TEXT

    async def _send_message(self, messages, tools=None, system=None, output_config=None):
        payload = json.loads(messages[0]["content"])
        self.calls.append({"payload": payload, "system": system, "output_config": output_config})
        answer = self.answer(payload)
        return answer if isinstance(answer, str) else json.dumps(answer)

    def extract_text(self, response):
        return response

    def extract_tool_uses(self, response):
        return []

    def extract_stop_reason(self, response):
        return "end_turn"

    def build_assistant_message(self, response):
        return {"role": "assistant", "content": response}

    def build_tool_result_block(self, tool_use_id, content):
        return {}

    def format_tools(self, tools):
        return tools


def _history(session: Session, master_key: str, merchants=MERCHANTS) -> None:
    _ops(session, master_key, *[
        (CURRENT, f"2026-03-{n + 1:02d}", f"{(len(merchants) - n) * 10}.00", "DBIT", f"CARTE {n + 1:02d}/03/26 {m} CB*08")
        for n, m in enumerate(merchants)
    ])


def _by_label(payload: dict, answers: dict[str, dict]) -> dict:
    """Answer per group, keyed by a merchant word of its label."""
    groups = []
    for group in payload["groups"]:
        for word, answer in answers.items():
            if word in group["label"]:
                groups.append({"group_id": group["group_id"], **answer})
    return {"groups": groups}


def _run(session: Session, master_key: str, provider: FakeProvider, skip: int = 0):
    return asyncio.run(run_ai_categorization(session, USER, master_key, CategorizeAgent(provider), skip))


def _filed(session: Session, master_key: str) -> dict[str, str | None]:
    return {label.split()[2]: tx.category_name for label, tx in _month(session, master_key).items()}


def test_valid_answers_become_ai_rules_and_categories(session: Session, master_key: str):
    _history(session, master_key)
    provider = FakeProvider(lambda payload: _by_label(payload, {
        "CARREFOUR": {"category_name": "Courses", "nature": "EXPENSE", "confidence": 0.9},
        "BOULANGERIE": {"category_name": "Courses", "nature": "EXPENSE", "confidence": 0.8},
        "SNCF": {"category_name": "Transport", "nature": "EXPENSE", "confidence": 0.95},
    }))

    result = _run(session, master_key, provider)

    assert (result.processed, result.rules_created, result.categories_created) == (5, 3, 2)
    assert _filed(session, master_key) == {
        "CARREFOUR": "Courses", "PHARMACIE": None, "SNCF": "Transport", "NETFLIX": None, "BOULANGERIE": "Courses",
    }
    assert {c.origin for c in load_categories(session, USER, master_key).values()} == {CategoryOrigin.AI}
    assert {r.source for r in load_rules(session, USER, master_key)} == {RuleSource.AI}
    assert (result.skip, result.remaining) == (2, 0)


def test_the_model_sees_each_group_and_the_user_s_categories(session: Session, master_key: str):
    _history(session, master_key, ("CARREFOUR",))
    create_category(session, USER, master_key, "Courses", CategoryNature.EXPENSE, CategoryOrigin.BANK)
    provider = FakeProvider(lambda payload: {"groups": []})

    _run(session, master_key, provider)

    [call] = provider.calls
    assert call["payload"] == {
        "categories": [{"name": "Courses", "nature": "EXPENSE"}],
        "groups": [{
            "group_id": "g1", "label": "CARTE 01/03/26 CARREFOUR CB*08", "direction": "debit",
            "count": 1, "median_amount": "10 EUR",
        }],
    }
    assert call["output_config"]["format"]["type"] == "json_schema"


def test_hallucinated_answers_are_ignored(session: Session, master_key: str):
    _history(session, master_key)
    provider = FakeProvider(lambda payload: {"groups": [
        {"group_id": "g999", "category_name": "Fantôme", "nature": "EXPENSE", "confidence": 1},
        {"group_id": "g1", "category_name": "Courses", "nature": "LUXURY", "confidence": 1},
        {"group_id": "g2", "category_name": None, "nature": "EXPENSE", "confidence": 0.9},
        {"group_id": "g3", "category_name": "Transport", "nature": "EXPENSE", "confidence": 0.59},
        {"group_id": "g4", "category_name": "   ", "nature": "EXPENSE", "confidence": 0.9},
    ]})

    result = _run(session, master_key, provider)

    assert (result.rules_created, result.categories_created) == (0, 0)
    assert load_categories(session, USER, master_key) == {}
    assert (result.skip, result.remaining) == (5, 0)


@pytest.mark.parametrize("answer", ["", "not json", "[]", json.dumps({"groups": "nope"})])
def test_an_empty_or_malformed_answer_files_nothing(session: Session, master_key: str, answer: str):
    _history(session, master_key)
    result = _run(session, master_key, FakeProvider(lambda payload: answer))
    assert (result.processed, result.rules_created, result.skip) == (5, 0, 5)


def test_a_name_close_to_an_existing_category_reuses_it(session: Session, master_key: str):
    _history(session, master_key)
    epargne = create_category(session, USER, master_key, "Épargne", CategoryNature.SAVING, CategoryOrigin.BANK)
    provider = FakeProvider(lambda payload: _by_label(payload, {
        "CARREFOUR": {"category_name": "EPARGNE", "nature": "EXPENSE", "confidence": 0.9},
    }))

    result = _run(session, master_key, provider)

    assert (result.rules_created, result.categories_created) == (1, 0)
    [rule] = load_rules(session, USER, master_key)
    assert rule.category_uuid == epargne.uuid
    assert load_categories(session, USER, master_key)[epargne.uuid].nature is CategoryNature.SAVING


def test_at_most_fifteen_new_categories_per_call(session: Session, master_key: str):
    merchants = tuple(f"MARCHAND{chr(65 + n)}" for n in range(20))
    _history(session, master_key, merchants)
    provider = FakeProvider(lambda payload: {"groups": [
        {"group_id": g["group_id"], "category_name": f"Catégorie {g['group_id']}", "nature": "EXPENSE", "confidence": 0.9}
        for g in payload["groups"]
    ]})

    result = _run(session, master_key, provider)

    assert (result.categories_created, result.rules_created, result.skip) == (15, 15, 5)


def test_remaining_decreases_until_the_queue_is_done(session: Session, master_key: str, monkeypatch):
    import services.ai.agents.categorize_agent as module
    monkeypatch.setattr(module, "BATCH_SIZE", 2)
    _history(session, master_key)
    # Files every other group it sees, whatever the batch.
    seen: list[str] = []

    def answer(payload):
        groups = []
        for group in payload["groups"]:
            seen.append(group["label"])
            if len(seen) % 2:
                groups.append({"group_id": group["group_id"], "category_name": "Divers", "nature": "EXPENSE", "confidence": 0.9})
        return {"groups": groups}

    provider = FakeProvider(answer)
    skip, remaining_seen = 0, []
    for _ in range(10):
        result = _run(session, master_key, provider, skip)
        skip = result.skip
        remaining_seen.append(result.remaining)
        if result.remaining == 0:
            break

    assert remaining_seen == sorted(remaining_seen, reverse=True) and remaining_seen[-1] == 0
    assert len(seen) == len(set(seen)) == 5
    assert len(uncategorized_groups(session, USER, master_key).groups) == skip


def test_a_user_s_rule_is_never_overwritten(session: Session, master_key: str):
    _history(session, master_key)
    mine = create_category(session, USER, master_key, "Supermarché", CategoryNature.EXPENSE, CategoryOrigin.BANK)
    carrefour = _month(session, master_key)["CARTE 01/03/26 CARREFOUR CB*08"]
    assign_category(session, USER, master_key, carrefour.id, mine.uuid, True, ["carrefour"])
    provider = FakeProvider(lambda payload: {"groups": [
        {"group_id": g["group_id"], "category_name": "Courses", "nature": "EXPENSE", "confidence": 0.9}
        for g in payload["groups"]
    ]})

    _run(session, master_key, provider)

    assert "CARREFOUR" not in json.dumps(provider.calls[0]["payload"])
    user_rules = [r for r in load_rules(session, USER, master_key) if r.source is RuleSource.USER]
    assert [(sorted(r.tokens), r.category_uuid) for r in user_rules] == [(["carrefour"], mine.uuid)]
    assert _filed(session, master_key)["CARREFOUR"] == "Supermarché"


def test_an_empty_queue_calls_no_model(session: Session, master_key: str):
    provider = FakeProvider(lambda payload: {"groups": []})
    result = _run(session, master_key, provider)
    assert (provider.calls, result.processed, result.remaining) == ([], 0, 0)
