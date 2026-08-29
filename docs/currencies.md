# Les devises dans CapitalView

## La règle

**Un compte porte sa devise ; tout ce qui est agrégé est en euros.**

La devise native d'un compte va jusqu'à la réconciliation — on compare les
mouvements au solde publié par la banque **dans sa devise à elle**. Convertir
avant de comparer transformerait chaque variation de change en faux écart de
réconciliation, sur un compte qui fonctionne parfaitement.

L'euro commence à l'agrégation : soldes totaux, courbes d'historique, flux.

| Étage | Devise |
| --- | --- |
| Solde d'un compte, affichage | celle du compte |
| Ancre de synchronisation | celle du compte |
| Réconciliation | celle du compte |
| `account_history` (courbe) | **EUR** |
| Solde total, agrégats | **EUR** |

## Où vit la liste

**`models/currency.py`** est la source unique.

- `SUPPORTED_CURRENCIES` — les codes et leurs noms, dans l'ordre d'affichage.
- `CURRENCY_CODES` — les codes seuls, pour les tests d'appartenance.
- `BASE_CURRENCY` — l'euro, le pivot de toute agrégation.
- `NO_CURRENCY` — `XXX`, le code ISO qui signifie « aucune devise ». Boursorama
  le renvoie sur la ressource compte ; l'accepter ferait lire un solde dans une
  devise qui n'existe pas.

Elle est servie par **`GET /market/currencies`**, que l'application web charge
au lieu de recopier la liste. Avant, les mêmes douze codes étaient écrits à trois
endroits, dont un avec seulement six entrées.

`dtos.crypto.FIAT_ASSET_KEYS` est un **alias** de `CURRENCY_CODES` : une
cinquantaine de points d'appel dans crypto, community et les imports le lisent
sous ce nom. `tests/routes/test_currencies.py` vérifie qu'ils ne divergent pas.

## Ajouter une devise

Une ligne dans `SUPPORTED_CURRENCIES`. La route et l'application web la
reprennent sans autre changement. Vérifier avant que le fournisseur de données
publie bien son cours — sinon elle apparaîtra dans le sélecteur et sera refusée
à l'enregistrement.

## Liste proposée ≠ liste autorisée

`SUPPORTED_CURRENCIES` alimente **l'interface**. Ce n'est pas ce que l'API
valide.

Ce que l'API exige d'un compte, c'est que sa devise soit **réellement
convertible** — `services.bank.require_convertible`, qui interroge les données
de marché. La distinction est délibérée : une banque peut légitimement répondre
avec un code absent de la liste, et le refuser rendrait un compte
inenregistrable sans raison.

## Ce qui est refusé, et pourquoi

**Une devise sans cours publié**, à la création comme à la modification d'un
compte. Sans ça elle rejoindrait le total au pair : `get_exchange_rate` répond
`1` aussi bien pour un taux qui vaut vraiment 1 que pour une devise qu'elle ne
connaît pas, et rien ne distinguerait les deux.

**Le total est retiré plutôt que faux.** Un cours peut cesser d'être publié
après la création du compte ; `BankSummaryResponse.total_balance` vaut alors
`null` et l'interface affiche « Total indisponible ». Les comptes restent
lisibles — seul le total disparaît. Ne rien montrer se rattrape, montrer un
chiffre faux ne se rattrape pas.

## Taux de change

**Un instantané prend le taux courant**, une **série prend le taux de chaque
date** — `get_historical_exchange_rates_db`. Un taux unique appliqué sur
plusieurs années dessinerait la forme du change au lieu de celle du solde.

Les jours de fermeture des marchés (week-ends, fériés) **reportent le dernier
cours publié**, comme la BCE qui ne publie que les jours ouvrés et dont un
samedi se lit au taux du vendredi.

## Limites connues

- Une devise dont le cours cesse d'être publié après la création du compte fait
  disparaître le total, pas le compte.
- Une panne du fournisseur refuse la création d'un compte dans une devise qui
  existe pourtant. Refus temporaire et lisible plutôt que total faux et
  permanent — arbitrage assumé.
- Une opération en devise étrangère **dans** un compte en euros est stockée mais
  exclue de la courbe et de la réconciliation : la banque la recomptabilise
  elle-même en euros quelques jours plus tard, et son taux fait foi. Mesuré sur
  4 240 opérations réelles : une seule ligne concernée, et seulement tant
  qu'elle est en attente.
- Les modules d'analyse gardent chacun leur constante `_EUR` locale. Elles ne
  divergent pas — une constante à la même valeur n'est pas une liste recopiée.
