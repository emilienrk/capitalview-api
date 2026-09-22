"""
Merchant identity (services/banking/merchants.py): one merchant for the labels a
bank writes for the same payee over the years.

Labels are real ones, taken from a user's operations.
"""
import random

import pytest

from services.banking.merchants import (
    Idf,
    group_merchants,
    merchant_words,
    same_merchant,
    words_alike,
)

# Other labels of a history, so that word weights read as they do on a real one.
HISTORY = [
    "CARTE 01/03/26 CARREFOUR ANNECY CB*0837",
    "VIR INST ROUKINE EMILIEN",
    "CARTE 12/03/26 LIDL 1234 CB*0837",
    "PRLV SEPA MACIF Production-MACIF",
    "CARTE 03/03/26 SNCF CB*0837",
    "CARTE 04/03/26 MCDONALDS CB*0837",
    "VIR SEPA VILMORIN & CIE SALAIRE",
    "CARTE 05/03/26 BURGER KING CB*0837",
    "PRLV SEPA ORANGE SA",
]


def _grouped(*labels: str) -> dict[str, int]:
    keys = {label: merchant_words(label) for label in [*labels, *HISTORY]}
    groups = group_merchants(keys.values())
    return {label: groups[keys[label]] for label in labels}


@pytest.mark.parametrize("labels", [
    # A new bank format appends the client number.
    ("PRLV SEPA EDF clients particuliers",
     "PRLV SEPA EDF clients particuliers ROUKINE EMILIEN Numero de client : 602600413 1526162A1I1G1SD"),
    # A reference, a legal form spelled two ways, instant and SEPA transfers.
    ("VIR SEPA TRANSALP'DOME S.A.S.",
     "VIR SEPA TRANSALP'DOME S.A.S. Virement pour le loyer de Emilien R Réf : SCT40618202608030071849",
     "VIR INST TRANSALP DOME S A S"),
    # Three means of payment, a legal form, an escaped HTML entity.
    ("To Sarl Olness'", "CARTE 08/11/23 OLNESS' CB*8897", 'PRLV SEPA OLNESS-OLNESS.apos"'),
    ("VIR INST FREDERIC DURAND", "VIR SEPA Frederic Durand"),
    # A label that grew a word.
    ("CARTE 13/03/26 ANTHROPIC CB*0837", "CARTE 02/07/26 ANTHROPIC* CLAUDE CB*0837"),
    # A label the bank cut short.
    ("EA *ELECTRONIC AR", "EA *ELECTRONIC ARTS"),
])
def test_the_spellings_of_one_merchant_are_one_merchant(labels):
    assert len(set(_grouped(*labels).values())) == 1


@pytest.mark.parametrize("labels", [
    # A prefix too short a share of the longer word.
    ("CARTE TOTAL 4", "PRLV SEPA TotalEnergies Electricite et Gaz"),
    # "com" names the merchant; "air" alone is another one.
    ("PRLV SEPA COM AIR", "CARTE AIR FRANCE"),
    ("CARTE PATHE ANNECY 2", "PRLV SEPA Pathe CinePass"),
    # A city never swallows the shop named after it.
    ("ANNECY", "CARREFOUR ANNECY"),
    # Renames without a shared word are left to the series (recurrence.py).
    ("OVH SAS", "OVHcloud"),
    ("ELECTRICITE DE FRANCE", "EDF clients particuliers"),
])
def test_different_merchants_stay_apart(labels):
    assert len(set(_grouped(*labels).values())) == len(labels)


def test_bank_plumbing_legal_forms_and_references_are_not_words():
    assert merchant_words("PRLV SEPA OLNESS-OLNESS.apos\" SARL RUM MA02 Réf : SCT406") == ("olness",)
    assert merchant_words("CARTE 02/07/26 Électricité CB*0837") == ("electricite",)


def test_a_word_every_label_holds_weighs_less_than_a_rare_one():
    keys = [("roukine", "edf"), ("roukine", "macif"), ("roukine", "orange")]
    idf = Idf(keys)
    assert idf("roukine") < idf("edf")


def test_words_every_label_shares_do_not_make_one_merchant():
    # Three words of four in common would pass for one merchant if every word
    # weighed the same.
    keys = [(*"roukine emilien paul".split(), name) for name in ("edf", "macif", "orange", "lidl")]
    assert not same_merchant(keys[0], keys[1], Idf(keys))


def test_a_label_without_words_is_only_ever_itself():
    assert merchant_words("H&L") == ("#h&l",)
    assert merchant_words("H&L 2") == merchant_words("H&L")
    # One letter apart, as a typo would be: still two labels.
    keys = [merchant_words("A.R.E.A."), merchant_words("A.R.E.B.")]
    groups = group_merchants(keys)
    assert groups[keys[0]] != groups[keys[1]]


def test_truncation_and_typos():
    assert words_alike("electronic", "electroni")
    assert words_alike("transalp", "transapl")
    assert not words_alike("total", "totalenergies")
    assert not words_alike("ovh", "ovhcloud")
    # Four letters: too short to call a swap a typo.
    assert not words_alike("lidl", "lild")


def test_groups_do_not_depend_on_the_order_labels_come_in():
    labels = [
        "PRLV SEPA EDF clients particuliers", "VIR INST TRANSALP DOME S A S", "VIR SEPA TRANSALP'DOME S.A.S.",
        "CARTE 13/03/26 ANTHROPIC CB*0837", "CARTE 02/07/26 ANTHROPIC* CLAUDE CB*0837", *HISTORY,
    ]
    keys = [merchant_words(label) for label in labels]
    expected = group_merchants(keys)
    shuffled = list(keys)
    for seed in range(5):
        random.Random(seed).shuffle(shuffled)
        assert group_merchants(shuffled) == expected
