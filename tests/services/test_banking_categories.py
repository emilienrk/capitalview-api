"""
The user's categories and which of them each screen offers
(services/banking/categories.py).
"""
from datetime import datetime, timezone

import pytest
from sqlmodel import Session, select

from dtos.banking import CategoryNature as Nature, CategoryOrigin as Origin, CategoryScope as Scope
from models.banking import BankCategory, BankCategoryRule
from models.cashflow import Cashflow
from services.banking.categories import (
    CategoryNameTakenError,
    CategoryNotFoundError,
    InvalidCategoryNameError,
    available_categories,
    create_category,
    delete_category,
    materialize_cashflow_category,
    rename_category,
    set_category_nature,
)
from services.encryption import encrypt_data, hash_index

USER = "categories_user"
OTHER = "someone_else"


def _cashflow(session: Session, master_key: str, category: str, flow_type: str = "OUTFLOW", user: str = USER) -> None:
    session.add(Cashflow(
        user_uuid_bidx=hash_index(user, master_key),
        name_enc=encrypt_data("peu importe", master_key),
        flow_type_enc=encrypt_data(flow_type, master_key),
        category_enc=encrypt_data(category, master_key),
        amount_enc=encrypt_data("10", master_key),
        frequency_enc=encrypt_data("MONTHLY", master_key),
        transaction_date_enc=encrypt_data("2026-01-01", master_key),
    ))
    session.commit()


def _one_of_each_origin(session: Session, master_key: str) -> None:
    create_category(session, USER, master_key, "Courses", Nature.EXPENSE, Origin.BANK)
    create_category(session, USER, master_key, "Abonnements", Nature.EXPENSE, Origin.AI)
    create_category(session, USER, master_key, "Loyer", Nature.EXPENSE, Origin.CASHFLOW)
    _cashflow(session, master_key, "Salaire", "INFLOW")


def _offered(session: Session, master_key: str, scope: Scope, ai_enabled: bool) -> list[tuple[str, Origin, bool]]:
    return [
        (c.name, c.origin, c.id is not None)
        for c in available_categories(session, USER, master_key, scope, ai_enabled)
    ]


class TestAvailability:
    def test_banque_with_ai_offers_its_own_and_the_ai_s(self, session: Session, master_key: str):
        _one_of_each_origin(session, master_key)
        assert _offered(session, master_key, Scope.BANK, ai_enabled=True) == [
            ("Abonnements", Origin.AI, True), ("Courses", Origin.BANK, True),
        ]

    def test_the_declared_cashflow_with_ai_offers_only_its_own(self, session: Session, master_key: str):
        _one_of_each_origin(session, master_key)
        assert _offered(session, master_key, Scope.PLANNED, ai_enabled=True) == [
            ("Loyer", Origin.CASHFLOW, True), ("Salaire", Origin.CASHFLOW, False),
        ]

    @pytest.mark.parametrize("scope", [Scope.BANK, Scope.PLANNED])
    def test_without_ai_everything_is_offered_everywhere(self, session: Session, master_key: str, scope: Scope):
        _one_of_each_origin(session, master_key)
        assert _offered(session, master_key, scope, ai_enabled=False) == [
            ("Abonnements", Origin.AI, True),
            ("Courses", Origin.BANK, True),
            ("Loyer", Origin.CASHFLOW, True),
            ("Salaire", Origin.CASHFLOW, False),
        ]

    def test_a_name_is_offered_once_whatever_its_case_or_accents(self, session: Session, master_key: str):
        create_category(session, USER, master_key, "Épargne", Nature.SAVING, Origin.BANK)
        _cashflow(session, master_key, "epargne")
        _cashflow(session, master_key, "EPARGNE ")
        assert _offered(session, master_key, Scope.BANK, ai_enabled=False) == [("Épargne", Origin.BANK, True)]

    def test_another_user_s_categories_are_never_offered(self, session: Session, master_key: str):
        create_category(session, OTHER, master_key, "Voyages", Nature.EXPENSE, Origin.BANK)
        _cashflow(session, master_key, "Cadeaux", user=OTHER)
        assert _offered(session, master_key, Scope.BANK, ai_enabled=False) == []


class TestNames:
    def test_a_name_is_unique_whatever_its_case_or_accents(self, session: Session, master_key: str):
        create_category(session, USER, master_key, "Épargne", Nature.SAVING, Origin.BANK)
        with pytest.raises(CategoryNameTakenError):
            create_category(session, USER, master_key, "  EPARGNE", Nature.SAVING, Origin.AI)

    def test_two_users_may_share_a_name(self, session: Session, master_key: str):
        create_category(session, USER, master_key, "Courses", Nature.EXPENSE, Origin.BANK)
        create_category(session, OTHER, master_key, "Courses", Nature.EXPENSE, Origin.BANK)

    def test_renaming_onto_another_category_s_name_is_refused(self, session: Session, master_key: str):
        create_category(session, USER, master_key, "Courses", Nature.EXPENSE, Origin.BANK)
        loyer = create_category(session, USER, master_key, "Loyer", Nature.EXPENSE, Origin.BANK)
        with pytest.raises(CategoryNameTakenError):
            rename_category(session, USER, master_key, loyer.uuid, "courses")

    def test_a_category_may_change_only_its_own_case(self, session: Session, master_key: str):
        courses = create_category(session, USER, master_key, "courses", Nature.EXPENSE, Origin.BANK)
        assert rename_category(session, USER, master_key, courses.uuid, "Courses").name == "Courses"

    @pytest.mark.parametrize("name", ["", "   ", "x" * 61])
    def test_an_empty_or_overlong_name_is_refused(self, session: Session, master_key: str, name: str):
        with pytest.raises(InvalidCategoryNameError):
            create_category(session, USER, master_key, name, Nature.EXPENSE, Origin.BANK)

    def test_another_user_s_category_cannot_be_touched(self, session: Session, master_key: str):
        theirs = create_category(session, OTHER, master_key, "Voyages", Nature.EXPENSE, Origin.BANK)
        with pytest.raises(CategoryNotFoundError):
            rename_category(session, USER, master_key, theirs.uuid, "Mine")
        with pytest.raises(CategoryNotFoundError):
            set_category_nature(session, USER, master_key, theirs.uuid, Nature.INCOME)
        with pytest.raises(CategoryNotFoundError):
            delete_category(session, USER, master_key, theirs.uuid)


class TestMaterialisation:
    def test_a_cashflow_category_picked_in_banque_becomes_a_row(self, session: Session, master_key: str):
        _cashflow(session, master_key, "Salaire", "INFLOW")
        _cashflow(session, master_key, "Salaire", "INFLOW")
        _cashflow(session, master_key, "salaire", "OUTFLOW")

        category = materialize_cashflow_category(session, USER, master_key, "Salaire")

        assert (category.name, category.nature, category.origin) == ("Salaire", Nature.INCOME, Origin.CASHFLOW)
        assert _offered(session, master_key, Scope.BANK, ai_enabled=False) == [("Salaire", Origin.CASHFLOW, True)]

    def test_a_tie_between_inflows_and_outflows_is_an_expense(self, session: Session, master_key: str):
        _cashflow(session, master_key, "Remboursements", "INFLOW")
        _cashflow(session, master_key, "Remboursements", "OUTFLOW")
        assert materialize_cashflow_category(session, USER, master_key, "Remboursements").nature is Nature.EXPENSE

    def test_materialising_twice_keeps_one_row(self, session: Session, master_key: str):
        _cashflow(session, master_key, "Loyer")
        first = materialize_cashflow_category(session, USER, master_key, "Loyer")
        assert materialize_cashflow_category(session, USER, master_key, "LOYER").uuid == first.uuid

    def test_a_name_no_cashflow_carries_is_not_materialised(self, session: Session, master_key: str):
        _cashflow(session, master_key, "Loyer", user=OTHER)
        with pytest.raises(CategoryNotFoundError):
            materialize_cashflow_category(session, USER, master_key, "Loyer")


class TestDeletion:
    def _rule(self, session: Session, master_key: str, category_uuid: str, word: str) -> None:
        session.add(BankCategoryRule(
            user_uuid_bidx=hash_index(USER, master_key),
            tokens_enc=encrypt_data(f'["{word}"]', master_key),
            tokens_bidx=hash_index(word, master_key),
            category_ref_enc=encrypt_data(category_uuid, master_key),
            source_enc=encrypt_data("user", master_key),
            created_at=datetime.now(timezone.utc),
        ))
        session.commit()

    def test_deleting_a_category_deletes_its_rules_and_only_its(self, session: Session, master_key: str):
        courses = create_category(session, USER, master_key, "Courses", Nature.EXPENSE, Origin.BANK)
        loyer = create_category(session, USER, master_key, "Loyer", Nature.EXPENSE, Origin.BANK)
        self._rule(session, master_key, courses.uuid, "carrefour")
        self._rule(session, master_key, courses.uuid, "lidl")
        self._rule(session, master_key, loyer.uuid, "foncia")

        delete_category(session, USER, master_key, courses.uuid)

        assert session.get(BankCategory, courses.uuid) is None
        assert [r.tokens_bidx for r in session.exec(select(BankCategoryRule)).all()] == [hash_index("foncia", master_key)]

    def test_the_nature_can_change(self, session: Session, master_key: str):
        livret = create_category(session, USER, master_key, "Livret", Nature.EXPENSE, Origin.BANK)
        assert set_category_nature(session, USER, master_key, livret.uuid, Nature.SAVING).nature is Nature.SAVING
