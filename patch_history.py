import re

with open("/Users/emilien/Dev/perso/capitalview/backend/services/account_history.py", "r") as f:
    lines = f.readlines()

out_lines = []
in_func = False

for line in lines:
    if line.startswith("def _generate_missing_snapshots("):
        in_func = True
        out_lines.append(line)
        # Append the new function body
        out_lines.append("""\
    user_uuid_bidx: str,
    account_id_bidx: str,
    account_snapshot: _AccountSnapshot,
    price_matrix: dict[str, dict[date, Decimal]],
    missing_dates: list[date],
    prev_value: Decimal,
    master_key: str,
    has_previous_snapshot: bool = True,
) -> list[dict]:
    \"\"\"
    Generate one encrypted row dict per missing date.
    Returned rows are ready for a bulk `pg_insert(AccountHistory).values(rows)`.
    \"\"\"
    now = datetime.now(timezone.utc)
    rows: list[dict] = []

    is_exact_mode = bool(account_snapshot.transactions)
    txs = account_snapshoimport re

with open("/Users/emilien/Dev/perso/capitalview/backend/services/TO
with op  f    lines = f.readlines()

out_lines = []
in_func = False

for line in lines:
    if line.startou
out_lines = []
in_func ry.in_func = Fal  
for line in lsto    if line.startpo        in_func = True
        out_lines.append(line)
     f        out_lines.app B        # Append the new funcqu        out_lines.append("""\
    use}
    user_uuid_bidx: str,
         account_id_bidx: stou    account_snapshot: _Aed    price_matrix: dict[str, dict[date,po    missing_dates: list[date],
    prev_value: D"q    prev_value: Decimal,
    ed    master_key: str,
        has_previous_snbu) -> list[dict]:
    \"\"\"
    Generrre    \"\"\"
          Generen    Returned rows are ready for a bulk `pg_insert(Aces    : Decimal("0")}

    symbols = list(price_matrix.keys())

    for day_index,     now =er    rows: list[dict] = []

    is_eun
    is_exact_mode = booin     txs = account_snapshoimport re

with open("/Users/  
with open("/Users/emilien/Dev/pericwith op  f    lines = f.readlines()

out_lines = []
in_func = Falsri
out_lines = []
in_func = False

fmalin_func = Fal  
for line in lm i    if line.start  out_lines = []
in_ Ain_func ry.inR for line in lsto    if liic        out_lines.append(line)
     f        out_lines.aom     f        out_lines.app Bcc    use}
    user_uuid_bidx: str,
         account_id_bidx: stou    account_snapshot: _Ato    usnt_summary(
                    prev_value: D"q    prev_value: Decimal,
    ed    master_key: str,
        has_previous_snbu) -> list[dict]:
    \"\"\"   a    ed    master_key: str,
        has_prees        has_previous_snbu      \"\"\"
    Generrre    \"\"\"
      su    Generet          Generen    y(
    symbols = list(price_matrix.keys())

    for day_index,     now =er    rows: list[di   
    for day_index,     now =er    rowd_p
    is_eun
    is_exact_mode = booin     txs = accoun       is_exal
with open("/Users/  
with open("/Users/emilien/Dev/pericwicurwith open("/Users/ema
out_lines = []
in_func = Falsri
out_lines = []
in_func = False

fmalosiin_func = Fal  out_lines = []
n in_func = Falio
fmal            for line in lm i  moin_ Ain_func ry.inR for line in lsto    if liic ec     f        out_lines.aom     f        out_lines.app Bcc    use}
    user_ p.total_invested
                
                if total_value > D         account_id_bid                      prev_value: D"q    prev_value: Decimal,
    ed    ma      ed    master_key: str,
        has_previous_snbu) -> l          has_previous_snbu      \"\"\"   a    ed    master_key: str,          has_prees        has_previous_s      Generrre    \"\"\"
      su    Generet          G":      su    Generet  ,
    symbols = list(price_matrix.keys())

ic
    for day_index,     now =er    row
      for day_index,     now =er    rowd_p
    is_e 2    is_eun
    is_exact_mode = booin   tr    is_exrcwith open("/Users/  
with open("/Users/emilien/Dev/perionwith open("/Users/ehoout_lines = []
in_func = Falsri
out_lines = []
in_func = Falccin_func = Fal.aout_nt_type == Acin_func = Fal.B
fmalosiin_fun   n in_func = Falio
fmal            ftyfmal           tio    user_ p.total_invested
                
                if total_value > D         account_id_bid                      prev_value: D"q    prev_valuls                
        ac               fr    ed    ma      ed    master_key: str,
        has_previous_snbu) -> l          has_previous_snbu      \"\"\"   a  sn        has_previous_snbu) -> l        ed      su    Generet          G":      su    Generet  ,
    symbols = list(price_matrix.keys())

ic
    for day_index,     now =er    row
      for day_index,     no      symbols = list(price_matrix.keys())

ic
    for d  
ic
    for day_index,     now =er                for day_index,     now =er          is_e 2    is_eun
    is_exact_mode =       is_exact_mode =unwith open("/Users/emilien/Dev/perionwith open("/Users/ehoout  in_func = Falsri
out_lines = []
in_func = Falccin_func = Fal.aout_nt_t  out_lines = []
10in_func = Fal  fmalosiin_fun   n in_func = Falio
fmal            ftyfmal    sifmal      snapshot_positions else                 
                if total_value > D         aceg                         ac               fr    ed    ma      ed    master_key: str,
        has_previous_snbu) -> l          has_previous_snr         has_previous_snbu) -> l          has_previous_snbu      \"as    symbols = list(price_matrix.keys())

ic
    for day_index,     now =er    row
      for day_index,     no      symbols = list(price_matrix.keys())

ic
    for d  
ic
  la
ic
    for day_index,     now =er    set in      for day_index,     no      sylua
ic
    for d  
ic
    for day_index,     now =er                foval   ic
    for    c    is_exact_mode =       is_exact_mode =unwith open("/Users/emilien/Dev/perionwith open("/Users/ymout_lines = []
in_func = Falccin_func = Fal.aout_nt_t  out_lines = []
10in_func = Fal  fmalosiin_fun   n in_func = Falio  in_f"price": cu10in_func = Fal  fmalosiin_fun   n in_func = Falio
fmstfmal            ftyfmal    sifmal      snapshot_pti                if total_value > D         aceg                         ac    "v        has_previous_snbu) -> l          has_previous_snr         has_previous_snbu) -> l          has_previous_snbu      \"as    
 
ic
    for day_index,     now =er    row
      for day_index,     no      symbols = list(price_matrix.keys())

ic
    for d  
ic
  la
ic
    for day_index,     now            for day_index,     no      sym  
ic
    for d  
ic
  la
ic
    for day_index,     now =er    set in      ic
  la
icrc ntic
": stic
    for d  
ic
    for day_index,     now =er                foval   ic
  .d mpic
    for_p si    for f snapshot_positions else None

        if day_inin_func = Falccin_func = Fal.aout_nt_t  out_lines = []
10in_func = Fal  fmalosiin_fun   n in_func = Falio  in_f"price": cu10er10in_func = Fal  fmalosiin_fun   n in_func = Falio  i
 fmstfmal            ftyfmal    sifmal      snapshot_pti                if total_value > D         aceg               " 
ic
    for day_index,     now =er    row
      for day_index,     no      symbols = list(price_matrix.keys())

ic
    for d  
ic
  la
ic
    for day_index,     now            for day_index,     no      sym  
ic
    for d  
ic
  la
ic
    for day_index,     now =             for day_index,     no      symta
ic
    for d  
ic
  la
ic
    for day_index,     now            fonl_encic
  la
ic_d taic
r( ouic
    for d  
ic
  la
ic
    for day_index,     now =er    set in   da a(ic
  la
icjs n,ic
st r_  la
icrc ntic
": stic
    for d  
ic
    for dcricred": stic
w,    fo  ic
    forted_at": now,
        }
        rows.append(row)
        prev_value = to
        if day_inin_func = Falccin_func = Fal.ao   10in_func = Fal  fmalosiin_fun   n in_func = Falio  in_f"price": cu10e-- fmstfmal            ftyfmal    sifmal      snapshot_pti                if total_value > D         aceg               " 
ic  ic
    for day_index,     now =er    row
      for day_index,     no      symbols = list(price_matrixcapitalview/backend/ser      for day_index,     no      sym:
    f.writelines(out_lines)

