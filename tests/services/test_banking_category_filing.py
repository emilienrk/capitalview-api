"""
Categories as operations are read (services/banking/flows.py): the list's
category, nature and type, filing one operation or all those like it, and the
queue of what is left to file.

Labels are shaped like the real ones, names replaced.
"""
from decimal import Decimal

import pytest
from sqlmodel import Session, select

from dtos.banking import (
    CategoryNature,
    CategoryOrigin,
    CategorySource as Source,
    OperationNature as Nature,
    OperationType,
)
from models.bank import BankAccount
from models.banking import BankTransaction
from services.banking.categories import create_category, delete_category
from services.banking.categorize import EmptyRuleError, TooGeneralRuleError
from services.banking.flows import (
    CategoryRequiredError,
    RuleOutsideLabelError,
    assign_category,
    list_month_transactions,
    uncategorized_groups,
)
from services.banking.transfer_decisions import TransactionNotFoundError
from services.encryption import encrypt_data
from tests.services.test_banking_flows import USER, _link, _raw, _store

CURRENT, LIVRET, LDDS = "current", "savings", "ldds"  # "savings" is a Livret A


def _ops(session: Session, master_key: str, *operations: tuple[str, str, str, str, str]) -> None:
    """(account, day, amount, direction, label)"""
    for account in sorted({op[0] for op in operations}):
        if session.get(BankAccount, account) is None:
            _link(session, master_key, account)
    for n, (account, day, amount, direction, label) in enumerate(operations):
        _store(session, master_key, account, _raw(amount, direction, day, ref=f"{account}-{day}-{n}", label=label))


def _as_ldds(session: Session, master_key: str) -> None:
    account = session.get(BankAccount, LDDS)
    account.account_type_enc = encrypt_data("LDD", master_key)
    session.add(account)
    session.commit()


def _month(session: Session, master_key: str, period: str = "2026-03"):
    return {tx.label: tx for tx in list_month_transactions(session, USER, master_key, period).transactions}


def _groceries(session: Session, master_key: str) -> None:
    _ops(
        session, master_key,
        (CURRENT, "2026-03-02", "42.10", "DBIT", "CARTE 01/03/26 CARREFOUR ANNECY CB*08"),
        (CURRENT, "2026-03-09", "18.40", "DBIT", "CARTE 08/03/26 CARREFOUR CITY LYON CB*08"),
        (CURRENT, "2026-03-16", "33.00", "DBIT", "CARTE 15/03/26 CARREFOUR ANNECY CB*08"),
        (CURRENT, "2026-03-20", "9.90", "DBIT", "CARTE 19/03/26 PHARMACIE DU LAC CB*08"),
        (CURRENT, "2026-03-25", "2100.00", "CRDT", "VIR SEPA EMPLOYEUR SALAIRE DE 2026-03 REFABC"),
    )


def _courses(session: Session, master_key: str):
    return create_category(session, USER, master_key, "Courses", CategoryNature.EXPENSE, CategoryOrigin.BANK)


class TestTheList:
    def test_an_operation_nothing_files_counts_by_its_direction(self, session: Session, master_key: str):
        _groceries(session, master_key)
        month = _month(session, master_key)
        pharmacie = month["CARTE 19/03/26 PHARMACIE DU LAC CB*08"]
        salaire = month["VIR SEPA EMPLOYEUR SALAIRE DE 2026-03 REFABC"]
        assert (pharmacie.category_id, pharmacie.category_source, pharmacie.nature) == (None, None, Nature.EXPENSE)
        assert (salaire.nature, salaire.operation_type) == (Nature.INCOME, OperationType.TRANSFER)
        assert pharmacie.operation_type is OperationType.CARD

    def test_a_transfer_to_a_livret_is_saving(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2026-03-05", "300.00", "DBIT", "VIR Virement depuis Compte courant"),
            (LIVRET, "2026-03-05", "300.00", "CRDT", "VIR Virement depuis Compte courant"),
        )
        transactions = list_month_transactions(session, USER, master_key, "2026-03").transactions
        assert {tx.account_id: tx.nature for tx in transactions} == {CURRENT: Nature.SAVING, LIVRET: Nature.SAVING}

    def test_a_transfer_from_a_livret_to_an_ldds_is_internal(self, session: Session, master_key: str):
        _link(session, master_key, LDDS)
        _as_ldds(session, master_key)
        _ops(
            session, master_key,
            (LIVRET, "2026-03-05", "300.00", "DBIT", "VIR Virement interne depuis LIVRET A"),
            (LDDS, "2026-03-05", "300.00", "CRDT", "VIR Virement interne depuis LIVRET A"),
        )
        transactions = list_month_transactions(session, USER, master_key, "2026-03").transactions
        assert [tx.nature for tx in transactions] == [Nature.INTERNAL, Nature.INTERNAL]

    def test_a_refund_is_neutralised(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2026-03-02", "59.45", "DBIT", "CARTE 01/03/26 ZALANDO PAYMENTS CB*08"),
            (CURRENT, "2026-03-12", "59.45", "CRDT", "AVOIR 11/03/26 ZALANDO PAYMENTS CB*08"),
        )
        assert {tx.nature for tx in _month(session, master_key).values()} == {Nature.NEUTRALIZED}

    def test_a_row_without_stored_type_is_listed_with_its_type(self, session: Session, master_key: str):
        _ops(session, master_key, (CURRENT, "2026-03-02", "20.00", "DBIT", "RETRAIT DAB 01/03/26 ANNECY CB*08"))
        row = session.exec(select(BankTransaction)).one()
        row.operation_type_enc = None
        session.add(row)
        session.commit()
        month = list_month_transactions(session, USER, master_key, "2026-03")
        assert month.transactions[0].operation_type is OperationType.WITHDRAWAL


class TestFiling:
    def test_correcting_one_operation_files_every_similar_one(self, session: Session, master_key: str):
        _groceries(session, master_key)
        courses = _courses(session, master_key)
        target = _month(session, master_key)["CARTE 01/03/26 CARREFOUR ANNECY CB*08"]

        result = assign_category(session, USER, master_key, target.id, courses.uuid, apply_to_similar=True, tokens=["carrefour"])

        assert result.filed_count == 3
        assert (result.transaction.category_name, result.transaction.category_source) == ("Courses", Source.USER_RULE)
        filed = {label for label, tx in _month(session, master_key).items() if tx.category_id == courses.uuid}
        assert filed == {
            "CARTE 01/03/26 CARREFOUR ANNECY CB*08",
            "CARTE 08/03/26 CARREFOUR CITY LYON CB*08",
            "CARTE 15/03/26 CARREFOUR ANNECY CB*08",
        }

    def test_the_proposed_words_are_used_when_none_are_given(self, session: Session, master_key: str):
        _groceries(session, master_key)
        courses = _courses(session, master_key)
        target = _month(session, master_key)["CARTE 19/03/26 PHARMACIE DU LAC CB*08"]
        result = assign_category(session, USER, master_key, target.id, courses.uuid, apply_to_similar=True)
        assert result.filed_count == 1
        assert result.transaction.rule_id is not None

    def test_one_operation_can_be_filed_alone(self, session: Session, master_key: str):
        _groceries(session, master_key)
        courses = _courses(session, master_key)
        month = _month(session, master_key)
        target = month["CARTE 01/03/26 CARREFOUR ANNECY CB*08"]

        result = assign_category(session, USER, master_key, target.id, courses.uuid, apply_to_similar=False)

        assert (result.filed_count, result.transaction.category_source) == (1, Source.MANUAL)
        after = _month(session, master_key)
        assert after["CARTE 15/03/26 CARREFOUR ANNECY CB*08"].category_id is None

    def test_an_operation_filed_as_none_ignores_the_rules(self, session: Session, master_key: str):
        _groceries(session, master_key)
        courses = _courses(session, master_key)
        month = _month(session, master_key)
        assign_category(session, USER, master_key, month["CARTE 01/03/26 CARREFOUR ANNECY CB*08"].id, courses.uuid, True, ["carrefour"])
        exception = month["CARTE 15/03/26 CARREFOUR ANNECY CB*08"]

        result = assign_category(session, USER, master_key, exception.id, None, apply_to_similar=False)

        assert (result.filed_count, result.transaction.category_id, result.transaction.category_source) == (0, None, Source.MANUAL)

    def test_a_rule_drops_the_operation_s_own_override(self, session: Session, master_key: str):
        _groceries(session, master_key)
        courses = _courses(session, master_key)
        target = _month(session, master_key)["CARTE 01/03/26 CARREFOUR ANNECY CB*08"]
        assign_category(session, USER, master_key, target.id, None, apply_to_similar=False)

        result = assign_category(session, USER, master_key, target.id, courses.uuid, True, ["carrefour"])

        assert result.transaction.category_source is Source.USER_RULE

    def test_a_manual_filing_to_a_deleted_category_reads_as_uncategorised(self, session: Session, master_key: str):
        _groceries(session, master_key)
        courses = _courses(session, master_key)
        target = _month(session, master_key)["CARTE 01/03/26 CARREFOUR ANNECY CB*08"]
        assign_category(session, USER, master_key, target.id, courses.uuid, apply_to_similar=False)
        delete_category(session, USER, master_key, courses.uuid)
        after = _month(session, master_key)["CARTE 01/03/26 CARREFOUR ANNECY CB*08"]
        assert (after.category_id, after.category_source, after.nature) == (None, None, Nature.EXPENSE)

    def test_a_category_nature_decides_the_operation_s(self, session: Session, master_key: str):
        _groceries(session, master_key)
        placements = create_category(session, USER, master_key, "Placements", CategoryNature.INVESTMENT, CategoryOrigin.BANK)
        target = _month(session, master_key)["CARTE 19/03/26 PHARMACIE DU LAC CB*08"]
        assert assign_category(session, USER, master_key, target.id, placements.uuid, False).transaction.nature is Nature.INVESTMENT

    def test_a_rule_needs_a_category(self, session: Session, master_key: str):
        _groceries(session, master_key)
        target = _month(session, master_key)["CARTE 01/03/26 CARREFOUR ANNECY CB*08"]
        with pytest.raises(CategoryRequiredError):
            assign_category(session, USER, master_key, target.id, None, apply_to_similar=True)

    def test_a_rule_only_requires_words_of_the_label(self, session: Session, master_key: str):
        _groceries(session, master_key)
        courses = _courses(session, master_key)
        target = _month(session, master_key)["CARTE 01/03/26 CARREFOUR ANNECY CB*08"]
        with pytest.raises(RuleOutsideLabelError):
            assign_category(session, USER, master_key, target.id, courses.uuid, True, ["lidl"])

    @pytest.mark.parametrize("tokens, error", [([], EmptyRuleError), (["CB*08"], TooGeneralRuleError)])
    def test_an_empty_or_general_rule_is_refused(self, session: Session, master_key: str, tokens, error):
        _groceries(session, master_key)
        courses = _courses(session, master_key)
        target = _month(session, master_key)["CARTE 01/03/26 CARREFOUR ANNECY CB*08"]
        with pytest.raises(error):
            assign_category(session, USER, master_key, target.id, courses.uuid, True, tokens)

    def test_another_user_s_operation_is_not_found(self, session: Session, master_key: str):
        _groceries(session, master_key)
        target = _month(session, master_key)["CARTE 01/03/26 CARREFOUR ANNECY CB*08"]
        with pytest.raises(TransactionNotFoundError):
            assign_category(session, "someone_else", master_key, target.id, None, apply_to_similar=False)


class TestQueue:
    def test_groups_are_what_nothing_files_heaviest_first(self, session: Session, master_key: str):
        _groceries(session, master_key)
        queue = uncategorized_groups(session, USER, master_key)
        assert [(g.label, g.count, g.total, g.is_credit) for g in queue.groups] == [
            ("VIR SEPA EMPLOYEUR SALAIRE DE 2026-03 REFABC", 1, Decimal("2100.00"), True),
            ("CARTE 15/03/26 CARREFOUR ANNECY CB*08", 2, Decimal("75.10"), False),
            ("CARTE 08/03/26 CARREFOUR CITY LYON CB*08", 1, Decimal("18.40"), False),
            ("CARTE 19/03/26 PHARMACIE DU LAC CB*08", 1, Decimal("9.90"), False),
        ]
        annecy = queue.groups[1]
        assert (str(annecy.median), annecy.currency, str(annecy.last_date)) == ("37.55", "EUR", "2026-03-16")
        assert (queue.total_groups, queue.total_operations) == (4, 5)

    def test_a_filed_operation_leaves_the_queue(self, session: Session, master_key: str):
        _groceries(session, master_key)
        courses = _courses(session, master_key)
        target = _month(session, master_key)["CARTE 01/03/26 CARREFOUR ANNECY CB*08"]
        assign_category(session, USER, master_key, target.id, courses.uuid, True, ["carrefour"])
        pharmacie = _month(session, master_key)["CARTE 19/03/26 PHARMACIE DU LAC CB*08"]
        assign_category(session, USER, master_key, pharmacie.id, None, apply_to_similar=False)
        assert [g.label for g in uncategorized_groups(session, USER, master_key).groups] == [
            "VIR SEPA EMPLOYEUR SALAIRE DE 2026-03 REFABC",
        ]

    def test_a_paired_transfer_is_not_queued(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2026-03-05", "300.00", "DBIT", "VIR Virement depuis Compte courant"),
            (LIVRET, "2026-03-05", "300.00", "CRDT", "VIR Virement depuis Compte courant"),
        )
        assert uncategorized_groups(session, USER, master_key).groups == []

    def test_the_limit_keeps_the_heaviest_and_the_totals_count_them_all(self, session: Session, master_key: str):
        _groceries(session, master_key)
        queue = uncategorized_groups(session, USER, master_key, limit=1)
        assert ([g.count for g in queue.groups], queue.total_groups) == ([1], 4)
