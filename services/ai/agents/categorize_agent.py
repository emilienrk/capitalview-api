"""
Suggests a category for the groups of operations nothing files yet.

One constrained call per batch: the model sees each group's example label,
direction, count and median amount, plus the user's categories, and answers a
category name, a nature and a confidence per group. It never writes anything
itself — what it says goes through `run_ai_categorization`'s guards, and each
accepted group becomes an AI rule the user can overwrite.

Called batch by batch from the front rather than as a background job, so the
Master Key never outlives the request.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlmodel import Session

from dtos.banking import BankAICategorizeResult, BankUncategorizedGroup, CategoryNature, CategoryOrigin, RuleSource
from services.ai.manager import AIProviderManager
from services.ai.providers.base import AIProvider
from services.banking.categories import (
    InvalidCategoryNameError,
    create_category,
    load_categories,
    name_key,
    save_rule,
)
from services.banking.categorize import EmptyRuleError, TooGeneralRuleError
from services.banking.flows import transfer_patterns, uncategorized_groups

logger = logging.getLogger(__name__)

BATCH_SIZE = 100
MIN_CONFIDENCE = 0.6
# A model inventing a category per merchant would bury the user's own.
MAX_NEW_CATEGORIES = 15


@dataclass(frozen=True)
class Suggestion:
    group_id: str
    category_name: str
    nature: CategoryNature
    confidence: float


class CategorizeAgent:
    def __init__(self, provider: AIProvider):
        self._provider = provider

    @staticmethod
    def system_prompt() -> str:
        return (
            "You file bank operations into spending categories for a personal finance app. "
            "Each group gathers operations whose labels read alike. Reuse one of the user's "
            "categories whenever it fits, spelled exactly as given. Otherwise propose a short, "
            "general category name in French (\"Courses\", \"Transport\"), never a merchant name. "
            "Nature: EXPENSE for spending, INCOME for money earned or received, SAVING for money "
            "set aside on a savings account, INVESTMENT for money sent to a broker or an "
            "investment account. Answer null with a low confidence when the label does not "
            "say what the operation is."
        )

    @staticmethod
    def output_config() -> dict[str, Any]:
        return {
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "groups": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "group_id": {"type": "string"},
                                    "category_name": {"type": ["string", "null"]},
                                    "nature": {"type": "string", "enum": [n.value for n in CategoryNature]},
                                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                                },
                                "required": ["group_id", "category_name", "nature", "confidence"],
                                "additionalProperties": False,
                            },
                        }
                    },
                    "required": ["groups"],
                    "additionalProperties": False,
                },
            }
        }

    async def suggest(
        self, groups: dict[str, BankUncategorizedGroup], categories: list[tuple[str, CategoryNature]]
    ) -> list[dict[str, Any]]:
        """The model's raw answer per group; malformed output reads as no answer."""
        payload = {
            "categories": [{"name": name, "nature": nature.value} for name, nature in categories],
            "groups": [
                {
                    "group_id": group_id,
                    "label": group.label,
                    "direction": "credit" if group.is_credit else "debit",
                    "count": group.count,
                    "median_amount": f"{group.median} {group.currency}",
                }
                for group_id, group in groups.items()
            ],
        }
        response = await self._provider._send_message(
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            system=self.system_prompt(),
            output_config=self.output_config(),
        )
        try:
            answer = json.loads(self._provider.extract_text(response))
        except (json.JSONDecodeError, TypeError):
            logger.warning("categorisation: the model's answer is not JSON")
            return []
        items = answer.get("groups") if isinstance(answer, dict) else None
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def build_categorize_agent(session: Session, user_uuid: str, master_key: str) -> CategorizeAgent:
    """Raises NoProviderAvailableError when no chat provider is configured."""
    manager = AIProviderManager.from_user_settings(session, user_uuid, master_key)
    return CategorizeAgent(manager.get_provider_for_capability("chat"))


def accepted_suggestions(raw: list[dict[str, Any]], group_ids: set[str]) -> list[Suggestion]:
    """What survives the guards, one per group: a known group, a real nature,
    a category name, and enough confidence."""
    accepted: dict[str, Suggestion] = {}
    for item in raw:
        group_id, name = item.get("group_id"), item.get("category_name")
        if group_id not in group_ids or group_id in accepted:
            continue
        try:
            nature = CategoryNature(item.get("nature"))
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError):
            continue
        if not isinstance(name, str) or confidence < MIN_CONFIDENCE:
            continue
        accepted[group_id] = Suggestion(group_id, name.strip(), nature, confidence)
    return list(accepted.values())


async def run_ai_categorization(
    session: Session, user_uuid: str, master_key: str, agent: CategorizeAgent, skip: int = 0
) -> BankAICategorizeResult:
    """File the next batch of the heaviest groups nothing files yet.

    `skip` is the number of groups at the head of the queue earlier batches of
    this run left unfiled. The queue is sorted on a total order and filing only
    removes groups from it, so those stay at its head.
    """
    queue = uncategorized_groups(session, user_uuid, master_key)
    batch = queue.groups[skip:skip + BATCH_SIZE]
    groups = {f"g{n}": group for n, group in enumerate(batch, start=1)}
    categories = load_categories(session, user_uuid, master_key)
    by_name = {name_key(c.name): c for c in categories.values()}

    raw = await agent.suggest(groups, sorted((c.name, c.nature) for c in categories.values())) if groups else []
    frequency = transfer_patterns(session, user_uuid, master_key).word_frequency

    rules_created = categories_created = 0
    for suggestion in accepted_suggestions(raw, set(groups)):
        category = by_name.get(name_key(suggestion.category_name))
        if category is None:
            if categories_created >= MAX_NEW_CATEGORIES:
                continue
            try:
                category = create_category(
                    session, user_uuid, master_key, suggestion.category_name, suggestion.nature, CategoryOrigin.AI,
                )
            except InvalidCategoryNameError:
                continue
            by_name[name_key(category.name)] = category
            categories_created += 1
        try:
            rule = save_rule(
                session, user_uuid, master_key, groups[suggestion.group_id].tokens,
                category.uuid, RuleSource.AI, frequency,
            )
        except (EmptyRuleError, TooGeneralRuleError):
            continue
        if rule is not None:
            rules_created += 1

    # Every group tried so far and still unfiled sits at the head of the queue:
    # each is heavier than any group not tried yet.
    tried = {(g.signature, g.is_credit) for g in queue.groups[:skip + len(batch)]}
    after = uncategorized_groups(session, user_uuid, master_key)
    next_skip = sum(1 for g in after.groups if (g.signature, g.is_credit) in tried)
    return BankAICategorizeResult(
        processed=len(batch),
        rules_created=rules_created,
        categories_created=categories_created,
        skip=next_skip,
        remaining=max(0, after.total_groups - next_skip),
    )
