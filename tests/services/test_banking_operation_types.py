"""
The operation type read from a label (services/banking/operation_types.py).

Labels are shaped like the real ones, names replaced.
"""
import pytest
import sqlalchemy as sa
from sqlmodel import Session, select

from dtos.banking import OperationType as Type
from models.banking import BankTransaction
from services.banking.flows import list_month_transactions, transfer_patterns
from services.banking.operation_types import operation_type
from services.encryption import decrypt_data, encrypt_data
from tests.services.test_banking_flows import ACCOUNT_A, USER, _link, _raw, _store


@pytest.mark.parametrize("label, expected", [
    # Boursorama
    ("CARTE 21/06/26 CARREFOUR ANNECY CB*0837", Type.CARD),
    ("TDF EMIS VIA CB 12/03/25 Revolut**6361*", Type.CARD),
    ("AVOIR 02/05/24 Zalando Payments  CB*0837", Type.CARD),
    ("PRLV SEPA EDF clients particuliers", Type.DIRECT_DEBIT),
    ("REJ PRLV SEPA COM AIR", Type.DIRECT_DEBIT),
    ("VIR INST MARTIN JEANNE", Type.TRANSFER),
    ("VIR SEPA EMPLOYEUR & CIE", Type.TRANSFER),
    ("REJ VIR INST MARTIN JEANNE", Type.TRANSFER),
    ("RETRAIT DAB 14/07/25 ANNECY      CB*0837", Type.WITHDRAWAL),
    ("*INTER.BRUTS 2025", Type.INTEREST),
    ("*INTERETS DEBITEURS", Type.INTEREST),
    # Revolut
    ("To Jeanne Martin", Type.TRANSFER),
    ("Virement de : Jean Tiers", Type.TRANSFER),
    ("Paiement envoyé par Jean Tiers", Type.TRANSFER),
    ("Retrait d'espèces à Annecy", Type.WITHDRAWAL),
    ("Carrefour", Type.UNKNOWN),
    # Neither
    ("Prime Parrainage", Type.UNKNOWN),
    ("AVOIR 02/05/24 BlaBlaCar", Type.UNKNOWN),
    ("", Type.UNKNOWN),
    (None, Type.UNKNOWN),
])
def test_a_label_reads_as_its_type(label, expected):
    assert operation_type(label) is expected


@pytest.mark.parametrize("label", [
    "LIBRAIRIE CARTE BLANCHE",
    "SNCF VIREMENT",
    "Toto Pizza",
    "BAR LE RETRAIT",
    "INTERMARCHE",
])
def test_a_keyword_away_from_the_start_says_nothing(label):
    assert operation_type(label) is Type.UNKNOWN


def _stored_types(session: Session, master_key: str) -> list[str | None]:
    return [
        decrypt_data(row.operation_type_enc, master_key) if row.operation_type_enc else None
        for row in session.exec(select(BankTransaction)).all()
    ]


def test_a_stored_row_carries_its_type(session: Session, master_key: str):
    _link(session, master_key, ACCOUNT_A)
    _store(session, master_key, ACCOUNT_A, _raw("12.00", "DBIT", "2026-03-05", ref="r1", label="PRLV SEPA ORANGE SA"))

    assert _stored_types(session, master_key) == ["DIRECT_DEBIT"]


def test_the_rebuild_gives_a_row_without_type_its_own(session: Session, master_key: str):
    _link(session, master_key, ACCOUNT_A)
    _store(session, master_key, ACCOUNT_A, _raw("12.00", "DBIT", "2026-03-05", ref="r1", label="CARTE 04/03/26 BOULANGERIE CB*08"))
    for row in session.exec(select(BankTransaction)).all():
        row.operation_type_enc = None
        session.add(row)
    session.commit()

    transfer_patterns(session, USER, master_key)

    assert _stored_types(session, master_key) == ["CARD"]


def test_the_rebuild_corrects_a_type_the_lexicon_now_reads_otherwise(session: Session, master_key: str):
    _link(session, master_key, ACCOUNT_A)
    _store(session, master_key, ACCOUNT_A, _raw("12.00", "DBIT", "2026-03-05", ref="r1", label="RETRAIT DAB 04/03/26 ANNECY CB*08"))
    for row in session.exec(select(BankTransaction)).all():
        row.operation_type_enc = encrypt_data("UNKNOWN", master_key)
        session.add(row)
    session.commit()

    transfer_patterns(session, USER, master_key)

    assert _stored_types(session, master_key) == ["WITHDRAWAL"]


def test_a_row_the_rebuild_has_not_reached_reads_its_type_from_its_label(session: Session, master_key: str):
    _link(session, master_key, ACCOUNT_A)
    _store(session, master_key, ACCOUNT_A, _raw("12.00", "DBIT", "2026-03-05", ref="r1", label="CARTE 04/03/26 BOULANGERIE CB*08"))
    transfer_patterns(session, USER, master_key)
    # Kept at its timestamp, so the stored patterns stay current and nothing rebuilds.
    [row] = session.exec(select(BankTransaction)).all()
    session.exec(
        sa.update(BankTransaction)
        .where(BankTransaction.uuid == row.uuid)
        .values(operation_type_enc=None, updated_at=row.updated_at)
    )
    session.commit()

    [item] = list_month_transactions(session, USER, master_key, "2026-03").transactions

    assert item.operation_type is Type.CARD
    assert _stored_types(session, master_key) == [None]
