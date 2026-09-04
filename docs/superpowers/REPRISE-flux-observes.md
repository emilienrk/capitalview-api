# Reprise — flux observés et virements internes (lot 3)

État au 2026-09-04. **Ce document est autosuffisant** : il dit d'où vient le sujet, ce qui a déjà
été livré, où est le code à reprendre, ce que son audit a trouvé, et dans quel ordre travailler.
Il est suivi par git.

Complément de `REPRISE-enable-banking.md`, qui couvre la connexion bancaire elle-même.

---

## D'où vient ce chantier

Le 1er septembre 2026, le compte carte d'Emilien s'est amorcé pendant que la synchro de son compte
courant échouait sur une erreur de solde. Le lendemain, quand le compte courant a enfin réussi, la
déduplication croisée a écarté **1 442 de ses 2 776 opérations** — celles que la carte avait déjà
stockées. Sa courbe est passée de `+541,81 €` à `−5 288,06 €` au 5 juin. Reproduit en rejouant son
export réel à travers `sync_user_accounts` dans les deux ordres.

En enquêtant, deux autres sujets sont remontés. Le troisième est ce chantier : **les virements entre
les propres comptes d'Emilien seraient comptés comme des dépenses.** Il tient un compte courant
BoursoBank, un compte Revolut, un Livret A et un LDDS, et déplace régulièrement de l'argent entre eux.

Mesuré sur ses données réelles, sur quatre ans :

| | opérations | volume brut |
| --- | --- | --- |
| libellé nommant un de ses comptes Bourso | 173 | 127 023 € |
| virements à son propre nom vers une autre banque (Revolut) | 135 | 21 877 € |

Non filtrés, **~79 000 € de fausses dépenses**, et c'est un plancher : ces chiffres viennent d'une
reconnaissance par libellé, qui rate tout ce qui ne suit pas ces formes.

**La banque ne fournit aucune contrepartie structurée.** Sur les 2 776 opérations du compte courant,
`creditor`, `creditor_account`, `debtor`, `debtor_account`, `creditor_agent`, `debtor_agent`,
`bank_transaction_code` et `merchant_category_code` sont **vides à 100 %**. Seul
`remittance_information` est rempli, en texte libre. La seule méthode robuste est donc
l'**appariement** : un débit sur A, un crédit sur B, même montant, dates proches.

## Ce qui est déjà livré (lots 1 et 2, en production)

- Les comptes carte ne sont plus rattachables (`list_session_accounts` les écarte, `link_account`
  répond 409). **R21**.
- La déduplication croisée (« niveau 3 ») est supprimée. **R22**.
- R12 retiré, R19 conservé sur une justification nouvelle. Voir la table des rulings de
  `REPRISE-enable-banking.md`.
- Détachement d'un compte, avec choix de supprimer ou garder ses opérations, et ré-amorçage
  automatique du compte jumeau.
- Le résultat de `POST /banking/sync` est lu par le front : un compte en échec s'affiche.
- Deux bugs corrigés : le filtre `flow_type` de `get_user_cashflow` (comparaison en minuscules
  contre un énum en majuscules, le filtre ne faisait rien) et `get_user_balance(date=…)` qui levait
  une `AttributeError` dès qu'un compte existait.

## L'état des lieux côté dépenses

**Le total est déjà juste, rien à faire.** `_total_in_base_currency` (`services/bank.py:391`) somme
tous les comptes sans distinction ; `get_all_bank_accounts_history` (`:765`) additionne les
instantanés par date. Un virement A→B est neutre sur le solde total et sur la courbe. L'agent IA lit
exactement ce chiffre.

**Les dépenses sortent uniquement des flux déclarés.** `FlowType` n'a que `INFLOW` et `OUTFLOW`
(`models/enums.py:22`), les catégories sont du texte libre sans sémantique, un `Cashflow` ne porte
qu'**un** compte (`models/cashflow.py:24`) — aucune forme de schéma ne peut dire « Bourso → Revolut ».
Aucune opération bancaire réelle n'alimente ces chiffres, et les mouvements bancaires sont
explicitement refusés à l'IA (`mcp_server/tools.py:94-110`).

**Décision d'Emilien** : les dépenses affichées viendront du réel, **le module Cashflow reste**. Les
flux déclarés prévoient l'avenir et tiennent à jour les soldes des comptes sans API
(`_apply_pending_cashflows`) ; les opérations réelles mesurent le passé. Les deux se rejoignent dans
une comparaison prévu ↔ réel. **Aucun changement à `FlowType`** : pas de troisième sens,
l'annulation des virements se fait par appariement.

---

## Où est le code à reprendre

**Pas sur une branche à fusionner.** `feat/cashflow-real-flows` n'a **aucun commit** que `main` n'a
pas : elle pointe 31 commits en arrière, sur un état d'avant la suppression. Le code vit dans
l'historique de `main`.

```
git show 77e70bf^:services/banking/flows.py
git show 77e70bf^:services/banking/matching.py
git show 77e70bf^:tests/services/test_banking_flows.py
git show 77e70bf^:tests/services/test_banking_matching.py
```

`77e70bf` — *« refactor(banking): move the observed flows out to their own branch »*, 28/08/2026,
**1 475 lignes supprimées** dont **642 de tests**. Motif du retrait, dans le message du commit :
*« flows.py and matching.py are the only part of this branch never reviewed by anyone but their
author. »* Ont disparu avec eux : les DTO `BankFlowsResponse`, `BankFlowMonth`,
`BankFlowCurrencyTotal`, `CashflowComparison`, `MatchCandidate`, `RecentOccurrence`, et les routes
`GET /banking/flows`, `GET /cashflow/me/comparison`, `PUT /cashflow/{id}/match`.

Résidus laissés sur `main`, à traiter en passant : `Cashflow.match_pattern_enc` et sa migration
`d5e2c81f9a34` (schéma mort, jamais lu ni écrit), et deux en-têtes de section orphelines
(`dtos/cashflow.py:94-96`, `dtos/banking.py:179-181`).

---

## Audit du code retiré — fait le 2026-09-04

### Ce qui tient

- **Agnostique de la banque.** Ne lit que les champs que le contrat marque obligatoires — montant,
  devise, `credit_debit_indicator` — plus le statut. Ne parse jamais un libellé, et le docstring dit
  pourquoi : les champs structurés qui le remplaceraient sont vides sur les 4 240 lignes réelles.
- **Déterministe.** `movements.sort(...)` avant toute indexation, sinon l'appariement rendrait une
  réponse différente à chaque appel sur les mêmes données.
- **Appariement au plus proche, un pour un.** Un crédit déjà pris ne sert pas un second débit ; le
  candidat retenu est le plus proche en date, pas le premier trouvé.
- **La signature de libellé ne contient aucun vocabulaire bancaire.** Elle garde les mots
  alphabétiques et jette tout groupe contenant un chiffre — ce à quoi ressemblent dates, références
  et suffixes de carte dans tous les formats vus. Mesuré : 1 252 variantes brutes → 815 groupes.
- **Seuil de dérive raisonné** : 2 % avec plancher à 1 €, comparé sur la **médiane** pour qu'un mois
  isolé ne décide pas seul.
- **Moyennes sur les mois couverts**, pas sur la fenêtre : diviser trois mois d'historique par douze
  se lirait comme −75 % de revenus.

### 🔴 Bloquant — le double comptage carte, créé par le lot 1

`compute_real_flows` (`flows.py:120`) **et** `load_signature_groups` (`matching.py:127`) interrogent
`BankTransaction.account_id_bidx.in_(account_bidxs)` sur **tous** les comptes rattachés.

Depuis le retrait de la déduplication croisée (**R22**), un compte carte encore rattaché porte les
mêmes lignes que son compte courant. Donc :

- `compute_real_flows` **compterait chaque achat carte deux fois** en dépenses ;
- `load_signature_groups` produirait **deux occurrences de la même signature**, ce qui fausse
  `MIN_OCCURRENCES` et déclenche des verdicts `DUPLICATED` sur des flux parfaitement sains.

`_internal_transfer_legs` ne les rattrape pas : un écho de carte est de **même** sens, alors qu'il
cherche des sens **opposés**.

Ce n'est pas un argument pour rallumer la dédup — elle détruisait des données pour ça. **Le filtre
doit être à la lecture, jamais à l'écriture.** Deux garde-fous existent déjà : R21 rend les comptes
carte non rattachables, donc le cas ne concerne que les liens hérités ; et le filtre lui-même est à
écrire et à tester.

### 🟠 Mesuré — 26 faux positifs d'appariement

L'algorithme d'`_internal_transfer_legs` a été rejoué tel quel sur les 4 240 opérations réelles
(compte courant + carte) : **26 paires trouvées, 1 236,38 € exclus des totaux**.

Or il n'existe **aucun** virement interne entre ces deux comptes-là. Ce sont des remboursements et
avoirs qui apparaissent sur les deux, que la règle « sens opposés, même montant, ≤ 3 jours, comptes
différents » prend pour des virements. Échantillon :

```
2024-07-08 CACC -3      <-> 2024-07-08 CARD +3       (0j)
2024-10-09 CACC -12.99  <-> 2024-10-09 CARD +12.99   (0j)
2024-11-21 CARD -10     <-> 2024-11-18 CACC +10      (3j)
2024-12-10 CARD -55     <-> 2024-12-10 CACC +55      (0j)
```

1,2 % des mouvements, mais ce sont de vraies dépenses effacées du total. **Le filtre carte du point
précédent en supprimerait la quasi-totalité** — les deux correctifs se recouvrent largement.

### 🟡 A vieilli — 31 commits depuis le retrait

Trois ont changé le terrain sous ce code :

- `ec4b059` — chaque compte a sa propre devise
- `a7c65e8` / `06ca827` / `08ed9ea` — la courbe est stockée en euros, convertie à l'écriture
- `27d972c` — un flux déclaré est dénommé par le compte qu'il touche

Conséquences :

- `flows.py` **ne convertit jamais** : il choisit la devise majoritaire et range les autres dans
  `other_currencies`. Défendable (aucun taux n'arrive avec la transaction), mais plus cohérent avec
  le reste de l'app qui convertit à l'écriture. À trancher, pas forcément à changer.
- `matching.py` **ne regarde aucune devise** : il compare un montant déclaré à des montants observés
  sans vérifier qu'ils parlent la même monnaie. Depuis `27d972c`, c'est un vrai trou.

**Sur les virements, l'hypothèse « même devise » est bonne** (tranchée par Emilien) : un virement
entre ses comptes est en euros des deux côtés, et l'algorithme exigeant montant **et** devise
identiques, un virement en devise ne serait simplement pas apparié — il rate, il n'invente pas. Bon
sens d'échec.

**Rien ne dépend de `store_transactions` ni du code supprimé au lot 1** — pas d'autre casse
mécanique. Noter tout de même que `store_transactions` a perdu son paramètre `user_uuid` (commit
`3159622`) : les tests ressuscités qui l'appellent doivent être ajustés.

---

## Ordre de travail

### 1. Ressusciter, faire compiler, faire passer les 642 lignes de tests

Restaurer les quatre fichiers, les DTO et les trois routes depuis `77e70bf^`. Ne rien corriger
encore : le but est d'avoir une base verte avant de la juger.

### 2. Le filtre carte (bloquant)

À la lecture, dans `compute_real_flows` et `load_signature_groups`. Test à écrire, **prouvé non vide
par mutation** : un compte carte lié + son compte courant, la même opération sur les deux, le total
ne doit la compter qu'une fois. Retirer le filtre doit rougir le test.

### 3. Les faux positifs d'appariement

Remesurer après le filtre carte. S'il en reste, arbitrer entre resserrer la tolérance de 3 jours et
exiger un signal supplémentaire. **Ne pas régler ça au libellé** — c'est spécifique à Boursorama.

### 4. La devise dans `matching.py`

Comparer des montants de même devise, ou refuser la comparaison. Petit, mais silencieusement faux
aujourd'hui.

### 5. L'import CSV transactionnel (pour le Livret A)

**Non fait, contrairement à ce qu'on pourrait croire.** Sur `main`,
`services/imports/bank_csv.py` produit **uniquement des points de solde** :
`parse_bank_points` (`:43`) renvoie des (date, solde) et le mode `delta` **intègre** les mouvements
pour en faire des soldes de fin de journée (`:68-74`) — le sens est détruit à l'ingestion. Seuls la
synchro et l'import du JSON d'export écrivent des `BankTransaction`.

Ajouter un chemin transactionnel : date, montant, sens, libellé → la forme attendue par
`normalize_transaction`, puis `store_transactions`. Les niveaux 1 et 2 de déduplication viennent
gratuitement, donc un réimport du même fichier est sans effet.

⚠️ **Piège** : un CSV n'a pas d'`entry_reference`, donc le niveau 1 ne s'applique pas et le niveau 2
(empreinte) devient seul juge. Deux opérations réellement distinctes, même jour, même montant, sans
référence, **se confondent d'un import à l'autre**. Remède : **synthétiser une référence stable**
depuis le contenu de la ligne et son rang dans le fichier — le réimport reste idempotent et deux
opérations jumelles survivent.

⚠️ **Second piège** : l'import de soldes existant écrit des instantanés `AccountHistory`, le
transactionnel n'en écrit pas. Décider si la courbe d'un compte importé en transactions se
reconstruit (il faudrait un solde de référence, que le CSV ne porte pas forcément) ou si les deux
imports restent complémentaires. **Ne pas casser le chemin de soldes qui marche.**

### 6. Brancher

Les dépenses observées deviennent la source affichée, les virements internes exclus et **comptés à
part** — ne pas les cacher, « 1 240 € déplacés entre vos comptes » est une information. La
comparaison prévu ↔ réel revient avec `matching.py`. Ouvrir ces chiffres à l'agent IA : attention au
refus explicite en place (`mcp_server/tools.py:94-110`), à lever délibérément, pas par accident.

**Ne bougent pas** : la page Flux de trésorerie, l'épargne mensuelle et le taux d'épargne du tableau
de bord, la mise à jour automatique des soldes manuels, la dénomination en devise des flux.

---

## Vérification

1. `uv run pytest -q` — **exige `dangerouslyDisableSandbox: true`** (cache uv bloqué).
   Référence au 2026-09-04 : **1 180 tests**.
2. `pnpm type-check` et `pnpm test` dans `capitalview-web` (92 tests). Éviter `pnpm build` (run-p).
3. **Mutation** : chaque test neuf doit être prouvé non vide en cassant volontairement le code qu'il
   couvre. C'est la convention du dépôt, pas une option.
4. **Rejeu réel** : `vendor-docs/spike/export-boursorama-2022-2026.json` (hors git, tests sous garde
   de skip). L'export complet dans les deux sens doit donner 2 776 et 1 464 insertions.
5. Après le filtre carte, **remesurer les 26 faux positifs** : ils doivent avoir quasiment disparu.

## Contexte utilisateur à ne pas oublier

Le « Compte plaisir » d'Emilien est **encore rattaché à son compte carte** — le déploiement du lot 1
ne rompt aucun lien existant. Tant qu'il l'est, ses opérations existent en double dans les données et
dans l'export, et c'est exactement le cas que le filtre du point 2 doit couvrir. Son parcours de
réparation, à faire par lui : détacher « Compte plaisir » en cochant la suppression des opérations →
aller sur Comptes Bancaires (ré-amorçage automatique du compte courant) → importer son JSON d'export.
