"""Quick smoke test for the Binance CSV parser."""
from services.imports.binance import generate_preview

csv = (
    '"User_ID","UTC_Time","Account","Operation","Coin","Change","Remark"\n'
    '"1","2025-04-14 12:07:14","Spot","Deposit","EUR","14.70",""\n'
    '"1","2025-04-14 12:08:33","Spot","Buy Crypto With Fiat","EUR","-10.00",""\n'
    '"1","2025-04-14 12:08:33","Spot","Buy Crypto With Fiat","ETH","0.00677440",""\n'
    '"1","2025-04-14 12:28:41","Funding","Crypto Box","BNB","0.00011233",""\n'
    '"1","2025-04-14 12:29:54","Funding","Binance Convert","ETH","0.00003952",""\n'
    '"1","2025-04-14 12:29:54","Funding","Binance Convert","BNB","-0.00011233",""\n'
    '"1","2025-04-14 17:10:28","Spot","Deposit","BTC","0.00122343",""\n'
    '"1","2025-11-26 15:54:36","Spot","Withdraw","BTC","-0.00000068",""\n'
    '"1","2025-12-08 19:24:33","Spot","Transaction Buy","BTC","0.00055",""\n'
    '"1","2025-12-08 19:24:33","Spot","Transaction Spend","USDC","-49.489",""\n'
    '"1","2025-12-08 19:24:33","Spot","Transaction Fee","BTC","-0.00000052",""\n'
    '"1","2025-12-06 08:18:41","Spot","Transaction Sold","EUR","-200.00",""\n'
    '"1","2025-12-06 08:18:41","Spot","Transaction Fee","USDC","-0.221",""\n'
    '"1","2025-12-06 08:18:41","Spot","Transaction Revenue","USDC","232.62",""\n'
)

r = generate_preview(csv)
print(f"Groups={r.total_groups}  Rows={r.total_rows}  NeedEur={r.groups_needing_eur}")
print()
for g in r.groups:
    eur_info = f"auto={g.auto_eur_amount}" if g.has_eur else (
        f"hint_usdc={g.hint_usdc_amount}" if g.needs_eur_input else "n/a"
    )
    print(
        f"  #{g.group_index + 1} {g.timestamp}  [{g.summary}]  "
        f"eur={g.has_eur} need={g.needs_eur_input}  {eur_info}"
    )
    for row in g.rows:
        print(f"    {row.mapped_type:15s} {row.mapped_symbol:6s} {row.mapped_amount}")
