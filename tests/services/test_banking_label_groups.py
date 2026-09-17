"""
Counterparts read off labels (services/banking/label_groups.py): the key that
groups one counterpart's operations and the name shown for it.

Labels are shaped like the real ones, names replaced.
"""
from datetime import date

import pytest

from services.banking.label_groups import display_label, group_key, group_name, merge_similar


@pytest.mark.parametrize(("label", "name"), [
    ("CARTE 21/06/26 CARREFOUR ANNECY CB*0837", "Carrefour Annecy"),
    ("CARTE 14/09/26 LIDL 4147 CB*0837", "Lidl"),
    ("CARTE 05/08/26 EASYJET 000KD CB*0837 340,36 C", "Easyjet"),
    ("AVOIR 11/03/26 ZALANDO PAYMENTS CB*08", "Zalando Payments"),
    ("RETRAIT DAB 06/06/26 REQUISTA    CB*0837", "Requista"),
    ("VIR SEPA TRANSALP'DOME S.A.S.", "Transalp'Dome S.A.S."),
    ("REJ VIR INST JEAN TIERS", "Jean Tiers"),
    ("PRLV SEPA EDF clients particuliers", "EDF clients particuliers"),
    ("VIR SEPA EMPLOYEUR SALAIRE DE 2026-06 402147-1 Réf ZZ1KQCU8WQC5OZZ7OWGDQFCIK", "Employeur Salaire De"),
    ("VIR Virement depuis Compte courant JEAN TIERS Réf : SCT406182026", "Virement depuis Compte courant JEAN TIERS"),
    ("Virement de : TITOUAN TIERS", "Titouan Tiers"),
    ("PAIEMENT ENVOYÉ PAR MME TIERS", "Mme Tiers"),
    ("CROUSTI PAIN\\CLERMONT FERR\\ FR", "Crousti Pain"),
    ("Vinted", "Vinted"),
    ("12345", "12345"),
    # Nothing left once the noise is gone: the label as it came.
    ("CARTE 01/03/26 \\PARIS\\ FR", "Carte 01/03/26 \\Paris\\ Fr"),
])
def test_a_label_is_named_without_its_bank_noise(label: str, name: str):
    assert display_label(label) == name


def test_a_changing_reference_keeps_one_key():
    june = "VIR SEPA EMPLOYEUR & CIE SALAIRE DE 2026-06 402147-1 Réf ZZ1KQCU8WQC5OZZ7OWGDQFCIK"
    july = "VIR SEPA EMPLOYEUR & CIE SALAIRE DE 2026-07 402147-1 Réf ZZ1KWVXDWRC9GJPBAZZ1KWVXDX4JPFVFQ"
    assert group_key(june, frozenset()) == group_key(july, frozenset())


def test_words_common_on_the_side_are_not_part_of_the_key():
    common = frozenset({"carte", "cb"})
    assert group_key("CARTE 01/03/26 CARREFOUR ANNECY CB*08", common) == "annecy carrefour"


def test_a_label_of_common_words_only_keys_on_its_signature():
    assert group_key("CARTE CB", frozenset({"carte", "cb"})) == "carte cb"


def test_merchants_without_a_word_of_two_letters_keep_apart():
    common = frozenset({"carte", "cb"})
    hl = group_key("CARTE 29/08/26 H&L 2 CB*0837", common)
    assert hl == group_key("CARTE 02/09/26 H&L 2 CB*0837", common)
    assert hl != group_key("CARTE 12/03/25 A.R.E.A. CB*0837", common)
    assert group_key("CARTE 01/02/26 O2 CB*0837", common) == "o2"


def test_a_group_is_named_after_its_most_frequent_label_then_its_latest():
    assert group_name([
        (date(2026, 1, 5), "CARTE 04/01/26 CARREFOUR CB*08"),
        (date(2026, 2, 5), "CARTE 04/02/26 CARREFOUR CB*08"),
        (date(2026, 3, 5), "Carrefour Market"),
    ]) == "Carrefour"
    assert group_name([
        (date(2026, 1, 5), "CARTE 04/01/26 CARREFOUR CB*08"),
        (date(2026, 3, 5), "Carrefour Market"),
    ]) == "Carrefour Market"


def test_bank_plumbing_never_names_a_counterpart():
    # Only the way it was sent differs: one counterpart, not two.
    assert group_key("PRLV SEPA EDF", frozenset()) == group_key("VIR SEPA EDF", frozenset())
    # "TDF EMIS VIA CB" sits on few enough labels to escape the common words,
    # and used to leave two shops sharing three quarters of their key.
    assert group_key("TDF EMIS VIA CB Revolut", frozenset()) != group_key("TDF EMIS VIA CB Lydia", frozenset())


def test_spellings_of_one_counterpart_merge_into_the_most_frequent():
    groups = [
        ("cie salaire vilmorin", frozenset({"cie", "salaire", "vilmorin"}), 3),
        ("cie vilmorin", frozenset({"cie", "vilmorin"}), 9),
        ("annecy carrefour", frozenset({"annecy", "carrefour"}), 93),
        ("annecy", frozenset({"annecy"}), 7),
        ("h&l", frozenset(), 2),
        ("a.r.e.a.", frozenset(), 4),
    ]
    into = merge_similar(groups)

    assert into["cie salaire vilmorin"] == "cie vilmorin"
    # Half the words shared is not enough: a town is not a shop.
    assert into["annecy"] == "annecy"
    assert into["annecy carrefour"] == "annecy carrefour"
    # Groups no word names merge with nothing, each other included.
    assert into["h&l"] == "h&l"
    assert into["a.r.e.a."] == "a.r.e.a."


def test_three_words_in_five_are_enough():
    # Exactly the threshold a nearby label is measured by.
    groups = [("a b c", frozenset({"a", "b", "c"}), 4), ("a b c d e", frozenset("abcde"), 1)]

    assert merge_similar(groups)["a b c d e"] == "a b c"


def test_merging_carries_over_a_chain_of_spellings():
    groups = [
        ("a b", frozenset({"a", "b"}), 1),
        ("a b c", frozenset({"a", "b", "c"}), 5),
        ("a b c d", frozenset({"a", "b", "c", "d"}), 2),
    ]
    into = merge_similar(groups)

    assert into == {"a b": "a b c", "a b c": "a b c", "a b c d": "a b c"}
