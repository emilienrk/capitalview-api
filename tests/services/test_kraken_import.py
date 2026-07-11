import textwrap

from services.imports.kraken import generate_preview, normalize_kraken_asset

KRAKEN_CSV = textwrap.dedent("""\
    "txid","refid","time","type","subtype","aclass","asset","amount","fee","balance"
    "L1","R1","2024-01-10 09:00:00","deposit","","currency","ZEUR","500.0000","0.0000","500.0000"
    "L2","R2","2024-01-15 10:30:00","trade","","currency","ZEUR","-100.0000","0.2600","399.7400"
    "L3","R2","2024-01-15 10:30:00","trade","","currency","XXBT","0.0025000000","0.0000000000","0.0025000000"
    "L4","R3","2024-02-01 08:00:00","staking","","currency","DOT.S","1.5000000000","0.0000000000","1.5000000000"
    "L5","R4","2024-02-10 12:00:00","withdrawal","","currency","XXBT","-0.0010000000","0.0000500000","0.0014500000"
    "L6","R5","2024-03-01 15:00:00","deposit","","currency","XETH","0.5000000000","0.0000000000","0.5000000000"
""")


def test_normalize_kraken_asset():
    assert normalize_kraken_asset("ZEUR") == "EUR"
    assert normalize_kraken_asset("XXBT") == "BTC"
    assert normalize_kraken_asset("XBT") == "BTC"
    assert normalize_kraken_asset("XETH") == "ETH"
    assert normalize_kraken_asset("DOT.S") == "DOT"
    assert normalize_kraken_asset("ETH2.S") == "ETH"
    assert normalize_kraken_asset("SOL") == "SOL"
    assert normalize_kraken_asset("XTZ") == "XTZ"  # real symbol, not stripped


def test_kraken_preview_groups_by_refid():
    preview = generate_preview(KRAKEN_CSV)
    assert preview.total_groups == 5

    by_summary = {g.group_index: g for g in preview.groups}

    # R1: EUR deposit
    g1 = by_summary[0]
    assert g1.has_eur
    assert not g1.needs_eur_input
    assert g1.rows[0].mapped_type == "DEPOSIT"
    assert g1.rows[0].mapped_asset_key == "EUR"

    # R2: trade EUR -> BTC, both legs + fee in one group, EUR anchor auto
    g2 = by_summary[1]
    types = {r.mapped_type for r in g2.rows}
    assert types == {"BUY", "SPEND", "FEE"}
    assert g2.has_eur
    # EUR out includes the EUR fee row (100 + 0.26)
    assert g2.eur_amount == 100.26
    keys = {r.mapped_asset_key for r in g2.rows}
    assert keys == {"BTC", "EUR"}

    # R3: staking reward, no EUR needed
    g3 = by_summary[2]
    assert g3.rows[0].mapped_type == "REWARD"
    assert g3.rows[0].mapped_asset_key == "DOT"
    assert not g3.needs_eur_input

    # R4: BTC withdrawal -> TRANSFER + FEE (transfer-only group, no EUR needed)
    g4 = by_summary[3]
    types4 = [r.mapped_type for r in g4.rows]
    assert "TRANSFER" in types4 and "FEE" in types4

    # R5: crypto deposit -> BUY price 0, needs EUR anchor
    g5 = by_summary[4]
    assert g5.rows[0].mapped_type == "BUY"
    assert g5.rows[0].mapped_asset_key == "ETH"
    assert g5.needs_eur_input


def test_kraken_skips_internal_staking_transfers():
    csv_content = textwrap.dedent("""\
        "txid","refid","time","type","subtype","aclass","asset","amount","fee","balance"
        "L1","R1","2024-01-10 09:00:00","transfer","spottostaking","currency","DOT","-5.0","0.0","0.0"
        "L2","R2","2024-01-10 09:00:05","transfer","stakingfromspot","currency","DOT.S","5.0","0.0","5.0"
    """)
    preview = generate_preview(csv_content)
    assert preview.total_groups == 0


def test_kraken_empty_csv():
    preview = generate_preview("not,a,kraken,file\n1,2,3,4")
    assert preview.total_groups == 0
