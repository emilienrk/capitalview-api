# Le solde d'un compte bancaire

## La règle

**Un compte synchronisé croit sa banque ; un compte non synchronisé croit ses
opérations.**

| Compte | Source de vérité | Solde et courbe |
| --- | --- | --- |
| Synchronisé (Open Banking) | la banque | lus à chaque synchronisation (`services/banking/sync.py`) ; un import CSV ne remplit que les jours d'avant l'historique servi par la banque |
| Non synchronisé | ses opérations | solde = somme de toutes ses opérations depuis 0 ; courbe = cumul jour par jour jusqu'à hier |

Pour un compte non synchronisé, `bank_accounts.balance_enc` et ses lignes
`account_history` ne sont qu'un **cache** : `services/bank_ledger.py`
(`rebuild_from_operations`) les réécrit en entier après chaque écriture
d'opération. Rien ne les lit comme une vérité.

Avant ce modèle (octobre 2026), le solde était un champ saisi, recopié chaque
jour dans l'historique, et l'import d'opérations s'ancrait sur cet historique.
Un réimport ne pouvait plus corriger un solde figé : d'où ce document.

## Trois sortes d'opérations

Une colonne chiffrée `bank_transactions.origin_enc` les distingue :

| `origin` | Vient de | Compte dans les flux |
| --- | --- | --- |
| `NULL` | la synchronisation ou un import CSV | oui |
| `manual` | une saisie à la main | oui |
| `adjustment` | un solde déclaré, un import de soldes, le solde à la création | non |
| `forecast` | une prévision récurrente appliquée (option « Synchronisation automatique ») | non |

Un **ajustement** vaut l'écart entre le solde déclaré à une date et le solde que
les opérations donnent à cette date. Ajustements et prévisions sont neutres
(`TypeSource.ADJUSTMENT`) : jamais rapprochés en virement, jamais récurrents,
jamais comptés en dépense, revenu ou flux réel. Ils restent visibles dans la
liste des opérations, avec un badge, et se suppriment.

## La réalité remplace le reste

- Un import d'opérations supprime les ajustements et les prévisions datés dans
  sa période (de la première à la dernière opération du fichier). L'aperçu
  les liste.
- Un solde déclaré au jour J supprime les prévisions datées de J ou avant.
- Une saisie manuelle n'a pas de référence : le niveau 2 de
  `store_transactions` (même date, montant, sens et devise) la reconnaît, et un
  import qui contient la même opération la **remplace** au lieu de la doubler.
  Ajustements et prévisions sont exclus de ce niveau 2.
- Importer un fichier de soldes crée, point par point dans l'ordre des dates,
  l'ajustement qui amène le solde calculé au solde lu. Le même fichier
  réimporté ne crée rien.

## Limite connue

Un ajustement garde son montant. Si des opérations plus anciennes arrivent
plus tard, hors de la période d'un import, le solde d'après l'ajustement
bouge d'autant. Accepté : le corriger demanderait de stocker le solde déclaré
et non l'écart, c'est-à-dire une seconde source de vérité.

## Conversion des comptes existants

Les montants sont chiffrés par la clé maître : une migration SQL ne peut pas
les lire. La conversion se fait à la connexion (`run_lazy_catchup`) et au
premier `get_user_bank_accounts`, une fois par compte (`ledger_version`) :

- avec des opérations : un ajustement d'ouverture, égal à la valeur de
  l'historique la veille de la première opération ;
- sans opération mais avec un historique : un ajustement par jour où la valeur
  change, ce qui redonne la même courbe ;
- sans rien : un ajustement égal au solde stocké, à l'ouverture.

Aucun montant n'est journalisé.
