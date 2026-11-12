"""How a label is read into words (services/banking/labels.py).

Labels are shaped like the real ones, names replaced.
"""
import pytest

from services.banking.labels import label_signature, label_words


@pytest.mark.parametrize("label, words", [
    ("VIR SEPA VILMORIN REF ZZ1L2ZJSYU78NB5", {"vir", "sepa", "vilmorin", "ref"}),
    ("CARTE 03/08/25 AIRBNB * HMFYWK53 CB*0837", {"carte", "airbnb", "cb"}),
    ("Paiement envoyé par CAF", {"paiement", "envoye", "par", "caf"}),
])
def test_a_label_reads_as_its_whole_words(label: str, words: set[str]):
    assert label_words(label) == words


def test_a_reference_changing_every_month_keeps_one_signature():
    assert label_signature("VIR SEPA VILMORIN REF ZZ1L2ZJSYU78NB5") == label_signature("VIR SEPA VILMORIN REF QX7PT2MM0LK3")


def test_case_and_accents_make_no_other_signature():
    assert label_signature("Géant Casino") == label_signature("GEANT CASINO")


def test_a_label_without_words_keys_on_its_whole_text():
    assert label_signature("00160669") == "#00160669"
    assert label_signature("H&L") == label_signature("h&l") == "#h l"
    assert label_signature("00160669") != label_signature("00160670")


def test_a_label_holding_neither_a_letter_nor_a_digit_has_no_signature():
    assert label_signature(" - ") is None
    assert label_signature(None) is None
