"""
What a recurring payment is for (services/banking/natures.py): the user's own
answer, never a guess.
"""
from dtos.banking import RecurringNature
from services.banking.natures import of


def test_nothing_is_read_until_the_user_files_it():
    assert of(None) is None
    assert of("") is None


def test_what_the_user_filed_is_what_comes_back():
    assert of("housing") is RecurringNature.HOUSING
    assert of("software") is RecurringNature.SOFTWARE


def test_a_nature_this_version_no_longer_knows_reads_as_unfiled():
    # Dropping a nature from the enum must leave the payment readable.
    assert of("timeshare") is None
