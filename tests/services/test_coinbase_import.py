import textwrap

from services.imports.coinbase import generate_preview

COINBASE_CSV = textwrap.dedent("""\
    "You can use this transaction report to inform your likely tax obligations."

    "ID","Timestamp","Transaction Type","Asset","Quantity Transacted","Price Currency","Price at Transaction","Subtotal","Total (inclusive of fees and/or spread)","Fees and/or Spread","Notes"
    "aaa","2024-01-10T09:00:00Z","Buy","BTC","0.00250000","EUR","40000.00","€100.00","€101.49","€1.49","Bought 0.0025 BTC for €101.49 EUR"
    "bbb","2024-02-05T10:00:00Z","Sell","BTC","0.00100000","EUR","45000.00","€45.00","€44.01","€0.99","Sold 0.001 BTC for €44.01 EUR"
    "ccc","2024-03-01T11:00:00Z","Rewards Income","SOL","0.05000000","EUR","90.00","€4.50","€4.50","€0.00","Received 0.05 SOL from Coinbase Rewards"
    "ddd","2024-03-15T12:00:00Z","Receive","ETH","0.20000000","EUR","2800.00","","","","Received 0.2 ETH from an external account"
    "eee","2024-04-01T13:00:00Z","Send","BTC","0.00050000","EUR","55000.00","","","","Sent 0.0005 BTC to an external account"
    "fff","2024-04-20T14:00:00Z","Convert","USDC","50.00000000","EUR","0.92","€46.00","€46.00","€0.46","Converted 50 USDC to 0.0008 BTC"
""")


def test_coinbase_preview_skips_preamble_and_maps_types():
    preview = generate_preview(COINBASE_CSV)
    assert preview.total_groups == 6
    groups = preview.groups

    # Buy: BUY BTC + SPEND EUR (subtotal) + FEE EUR
    g_buy = groups[0]
    types = [r.mapped_type for r in g_buy.rows]
    assert types.count("BUY") == 1 and types.count("SPEND") == 1 and types.count("FEE") == 1
    spend = next(r for r in g_buy.rows if r.mapped_type == "SPEND")
    assert spend.mapped_asset_key == "EUR"
    assert spend.mapped_amount == 100.0
    assert g_buy.has_eur and not g_buy.needs_eur_input

    # Sell: SPEND BTC + DEPOSIT EUR (total) + FEE
    g_sell = groups[1]
    deposit = next(r for r in g_sell.rows if r.mapped_type == "DEPOSIT")
    assert deposit.mapped_amount == 44.01

    # Rewards Income: REWARD, no EUR anchor needed
    g_reward = groups[2]
    assert g_reward.rows[0].mapped_type == "REWARD"
    assert not g_reward.needs_eur_input

    # Receive: BUY price 0, needs EUR anchor
    g_receive = groups[3]
    assert g_receive.rows[0].mapped_type == "BUY"
    assert g_receive.needs_eur_input

    # Send: TRANSFER, no anchor needed
    g_send = groups[4]
    assert g_send.rows[0].mapped_type == "TRANSFER"
    assert not g_send.needs_eur_input

    # Convert: SPEND USDC + BUY BTC (parsed from Notes) + FEE EUR
    g_convert = groups[5]
    conv_types = {r.mapped_type: r for r in g_convert.rows}
    assert conv_types["SPEND"].mapped_asset_key == "USDC"
    assert conv_types["BUY"].mapped_asset_key == "BTC"
    assert conv_types["BUY"].mapped_amount == 0.0008
    assert conv_types["FEE"].mapped_asset_key == "EUR"


def test_coinbase_non_eur_currency_needs_anchor():
    csv_content = textwrap.dedent("""\
        "ID","Timestamp","Transaction Type","Asset","Quantity Transacted","Price Currency","Price at Transaction","Subtotal","Total (inclusive of fees and/or spread)","Fees and/or Spread","Notes"
        "aaa","2024-01-10T09:00:00Z","Buy","BTC","0.00250000","USD","43000.00","$107.50","$109.00","$1.50","Bought BTC"
    """)
    preview = generate_preview(csv_content)
    assert preview.total_groups == 1
    g = preview.groups[0]
    # Only the BUY leg — no silent USD->EUR conversion
    assert [r.mapped_type for r in g.rows] == ["BUY"]
    assert g.needs_eur_input


def test_coinbase_not_a_coinbase_file():
    preview = generate_preview("a,b,c\n1,2,3")
    assert preview.total_groups == 0
