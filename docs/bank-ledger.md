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

**Aucun solde ne se saisit.** Ni à la création du compte, ni dans la modale
d'opération, ni par import : l'argent déjà présent à l'ouverture est la
première opération, une entrée comme une autre, qui compte dans les flux. La
modale montre le solde au jour choisi (`GET /bank/accounts/{id}/balance`) et
où l'opération l'amène ; elle peut aussi déduire le montant d'un solde lu sur
un relevé, ce qui donne une entrée ou une sortie réelle, jamais un ajustement.
Les relevés de solde saisis, l'import de fichiers de soldes et le solde
d'ouverture ont été retirés le 2026-10-09 : leur écart calculé à une date
compliquait tout le reste pour un cas que personne n'utilisait.

## Trois sortes d'opérations

Une colonne chiffrée `bank_transactions.origin_enc` les distingue :

| `origin` | Vient de | Compte dans les flux |
| --- | --- | --- |
| `NULL` | la synchronisation ou un import CSV | oui |
| `manual` | une saisie à la main | oui |
| `adjustment` | la conversion d'un compte : détaché de sa banque, ou antérieur à ce modèle | non |
| `forecast` | une prévision récurrente appliquée (option « Synchronisation automatique ») | non |

Un **ajustement** porte le solde qu'un compte avait avant d'être tenu par ses
opérations : quand on le détache de sa banque, ses opérations ne remontent
presque jamais à l'ouverture, et le solde lu à la banque la veille de la
première devient un ajustement d'ouverture. Rien d'autre n'en crée. Ajustements et prévisions sont neutres
(`TypeSource.ADJUSTMENT`) : jamais rapprochés en virement, jamais récurrents,
jamais comptés en dépense, revenu ou flux réel. Ils restent visibles dans la
liste des opérations, avec un badge, et se suppriment.

## La réalité remplace le reste

- Un import d'opérations supprime les ajustements et les prévisions datés dans
  sa période (de la première à la dernière opération du fichier). L'aperçu
  les liste.
- Une saisie manuelle n'a pas de référence : le niveau 2 de
  `store_transactions` (même date, montant, sens et devise) la reconnaît, et un
  import qui contient la même opération la **remplace** au lieu de la doubler.
  Ajustements et prévisions sont exclus de ce niveau 2.

## Limite connue

Un ajustement garde son montant. Si des opérations plus anciennes arrivent
plus tard, hors de la période d'un import, le solde d'après l'ajustement
bouge d'autant. Accepté : le corriger demanderait de stocker le solde lu
et non l'écart, c'est-à-dire une seconde source de vérité.

## Conversion des comptes

Un compte qu'on détache de sa banque (`forget_ledger`, dans
`services/banking/linking.py`) repasse par la conversion, comme les comptes
antérieurs à ce modèle. Les montants sont chiffrés par la clé maître : une
migration SQL ne peut pas les lire. La conversion se fait à la connexion
(`run_lazy_catchup`) et au premier `get_user_bank_accounts`, une fois par
compte (`ledger_version`) :

- avec des opérations : un ajustement d'ouverture, égal à la valeur de
  l'historique la veille de la première opération ;
- sans opération mais avec un historique : un ajustement par jour où la valeur
  change, ce qui redonne la même courbe ;
- sans rien : un ajustement égal au solde stocké, à l'ouverture.

Aucun montant n'est journalisé.
