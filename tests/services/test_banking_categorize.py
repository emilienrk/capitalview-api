"""
Category rules, resolution and natures (services/banking/categorize.py), and
the rules as stored (services/banking/categories.py).

Labels are shaped like the real ones, names replaced.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session

from dtos.banking import (
    BankTransferStatus as Status,
    CategoryNature,
    CategoryOrigin,
    CategorySource as Source,
    OperationNature as Nature,
    RuleSource,
)
from services.banking.categories import (
    Category,
    CategoryNotFoundError,
    create_category,
    load_rules,
    save_rule,
)
from services.banking.categorize import (
    EmptyRuleError,
    Rule,
    TooGeneralRuleError,
    WordFrequency,
    check_rule,
    nature_of,
    propose_tokens,
    resolve,
)
from services.banking.flows import transfer_patterns
from services.banking.transactions import label_words
from tests.services.test_banking_flows import ACCOUNT_A, USER, _link, _raw, _store

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
COURSES = Category("c-courses", "Courses", CategoryNature.EXPENSE, CategoryOrigin.BANK)
CARREFOUR = Category("c-carrefour", "Carrefour", CategoryNature.EXPENSE, CategoryOrigin.BANK)
PLACEMENTS = Category("c-placements", "Placements", CategoryNature.INVESTMENT, CategoryOrigin.BANK)
CATEGORIES = {c.uuid: c for c in (COURSES, CARREFOUR, PLACEMENTS)}
LABEL = "CARTE 21/06/26 CARREFOUR ANNECY CB*0837"


def _rule(uuid: str, words: str, category: Category, source: RuleSource = RuleSource.USER, minutes: int = 0) -> Rule:
    return Rule(uuid, frozenset(words.split()), category.uuid, source, T0 + timedelta(minutes=minutes))


def _resolve(override: str | None, *rules: Rule, label: str = LABEL, categories=CATEGORIES):
    return resolve(label_words(label), override, list(rules), categories)


class TestResolve:
    def test_a_rule_whose_words_are_all_in_the_label_files_it(self):
        resolution = _resolve(None, _rule("r1", "carrefour", CARREFOUR))
        assert (resolution.category, resolution.source, resolution.rule_uuid) == (CARREFOUR, Source.USER_RULE, "r1")

    def test_a_rule_missing_one_word_does_not(self):
        assert _resolve(None, _rule("r1", "carrefour lyon", CARREFOUR)).category is None

    def test_the_most_specific_rule_wins(self):
        general = _rule("r1", "carrefour", COURSES, minutes=5)
        specific = _rule("r2", "annecy carrefour", CARREFOUR, RuleSource.AI)
        assert _resolve(None, general, specific).rule_uuid == "r2"

    def test_at_equal_specificity_the_user_s_rule_beats_the_ai_s(self):
        ai = _rule("r1", "carrefour", COURSES, RuleSource.AI, minutes=5)
        user = _rule("r2", "annecy", CARREFOUR, RuleSource.USER)
        assert _resolve(None, ai, user).rule_uuid == "r2"

    def test_then_the_most_recent_wins(self):
        older = _rule("r1", "carrefour", COURSES)
        newer = _rule("r2", "annecy", CARREFOUR, minutes=1)
        assert _resolve(None, older, newer).rule_uuid == "r2"
        assert _resolve(None, newer, older).rule_uuid == "r2"

    def test_an_ai_rule_says_so(self):
        assert _resolve(None, _rule("r1", "carrefour", CARREFOUR, RuleSource.AI)).source is Source.AI_RULE

    def test_the_override_beats_every_rule(self):
        resolution = _resolve(PLACEMENTS.uuid, _rule("r1", "annecy carrefour", CARREFOUR))
        assert (resolution.category, resolution.source, resolution.rule_uuid) == (PLACEMENTS, Source.MANUAL, None)

    def test_an_override_to_none_files_it_nowhere_and_ignores_the_rules(self):
        resolution = _resolve("none", _rule("r1", "carrefour", CARREFOUR))
        assert (resolution.category, resolution.source) == (None, Source.MANUAL)

    def test_an_override_to_a_deleted_category_reads_as_uncategorised(self):
        resolution = _resolve("deleted-uuid", _rule("r1", "carrefour", CARREFOUR))
        assert (resolution.category, resolution.source) == (None, None)

    def test_a_rule_of_a_deleted_category_is_skipped(self):
        orphan = _rule("r1", "annecy carrefour", CARREFOUR)
        fallback = _rule("r2", "carrefour", COURSES)
        categories = {COURSES.uuid: COURSES}
        assert _resolve(None, orphan, fallback, categories=categories).rule_uuid == "r2"

    def test_no_rule_no_category(self):
        assert _resolve(None) == _resolve(None, _rule("r1", "lidl", COURSES))
        assert _resolve(None).category is None


class TestProposedWords:
    def test_the_rarest_telling_words_come_first(self):
        frequency = WordFrequency({"carte": 900, "cb": 800, "carrefour": 12, "annecy": 40}, common_above=50)
        assert propose_tokens(LABEL, frequency) == ["carrefour", "annecy"]

    def test_at_most_two_words_are_proposed(self):
        frequency = WordFrequency({"carte": 900, "boulangerie": 20, "dupont": 3, "annecy": 40}, common_above=50)
        assert propose_tokens("CARTE BOULANGERIE DUPONT ANNECY", frequency) == ["dupont", "boulangerie"]

    def test_the_history_decides_the_order(self):
        frequency = WordFrequency({"carte": 900, "cb": 800, "carrefour": 40, "annecy": 12}, common_above=50)
        assert propose_tokens(LABEL, frequency) == ["annecy", "carrefour"]

    def test_a_reference_unique_to_one_payslip_is_left_out(self):
        """Each payslip embeds an alphabetic reference, so each has its own signature."""
        frequency = WordFrequency(
            {"vir": 500, "sepa": 300, "de": 200, "employeur": 36, "salaire": 38, "gjpbazz": 1}, common_above=50,
        )
        label = "VIR SEPA EMPLOYEUR SALAIRE DE 2026-07 402147-GJPBAZZ"
        assert propose_tokens(label, frequency) == ["employeur", "salaire"]

    def test_a_word_seen_once_is_kept_when_nothing_else_tells(self):
        frequency = WordFrequency({"prlv": 110, "sepa": 300, "olness": 1}, common_above=50)
        assert propose_tokens("PRLV SEPA OLNESS-OLNESS", frequency) == ["olness"]

    def test_a_label_of_common_words_only_proposes_them_all(self):
        frequency = WordFrequency({"carte": 900, "cb": 800}, common_above=50)
        assert propose_tokens("CARTE CB*0837", frequency) == ["cb", "carte"]


class TestRuleChecks:
    FREQUENCY = WordFrequency({"carte": 900, "cb": 800, "carrefour": 12}, common_above=50)

    def test_an_empty_rule_is_refused(self):
        with pytest.raises(EmptyRuleError):
            check_rule(frozenset(), self.FREQUENCY)

    def test_a_rule_of_common_words_only_is_refused(self):
        with pytest.raises(TooGeneralRuleError):
            check_rule(frozenset({"carte", "cb"}), self.FREQUENCY)

    def test_a_word_exactly_at_the_threshold_still_tells(self):
        check_rule(frozenset({"annecy"}), WordFrequency({"annecy": 50}, common_above=50))

    def test_one_telling_word_is_enough(self):
        check_rule(frozenset({"carte", "carrefour"}), self.FREQUENCY)


class TestNature:
    def test_a_transfer_to_a_savings_account_is_saving(self):
        assert nature_of(False, Status.SAVINGS, savings_legs=1, category=COURSES) is Nature.SAVING
        assert nature_of(True, Status.RECURRING, savings_legs=1, category=None) is Nature.SAVING

    def test_a_transfer_between_two_savings_accounts_is_internal(self):
        assert nature_of(False, Status.SAVINGS, savings_legs=2, category=None) is Nature.INTERNAL

    def test_a_transfer_between_two_current_accounts_is_internal(self):
        assert nature_of(False, Status.CONFIRMED, savings_legs=0, category=COURSES) is Nature.INTERNAL
        assert nature_of(False, Status.LEARNED, savings_legs=0, category=None) is Nature.INTERNAL

    @pytest.mark.parametrize("status", [Status.REFUND, Status.REVERSAL])
    def test_a_cancelled_operation_is_neutralised(self, status):
        assert nature_of(False, status, savings_legs=0, category=COURSES) is Nature.NEUTRALIZED

    def test_a_pair_only_offered_counts_by_its_category(self):
        assert nature_of(False, Status.SUGGESTED, savings_legs=1, category=PLACEMENTS) is Nature.INVESTMENT

    def test_otherwise_the_category_decides(self):
        assert nature_of(False, None, savings_legs=0, category=PLACEMENTS) is Nature.INVESTMENT

    def test_without_category_the_direction_decides(self):
        assert nature_of(False, None, savings_legs=0, category=None) is Nature.EXPENSE
        assert nature_of(True, None, savings_legs=0, category=None) is Nature.INCOME


class TestWordFrequency:
    def test_words_are_counted_per_signature_not_per_operation(self, session: Session, master_key: str):
        _link(session, master_key, ACCOUNT_A)
        _store(
            session, master_key, ACCOUNT_A,
            *[_raw("5.00", "DBIT", f"2026-03-{day:02d}", ref=f"b{day}", label=f"CARTE {day:02d}/03/26 BOULANGERIE CB*08")
              for day in range(1, 11)],
            _raw("9.00", "DBIT", "2026-03-11", ref="p1", label="CARTE 11/03/26 PHARMACIE CB*08"),
        )
        frequency = transfer_patterns(session, USER, master_key).word_frequency
        assert (frequency.of("boulangerie"), frequency.of("carte"), frequency.of("pharmacie")) == (1, 2, 1)

    def test_a_small_history_calls_no_word_common_below_three_signatures(self, session: Session, master_key: str):
        _link(session, master_key, ACCOUNT_A)
        _store(
            session, master_key, ACCOUNT_A,
            _raw("5.00", "DBIT", "2026-03-01", ref="1", label="CARTE BOULANGERIE"),
            _raw("9.00", "DBIT", "2026-03-02", ref="2", label="CARTE PHARMACIE"),
            _raw("9.00", "DBIT", "2026-03-03", ref="3", label="CARTE LIBRAIRIE"),
        )
        frequency = transfer_patterns(session, USER, master_key).word_frequency
        assert not frequency.is_common("pharmacie")
        assert frequency.is_common("carte")

    def test_a_word_on_more_than_five_percent_of_signatures_is_common(self, session: Session, master_key: str):
        _link(session, master_key, ACCOUNT_A)
        merchants = [f"MARCHAND{chr(65 + i)}{chr(65 + j)}" for i in range(10) for j in range(10)]
        _store(
            session, master_key, ACCOUNT_A,
            *[_raw("1.00", "DBIT", "2026-03-01", ref=m, label=("VILLE " if n < 6 else "") + m) for n, m in enumerate(merchants)],
        )
        frequency = transfer_patterns(session, USER, master_key).word_frequency
        assert frequency.common_above == 5.0
        assert frequency.is_common("ville")


class TestStoredRules:
    FREQUENCY = WordFrequency({"carte": 900, "cb": 800, "carrefour": 12}, common_above=50)

    def _category(self, session: Session, master_key: str, user: str = USER) -> Category:
        return create_category(session, user, master_key, "Courses", CategoryNature.EXPENSE, CategoryOrigin.BANK)

    def test_a_rule_is_stored_in_the_words_a_label_is_read_as(self, session: Session, master_key: str):
        category = self._category(session, master_key)
        rule = save_rule(session, USER, master_key, ["CARREFOUR", "Carte 21/06"], category.uuid, RuleSource.USER, self.FREQUENCY)
        assert load_rules(session, USER, master_key) == [rule]
        assert rule.tokens == {"carrefour", "carte"}

    def test_a_rule_of_digits_only_is_refused_as_empty(self, session: Session, master_key: str):
        category = self._category(session, master_key)
        with pytest.raises(EmptyRuleError):
            save_rule(session, USER, master_key, ["0837", "*"], category.uuid, RuleSource.USER, self.FREQUENCY)

    def test_a_rule_of_common_words_is_refused(self, session: Session, master_key: str):
        category = self._category(session, master_key)
        with pytest.raises(TooGeneralRuleError):
            save_rule(session, USER, master_key, ["carte", "cb"], category.uuid, RuleSource.USER, self.FREQUENCY)
        assert load_rules(session, USER, master_key) == []

    def test_a_rule_on_the_same_words_is_replaced(self, session: Session, master_key: str):
        courses = self._category(session, master_key)
        other = create_category(session, USER, master_key, "Carrefour", CategoryNature.EXPENSE, CategoryOrigin.AI)
        save_rule(session, USER, master_key, ["carrefour"], courses.uuid, RuleSource.AI, self.FREQUENCY)
        replaced = save_rule(session, USER, master_key, ["carrefour"], other.uuid, RuleSource.USER, self.FREQUENCY)
        assert load_rules(session, USER, master_key) == [replaced]
        assert (replaced.category_uuid, replaced.source) == (other.uuid, RuleSource.USER)

    def test_the_ai_never_overwrites_a_user_s_rule(self, session: Session, master_key: str):
        courses = self._category(session, master_key)
        other = create_category(session, USER, master_key, "Carrefour", CategoryNature.EXPENSE, CategoryOrigin.AI)
        mine = save_rule(session, USER, master_key, ["carrefour"], courses.uuid, RuleSource.USER, self.FREQUENCY)
        assert save_rule(session, USER, master_key, ["carrefour"], other.uuid, RuleSource.AI, self.FREQUENCY) is None
        assert load_rules(session, USER, master_key) == [mine]

    def test_a_rule_cannot_file_into_another_user_s_category(self, session: Session, master_key: str):
        theirs = self._category(session, master_key, user="someone_else")
        with pytest.raises(CategoryNotFoundError):
            save_rule(session, USER, master_key, ["carrefour"], theirs.uuid, RuleSource.USER, self.FREQUENCY)
