# Le registre d'un compte crypto

## La règle

**Une ligne stockée est une jambe ; une opération est un groupe.**

Un compte crypto ne stocke ni positions, ni solde, ni prix de revient : il
stocke des lignes atomiques (`crypto_transactions`), et tout le reste — quantités,
PRU, investi, P/L latent et réalisé, liquidités, historique — se **rejoue** à
chaque lecture, dans l'ordre chronologique. Ce qu'a fait l'utilisateur (un
achat, un échange, une vente) n'existe que comme l'ensemble des lignes qui
partagent un `group_uuid`.

Tout est chiffré par la clé maître (`*_enc`) : type, actif, montant, prix, date.
Aucun filtre SQL ne peut lire une opération, donc tout se déchiffre et se trie
en mémoire. Restent en clair : `uuid`, `account_id_bidx` (index aveugle du
compte), `group_uuid` et `is_auto_provision`.

La référence de calcul est `get_crypto_account_summary`
(`services/crypto_transaction.py`). Tout ce qui rejoue le registre ailleurs doit
lui donner raison : l'historique (`services/account_history.py`), la courbe de
prix d'un actif (`services/market.py`, `_crypto_timeline_events`), les
statistiques communautaires (`services/community.py`), le PRU d'un transfert
(`_compute_asset_key_pru`) et le front (`capitalview-web/src/utils/cryptoOperations.ts`).

## Huit types atomiques

`CryptoTransactionType` (`models/enums.py`) :

| Type | Quantité | Prix de revient | Rôle |
| --- | --- | --- | --- |
| `BUY` | + | + coût du groupe | un actif arrive ; **ne porte jamais son prix** (`price_per_unit = 0`) |
| `REWARD` | + | 0 | staking, airdrop |
| `DEPOSIT` | + | — | euros (ou autre devise) qui arrivent |
| `SPEND` | − | − au prorata | un actif part dans un échange ou une vente |
| `TRANSFER` | − | − au prorata | envoi vers un portefeuille à soi, sans événement fiscal |
| `WITHDRAW` | − | − au prorata si crypto | sortie taxable, ou retrait d'euros vers la banque |
| `FEE` | − | inchangé | frais prélevés dans un actif ; valorisés à part (`fees_eur`) |
| `ANCHOR` | aucune | porte le coût du groupe | ligne EUR virtuelle, jamais comptée dans un solde |

Crédits et débits : `credit_types()` / `debit_types()`. `ANCHOR` n'est ni l'un
ni l'autre.

Une devise (`FIAT_ASSET_KEYS`, alias de `CURRENCY_CODES`, voir `currencies.md`)
est une liquidité, pas une position : elle va dans `cash_balance`, jamais dans
`current_value` ni `total_invested`.

## Le coût vient du groupe, pas de la ligne

Un `BUY` dit combien d'actif est arrivé, pas ce qu'il a coûté. Le coût est sur
une autre ligne du même groupe :

1. l'`ANCHOR` du groupe s'il y en a un ;
2. sinon le `SPEND` en devise du groupe.

Si les deux existent, l'`ANCHOR` gagne (`{**fiat_spend_by_group, **anchor_by_group}`).
Un `BUY` sans groupe garde son propre `amount × price_per_unit` : ce sont les
achats stockés avant les opérations composites.

Quand un groupe contient plusieurs `BUY` (un ordre exécuté en plusieurs fois),
`split_group_cost` (`services/crypto_group_cost.py`) partage le coût : au
prorata des quantités pour un même actif, à parts égales si les actifs
diffèrent, le reste d'arrondi sur la dernière ligne. Avant 2026-09-25, chaque
`BUY` réclamait le coût entier du groupe.

Lire un actif en filtrant les lignes sur lui jette donc le coût : c'est
l'erreur qui mettait tous les marqueurs de la courbe de prix à 0 € (corrigée le
2026-09-22). Toujours rejouer le registre entier.

## Les opérations composites

Le formulaire envoie une action (`CryptoCompositeTransactionType`) que
`create_composite_crypto_transaction` décompose. Toutes les lignes reçoivent le
même `group_uuid` et la même date.

| Action | Lignes écrites |
| --- | --- |
| `BUY` en euros | `BUY` + `SPEND EUR` (montant + frais en euros) |
| `BUY` crypto → crypto (échange) | `BUY` + `SPEND` de l'actif cédé + `ANCHOR` (valeur EUR de l'échange) |
| `BUY` avec frais crypto non inclus | + `ANCHOR` (montant + valeur des frais) + `FEE` |
| `CRYPTO_DEPOSIT` | `DEPOSIT EUR` (auto) + `BUY` + `SPEND EUR` |
| `SELL_TO_FIAT` | `SPEND` de l'actif + `DEPOSIT` de la devise (auto) + `FEE` éventuel |
| `FIAT_DEPOSIT` / `FIAT_WITHDRAW` | `DEPOSIT EUR` / `WITHDRAW EUR` |
| `REWARD` | `REWARD` |
| `TRANSFER`, `EXIT` | `TRANSFER` (+ `FEE` pour `EXIT`) |
| `FEE` | `FEE` |

Un montant EUR absent est déduit du prix en base au jour de l'opération. Un
`FEE` est valorisé à sa création (`_fee_price_per_unit`, prix en cache, 0 s'il
manque) et ce prix ne bouge plus. Des frais en devise ne créent jamais de `FEE` :
ils s'ajoutent au `SPEND`.

Les frais d'une ligne `FEE` restent hors du prix de revient : ils retirent de
la quantité sans rien rembourser. Des frais payés **en plus** d'un achat sont,
eux, dans le coût, parce qu'ils sont dans l'`ANCHOR` ou le `SPEND`.

Le front reconstruit l'opération à partir de la **forme** du groupe
(`classify`, dans `cryptoOperations.ts`) : achat, échange, vente, dépôt,
transfert reçu… Toute nouvelle forme de groupe côté API doit y être reconnue,
sinon elle s'affiche en « Opération ».

## Transfert entre deux comptes

`create_cross_account_transfer` écrit un groupe **à cheval sur deux comptes** :

- source : `TRANSFER` (+ `FEE` éventuel) ;
- destination : `ANCHOR` = quantité × PRU source, arrondi au centime, puis `BUY`.

Le prix de revient suit l'actif. L'`ANCHOR` est écrit même à 0 € (un actif
reçu en récompense) : la forme `BUY` + `ANCHOR` sans `SPEND` ni `DEPOSIT` est la
signature qui fait lire à `_compute_daily_net_flow` une entrée valorisée au
cours du jour, en miroir de la sortie côté source.

## Ce qui compte comme argent entré

| Ligne | `total_deposits` / flux | Contribution bancaire |
| --- | --- | --- |
| `DEPOSIT EUR` seul | oui | oui : peut prouver un virement sortant de la banque |
| `DEPOSIT EUR` d'un `CRYPTO_DEPOSIT` | oui : la valeur de la crypto entre dans le compte | non (`is_auto_provision`) |
| `DEPOSIT` devise d'une vente | non : produit de cession (groupe avec `SPEND` crypto) | non (`is_auto_provision`) |
| `WITHDRAW EUR` | sortie | oui |
| `TRANSFER` / `WITHDRAW` crypto | sortie au cours du jour | — |

`is_auto_provision` marque une jambe EUR que l'application a écrite elle-même à
côté d'une autre : aucun argent n'a franchi la frontière du compte, donc rien
ne doit la lire comme un virement depuis la banque
(`services/banking/contributions.py`). Les lignes antérieures à la colonne sont
marquées à la première lecture : un `DEPOSIT EUR` dans un groupe de plus d'une
ligne. L'import applique la même règle.

## Les soldes négatifs

Une quantité peut passer sous zéro : une vente importée sans l'achat qui la
précède, des achats payés en euros jamais déposés. Le registre l'accepte et
continue de compter.

- **Crypto** : un `BUY` qui suit ne reprend en coût que la part qui reste
  positive. Une cession plus grande que le solde ne réalise que la part
  couverte : le reste du produit n'a pas de coût connu et n'est pas compté
  (`excluded_proceeds`). Chaque actif concerné remonte dans
  `negative_balances` (depuis quand, de combien). Une poussière au-dessus de
  `NEGATIVE_BALANCE_TOLERANCE` n'est pas un manque.
- **Euros** : la position reste négative sur la page du compte. Dans tout
  agrégat (patrimoine, historique), `counted_cash` la ramène à 0 et
  `uncounted_cash` la garde à part (`services/broker_cash.py`) : l'argent est
  déjà parti de la banque, le compter deux fois le retirerait du patrimoine
  deux fois. Dans les flux, ce qui n'est pas compté se lit comme un dépôt fait
  le jour où il apparaît (`daily_pnl` ajoute la variation d'`uncounted_cash`).

Le réglage `crypto_auto_deposits` (« dépôts automatiques ») est purement
d'affichage : il masque les positions négatives et le solde d'euros négatif, et
retire l'action « Dépôt » du formulaire. Il a remplacé « afficher les positions
négatives » le 2026-09-30, et part désactivé sur un nouveau compte.

`compute_balance_warning` avertit après une écriture qui rend un actif
négatif ; il ne bloque rien.

## P/L

- **Latent** = valeur des positions crypto − prix de revient (PRU). Pas les
  dépôts nets : réinvestir un gain ferait diverger les deux (2026-07-22).
- **Réalisé**, par groupe de cession (un `SPEND` crypto dans un groupe) =
  produit − prix de revient retiré. Le produit est le `DEPOSIT` devise du
  groupe, sinon son `ANCHOR`. Un `TRANSFER` ou un `WITHDRAW` crypto retirent du
  coût mais ne réalisent rien.
- `current_value` (VALEUR) ne compte que les positions : VALEUR − INVESTI = P/L.
  Les liquidités sont dans `cash_balance` ; un snapshot d'historique additionne
  les deux (2026-07-23).

## Écrire, modifier, supprimer

**Une jambe ne se modifie pas : une opération se supprime et se recrée.**
Changer la quantité d'un `BUY` sans son `ANCHOR`, ou l'actif d'un `SPEND` sans
le reste du groupe, laisserait un groupe qu'aucune action composite n'aurait
écrit. `CryptoTransactionUpdate` n'accepte donc plus que :

- **la date**, propagée à tout le groupe ;
- **les notes et le hash** ;
- **le montant d'un `ANCHOR`** : la valeur EUR du groupe, qu'aucune autre jambe
  ne répète. C'est ce qu'on corrige quand le préremplissage d'un import s'est
  trompé. Tout autre montant renvoie 422 (`ImmutableLegError`).

Jusqu'au 2026-10-11, l'actif, le type, la quantité et le prix de n'importe
quelle ligne se modifiaient seuls.

**Supprimer** une ligne supprime tout son groupe — y compris, pour un transfert
entre comptes, les lignes de l'autre compte. C'est voulu : un demi-transfert
laisserait un `BUY` sans contrepartie.

Après une écriture, l'historique se reconstruit à partir de la date touchée
**pour chaque compte du groupe** (`_history_scope`, dans `routes/crypto.py`),
pas seulement pour celui de la ligne visée.

## L'import

Un parser (`services/imports/`) ne produit que des paquets de `MappedRow`
datés ; tout le reste est commun (`_crypto_common.py`) :

1. **Regroupement** : Binance réunit les lignes à moins de 6 secondes d'écart ;
   chaque paquet devient un groupe.
2. **Aperçu** : un paquet qui échange sans aucune jambe en euros
   (`needs_eur_input`) demande sa valeur EUR, préremplie depuis l'historique de
   prix (`prefill_eur_amounts`).
3. **Exécution** : une ligne par jambe, un `group_uuid` par paquet, un `ANCHOR`
   si une valeur EUR a été fournie. Un `DEPOSIT` dans un paquet de plusieurs
   lignes est marqué `is_auto_provision`.
4. **Doublons** : empreinte (date à la seconde, actif, type, montant) calculée
   en mémoire après déchiffrement. Un paquet n'est ignoré que si **toutes** ses
   lignes existent déjà.

Côté Binance, `Withdraw` devient un `TRANSFER` (neutre) et un `Deposit` de
crypto un `BUY` à prix nul, qui demande sa valeur EUR comme un échange.

## L'historique

`account_history` garde un snapshot chiffré par jour et par compte, calculé en
rejouant `get_crypto_account_summary(as_of=jour)` avec les prix du jour
préchargés. Le jour courant n'est jamais lu en base : il est recalculé en
direct. `daily_pnl` = variation de valeur − flux externes du jour
(`_compute_daily_net_flow`).

Changer une formule ne demande ni migration ni script : incrémenter
`CURRENT_CALC_VERSION[AccountCategory.CRYPTO]`, et l'historique de chaque
utilisateur se reconstruit à sa prochaine connexion.

## Limites connues

- Un achat composite sans actif de cotation mais avec des frais crypto non
  inclus (`BUY` + `ANCHOR` + `FEE`, sans `SPEND`) a la même forme qu'un
  transfert reçu : `_compute_daily_net_flow` le compte comme une entrée.
  Accepté, peu atteignable.
- Le PRU transmis par un transfert est figé à son écriture : un achat plus
  ancien ajouté ensuite sur la source ne le corrige pas.
- Tout rejouer coûte un déchiffrement de chaque ligne du compte à chaque
  lecture : c'est le prix du chiffrement, pas un oubli.
