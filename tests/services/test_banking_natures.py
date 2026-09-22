"""
What a recurring payment is for, guessed from its merchant's words
(services/banking/natures.py).
"""
import pytest

from dtos.banking import RecurringNature
from services.banking.merchants import merchant_words
from services.banking.natures import guess, of


@pytest.mark.parametrize(
    "label,expected",
    [
        ("PRLV SEPA EDF clients particuliers", RecurringNature.ENERGY),
        ("PRLV SEPA MACIF Production-MACIF", RecurringNature.INSURANCE),
        ("VIR SEPA TRANSALP'DOME S.A.S. Virement pour le loyer de Emilien R", RecurringNature.HOUSING),
        ("PRLV Fitness Park Clermont-Ferrand - Le Brezet", RecurringNature.SPORT),
        ("CARTE ANTHROPIC* CLAUDE CB*0837", RecurringNature.SOFTWARE),
        ("PRLV SEPA Bouygues Telecom", RecurringNature.TELECOM),
        ("CARTE SUPERCELL STORE", RecurringNature.LEISURE),
        ("PRLV SEPA COFIDIS pret personnel", RecurringNature.CREDIT),
        ("PRLV SEPA SNCF Abonnement", RecurringNature.TRANSPORT),
    ],
)
def test_the_merchant_says_what_the_payment_is_for(label: str, expected: RecurringNature):
    assert guess(merchant_words(label)) == expected


def test_the_user_s_answer_wins_and_an_answer_nobody_knows_falls_back():
    words = merchant_words("PRLV SEPA EDF clients particuliers")
    assert of("housing", words) is RecurringNature.HOUSING
    assert of(None, words) is RecurringNature.ENERGY
    # A nature dropped from the list must not make the payment unreadable.
    assert of("timeshare", words) is RecurringNature.ENERGY


def test_a_merchant_nobody_recognises_is_left_to_the_user():
    assert guess(merchant_words("PRLV SEPA OLNESS-OLNESS")) is RecurringNature.OTHER
    assert guess(()) is RecurringNature.OTHER


def test_a_filling_station_is_not_an_energy_bill():
    # `total` is a filling station, `totalenergies` the supplier: only the
    # whole word tells, so a fuel habit marked by hand stays unclassified.
    assert guess(merchant_words("CARTE TOTAL ACCES CB*8897")) is RecurringNature.OTHER
    assert guess(merchant_words("PRLV SEPA TOTALENERGIES ELECTRICITE")) is RecurringNature.ENERGY


def test_the_first_word_that_tells_wins():
    # Arverne and Brezet mean nothing; the gym in the middle does.
    assert guess(("arverne", "fitness", "brezet")) is RecurringNature.SPORT
    # A label naming two of them is read like the bank prints it, left to
    # right: an insurance taken out on a flat is still housing.
    assert guess(("loyer", "assurance")) is RecurringNature.HOUSING
    assert guess(("assurance", "loyer")) is RecurringNature.INSURANCE
