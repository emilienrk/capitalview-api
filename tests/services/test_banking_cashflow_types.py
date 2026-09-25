"""
The cashflow type of each operation (services/banking/cashflow_types.py) and
the rules of labels that set it (services/banking/type_rules.py).

Labels are shaped like the real ones, names replaced.
"""
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlmodel import Session, select

from dtos.banking import BankTransferStatus as Status, CashflowType as Type, TypeSource as Source
from models.banking import BankTransaction, BankTypeRule
from services.banking.flows import list_month_transactions
from services.banking.cashflow_types import Resolution, counted_leg, resolve_type, signed_amount
from services.banking.type_rules import TypeRule, TypeRules, rule_bidx, save_rule
from services.encryption import encrypt_data, hash_index
from tests.services.test_banking_flows import USER, _link
from tests.services.test_banking_real_cashflow import CURRENT, LDDS, LIVRET, _as_ldds, _month, _ops
from tests.services.test_banking_transfer_patterns import NEOBANK, _top_up


class TestResolution:
    def test_a_debit_with_nothing_is_an_expense_and_a_credit_an_income(self):
        assert resolve_type(False, None, 0, None, None) == Resolution(Type.EXPENSE, Source.DEFAULT, None)
        assert resolve_type(True, None, 0, None, None).type is Type.INCOME

    def test_a_transfer_with_one_savings_leg_is_saving(self):
        assert resolve_type(False, Status.SAVINGS, 1, None, None).type is Type.SAVING

    def test_a_transfer_between_two_savings_accounts_is_neutral(self):
        assert resolve_type(False, Status.SAVINGS, 2, None, None).type is Type.NEUTRAL

    @pytest.mark.parametrize("status", [Status.RECURRING, Status.LEARNED, Status.CONFIRMED])
    def test_a_transfer_between_two_current_accounts_is_neutral(self, status):
        assert resolve_type(False, status, 0, None, None) == Resolution(Type.NEUTRAL, Source.PAIR, None)

    @pytest.mark.parametrize("status", [Status.REVERSAL, Status.REFUND])
    def test_a_cancellation_is_neutral(self, status):
        assert resolve_type(True, status, 0, None, None).type is Type.NEUTRAL

    def test_a_pair_beats_the_override_and_the_rule(self):
        assert resolve_type(False, Status.RECURRING, 0, Type.EXPENSE, ("r1", Type.SAVING)).source is Source.PAIR

    def test_the_override_beats_the_rule(self):
        assert resolve_type(False, None, 0, Type.INVESTMENT, ("r1", Type.SAVING)) == Resolution(Type.INVESTMENT, Source.OVERRIDE, None)

    def test_the_rule_beats_the_default(self):
        assert resolve_type(False, None, 0, None, ("r1", Type.SAVING)) == Resolution(Type.SAVING, Source.RULE, "r1")

    def test_a_declared_deposit_types_what_nothing_else_does(self):
        assert resolve_type(False, None, 0, None, None, contributed=True) == Resolution(
            Type.INVESTMENT, Source.CONTRIBUTION, None
        )
        assert resolve_type(True, None, 0, None, None, contributed=True).type is Type.INVESTMENT

    def test_the_user_beats_a_declared_deposit(self):
        assert resolve_type(False, None, 0, Type.EXPENSE, None, contributed=True).source is Source.OVERRIDE

    def test_a_declared_deposit_beats_the_rule_of_its_label(self):
        """One label may go to an investment account one day and elsewhere the next."""
        resolution = resolve_type(False, None, 0, None, ("r1", Type.EXPENSE), contributed=True)
        assert (resolution.type, resolution.source) == (Type.INVESTMENT, Source.CONTRIBUTION)

    def test_a_suggested_pair_is_not_a_pair_yet(self):
        assert resolve_type(False, Status.SUGGESTED, 1, None, None) == Resolution(Type.EXPENSE, Source.DEFAULT, None)
        assert resolve_type(False, Status.SUGGESTED, 1, None, ("r1", Type.SAVING)).type is Type.SAVING


class TestAmounts:
    def test_a_refund_typed_expense_lowers_the_expenses(self):
        assert signed_amount(Decimal("30"), True, Type.EXPENSE) == Decimal("-30")

    def test_money_taken_back_lowers_the_saving(self):
        assert signed_amount(Decimal("50"), True, Type.SAVING) == Decimal("-50")

    def test_a_debit_typed_income_lowers_the_income(self):
        assert signed_amount(Decimal("150"), False, Type.INCOME) == Decimal("-150")

    def test_a_saving_pair_counts_on_the_leg_outside_the_savings_account(self):
        assert counted_leg(False, False, Type.SAVING, paired=True)
        assert not counted_leg(True, True, Type.SAVING, paired=True)

    def test_a_neutral_pair_counts_on_its_debit(self):
        assert counted_leg(False, False, Type.NEUTRAL, paired=True)
        assert not counted_leg(True, False, Type.NEUTRAL, paired=True)

    def test_an_operation_the_user_typed_neutral_counts_whatever_its_direction(self):
        assert counted_leg(True, False, Type.NEUTRAL, paired=False)


def _rule(signature: str, kind: Type, *, account: str = "a", is_credit: bool = False, minute: int = 0) -> TypeRule:
    return TypeRule(
        uuid=f"{signature}-{kind.value}-{minute}", account_bidx=account, is_credit=is_credit, signature=signature,
        words=frozenset(signature.split()), type=kind, created_at=datetime(2026, 9, 16, 12, minute, tzinfo=timezone.utc),
    )


def _rules(*rules: TypeRule) -> TypeRules:
    loaded = TypeRules()
    for rule in rules:
        loaded.exact[(rule.account_bidx, rule.is_credit, rule.signature)] = rule
        loaded.by_side.setdefault((rule.account_bidx, rule.is_credit), []).append(rule)
    return loaded


class TestRules:
    def test_the_exact_rule_beats_a_nearby_one(self):
        exact = _rule("emilien inst roukine vir", Type.SAVING)
        nearby = _rule("emilien inst roukine vir virement", Type.EXPENSE, minute=5)
        assert _rules(nearby, exact).reach("a", False, "emilien inst roukine vir", frozenset()) is exact

    def test_the_exact_rule_applies_even_when_every_word_is_common(self):
        rule = _rule("carte cb", Type.INVESTMENT)
        assert _rules(rule).reach("a", False, "carte cb", frozenset({"carte", "cb"})) is rule

    def test_a_rule_reaches_a_label_sharing_sixty_percent_of_its_words(self):
        rule = _rule("employeur juin salaire vir", Type.INCOME)
        assert _rules(rule).reach("a", False, "employeur juillet salaire vir", frozenset()) is rule  # 3/5

    def test_a_rule_stops_short_of_sixty_percent(self):
        shared = " ".join(f"w{n:02d}" for n in range(10))
        rule = _rule(f"{shared} ya yb yc yd", Type.INCOME)
        assert _rules(rule).reach("a", False, f"{shared} xa xb xc", frozenset()) is None  # 10/17

    def test_words_common_on_that_side_do_not_make_labels_alike(self):
        rule = _rule("boulangerie carte cb", Type.INVESTMENT)
        assert _rules(rule).reach("a", False, "boulangerie carte cb lac", frozenset()) is rule  # 3/4
        assert _rules(rule).reach("a", False, "boulangerie carte cb lac", frozenset({"carte", "cb"})) is None  # 1/2
        bare = _rule("boulangerie lac", Type.INVESTMENT)
        assert _rules(bare).reach("a", False, "boulangerie carte cb lac", frozenset({"carte", "cb"})) is bare  # 2/2

    def test_a_rule_of_another_account_or_direction_is_ignored(self):
        rules = _rules(_rule("emilien inst roukine vir", Type.SAVING, account="b"),
                       _rule("emilien inst roukine vir", Type.SAVING, is_credit=True))
        assert rules.reach("a", False, "emilien inst roukine vir", frozenset()) is None

    def test_the_nearest_rule_wins_then_the_most_recent(self):
        far = _rule("aa bb cc dd ee", Type.SAVING, minute=9)
        near = _rule("aa bb cc dd ff", Type.EXPENSE, minute=1)
        older = _rule("aa bb cc dd gg", Type.INCOME, minute=2)
        assert _rules(far, near).reach("a", False, "aa bb cc dd ff zz", frozenset()) is near
        assert _rules(near, older).reach("a", False, "aa bb cc dd zz", frozenset()).type is Type.INCOME

    def test_a_reference_changing_every_month_does_not_keep_labels_apart(self):
        rule = _rule("cie de ref salaire sepa vilmorin vir", Type.INCOME)
        label = "VIR SEPA VILMORIN & CIE SALAIRE DE 2026-08 402147-1 Réf ZZ1L2ZJSYU78NB5TWZZ1L2ZJSZ8833T8P0"
        assert _rules(rule).reach("a", False, label, frozenset()) is rule


class TestTheList:
    def test_a_transfer_to_a_livret_is_saving_on_both_legs(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2026-03-05", "300.00", "DBIT", "VIR Virement depuis Compte courant"),
            (LIVRET, "2026-03-05", "300.00", "CRDT", "VIR Virement depuis Compte courant"),
        )
        transactions = list_month_transactions(session, USER, master_key, "2026-03").transactions
        assert {(tx.account_id, tx.cashflow_type, tx.type_source) for tx in transactions} == {
            (CURRENT, Type.SAVING, Source.PAIR), (LIVRET, Type.SAVING, Source.PAIR),
        }

    def test_a_transfer_from_a_livret_to_an_ldds_is_neutral(self, session: Session, master_key: str):
        _link(session, master_key, LDDS)
        _as_ldds(session, master_key)
        _ops(
            session, master_key,
            (LIVRET, "2026-03-05", "300.00", "DBIT", "VIR Virement interne depuis LIVRET A"),
            (LDDS, "2026-03-05", "300.00", "CRDT", "VIR Virement interne depuis LIVRET A"),
        )
        transactions = list_month_transactions(session, USER, master_key, "2026-03").transactions
        assert [tx.cashflow_type for tx in transactions] == [Type.NEUTRAL, Type.NEUTRAL]

    def test_a_recurring_top_up_of_another_current_account_is_neutral(self, session: Session, master_key: str):
        _ops(session, master_key, *_top_up("03", "05", "20.00"), *_top_up("04", "10", "35.50"), *_top_up("05", "14", "12.00"))
        transactions = list_month_transactions(session, USER, master_key, "2025-05").transactions
        assert {(tx.account_id, tx.transfer_status, tx.cashflow_type) for tx in transactions} == {
            (CURRENT, Status.RECURRING, Type.NEUTRAL), (NEOBANK, Status.RECURRING, Type.NEUTRAL),
        }

    def test_a_label_rule_types_its_operations_and_an_override_one_of_them(self, session: Session, master_key: str):
        _ops(
            session, master_key,
            (CURRENT, "2026-03-05", "400.00", "DBIT", "VIR INST ROUKINE EMILIEN"),
            (CURRENT, "2026-03-20", "150.00", "DBIT", "VIR INST ROUKINE EMILIEN REF 2"),
        )
        save_rule(session, USER, master_key, CURRENT, False, "emilien inst roukine vir", Type.SAVING)
        month = _month(session, master_key)
        override = month["VIR INST ROUKINE EMILIEN REF 2"].id
        row = session.get(BankTransaction, override)
        row.type_override_enc = encrypt_data(Type.NEUTRAL.value, master_key)
        session.add(row)
        session.commit()

        month = _month(session, master_key)

        assert (month["VIR INST ROUKINE EMILIEN"].cashflow_type, month["VIR INST ROUKINE EMILIEN"].type_source) == (Type.SAVING, Source.RULE)
        assert (month["VIR INST ROUKINE EMILIEN REF 2"].cashflow_type, month["VIR INST ROUKINE EMILIEN REF 2"].type_source) == (Type.NEUTRAL, Source.OVERRIDE)


class TestRulesSavedUnderAnOlderReading:
    """Rules saved before labels were read with accents folded and a run holding
    a digit dropped whole (services/banking/labels.py)."""

    LABEL = "Paiement envoyé par Mme Dormia Laure SCT4412"

    def _older_rule(self, session: Session, master_key: str) -> None:
        signature = "dormia envoyé laure mme paiement par sct"
        session.add(BankTypeRule(
            user_uuid_bidx=hash_index(USER, master_key),
            rule_bidx=rule_bidx(CURRENT, True, signature, master_key),
            signature_enc=encrypt_data(signature, master_key),
            account_ref_enc=encrypt_data(CURRENT, master_key),
            credit_enc=encrypt_data("true", master_key),
            words_enc=encrypt_data(json.dumps(["dormia", "envoyé", "laure", "mme", "paiement", "par"]), master_key),
            type_enc=encrypt_data(Type.NEUTRAL.value, master_key),
            created_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
        ))
        session.commit()

    def test_one_still_types_its_label(self, session: Session, master_key: str):
        _ops(session, master_key, (CURRENT, "2026-03-05", "250.00", "CRDT", self.LABEL))
        self._older_rule(session, master_key)

        operation = _month(session, master_key)[self.LABEL]

        assert (operation.cashflow_type, operation.type_source) == (Type.NEUTRAL, Source.RULE)

    def test_answering_its_label_again_replaces_it(self, session: Session, master_key: str):
        _ops(session, master_key, (CURRENT, "2026-03-05", "250.00", "CRDT", self.LABEL))
        self._older_rule(session, master_key)

        save_rule(session, USER, master_key, CURRENT, True, self.LABEL, Type.INCOME)

        assert len(session.exec(select(BankTypeRule)).all()) == 1
        assert _month(session, master_key)[self.LABEL].cashflow_type is Type.INCOME
