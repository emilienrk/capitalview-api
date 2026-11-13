# Le solde d'un compte bancaire

Référence du solde et de la courbe d'un compte bancaire (`services/bank_ledger.py`). Le code la suit, et chaque
changement de comportement la met à jour. Les règles d'abord, numérotées L1 à L11 ; le journal daté en fin.

## La règle

**L1. Un compte synchronisé croit sa banque ; un compte non synchronisé croit ses opérations.**

| Compte | Source de vérité | Solde et courbe |
| --- | --- | --- |
| Synchronisé (Open Banking) | la banque | lus à chaque synchronisation (`services/banking/sync.py`) ; un import CSV ne remplit que les jours d'avant l'historique servi par la banque, et la banque remplace un import sur les jours qu'elle sert |
| Non synchronisé | ses opérations | solde = somme de toutes ses opérations depuis 0 ; courbe = cumul jour par jour jusqu'à hier |

**La banque remplace l'import sur la période qu'elle sert.** Un import CSV sur un compte déjà synchronisé s'arrête à
l'historique servi par la banque (`bank_coverage`). À l'inverse, quand un compte rempli par un import est rattaché à sa
banque, le passage d'amorçage supprime les lignes importées datées du jour de la plus ancienne opération que la banque
renvoie ou après, puis stocke les opérations de la banque comme sur tout compte (`_replace_imported`). Les lignes
importées plus anciennes restent : une banque qui ne sert que quatre-vingt-dix jours ne coûte aucun historique. Seules
les lignes importées partent ; une saisie à la main suit L8 et les ajustements L4. Ce qui était accroché à une ligne
supprimée part avec elle, jamais orphelin : ses paires (l'une ou l'autre jambe), sa réponse de versement, son type forcé ;
une décision de récurrent cesse de la nommer et garde l'identité qui lui fait retrouver sa série. Les synchronisations
suivantes ne remplacent rien. Une ligne importée se reconnaît sans marqueur : sa référence synthétique (`csv:…`) est
recalculée sur son propre contenu (`imported_rows`). Remplacer plutôt que garder : le relevé et la banque écrivent une
même opération autrement, et les deux moitiés d'un historique ne partageraient jamais une règle, un libellé appris ni
une réponse. Le compte le dit (`sync_notice`, voir L11) : « N opérations importées remplacées par celles de votre
banque à partir du JJ/MM/AAAA : M réponses sont à redonner dans À trier. », sans la seconde moitié quand aucune réponse
ne part. Le solde reste celui de la banque, et `history_served_from` garde ce qu'elle a servi.

**L2.** Pour un compte non synchronisé, `bank_accounts.balance_enc` et ses lignes `account_history` ne sont qu'un
**cache** : `services/bank_ledger.py` (`rebuild_from_operations`) les réécrit en entier après chaque écriture
d'opération. Rien ne les lit comme une vérité. Le solde compte toutes les opérations, celles d'aujourd'hui comprises ;
la courbe s'arrête à hier.

**L3. Aucun solde ne se saisit.** Ni à la création du compte, ni dans la modale d'opération, ni par import : l'argent
déjà présent à l'ouverture est la première opération, une entrée comme une autre, qui compte dans les flux. La modale
montre le solde au jour choisi (`GET /bank/accounts/{id}/balance`) et où l'opération l'amène ; elle peut aussi déduire
le montant d'un solde lu sur un relevé, ce qui donne une entrée ou une sortie réelle, jamais un ajustement. Limite
connue (02/11) : cette première entrée compte en revenu du mois tant qu'on ne la type pas « Non compté » ; sans
libellé, elle ne pose pas de question (docs/bank-sorting.md, R24).

## Trois sortes d'opérations

**L4.** Une colonne chiffrée `bank_transactions.origin_enc` les distingue :

| `origin` | Vient de | Compte dans les flux |
| --- | --- | --- |
| `NULL` | la synchronisation ou un import CSV | oui |
| `manual` | une saisie à la main | oui |
| `adjustment` | la conversion d'un compte : détaché de sa banque, ou antérieur à ce modèle | non |

Un **ajustement** porte le solde qu'un compte avait avant d'être tenu par ses opérations : quand on le détache de sa
banque, ses opérations ne remontent presque jamais à l'ouverture, et le solde lu à la banque la veille de la première
devient un ajustement d'ouverture. Rien d'autre n'en crée. Les ajustements sont neutres (`TypeSource.ADJUSTMENT`) :
jamais rapprochés en virement, jamais récurrents, jamais comptés en dépense, revenu ou flux réel. Ils restent visibles
dans la liste des opérations, avec un badge, et se suppriment.

**L5.** Aucune opération n'est inventée à l'avance : un solde ne compte que des opérations réelles, synchronisées,
importées ou saisies.

## La réalité remplace le reste

**L6.** Un import d'opérations supprime les ajustements datés dans sa période (de la première à la dernière opération
du fichier), **sauf l'ajustement d'ouverture** (L7). L'aperçu les liste (`bank_replaced`).

**L7. L'ajustement d'ouverture est reculé, jamais supprimé.** L'ajustement d'ouverture est le plus ancien du compte,
quand aucune opération réelle n'est datée avant lui (`bank_ledger.opening_adjustment`) : il dit ce que le compte
tenait à la fin de son jour. Tout ce qui s'écrit avant ce jour garde cette affirmation vraie :

- un **import** dont la période remonte à ce jour ou avant le recule à la veille de la première ligne du fichier, en
  lui retirant ce que le fichier apporte jusqu'à son jour (`carry_opening`). Le solde à son ancienne date ne bouge
  pas ; la courbe commence plus tôt, au bon niveau. L'aperçu le dit (`bank_carried` : date et montant avant, date et
  montant après), et l'import le renvoie (`carried`). Si le fichier commence vraiment à l'ouverture du compte, c'est à
  l'utilisateur de supprimer ensuite l'ajustement, visible dans la liste ;
- une **saisie à la main** datée ce jour ou avant le recule de même, à la veille de la saisie, moins son montant ;
- un ajustement ramené à zéro est supprimé : il n'y a plus rien à porter.

Avant le 02/11, un import qui couvrait l'ajustement d'ouverture le supprimait comme les autres : vérifié sur un cas,
un solde de 600 € passait à −92,50 €, et seul l'avertissement « passe sous zéro » le signalait, donc rien quand le
solde restait positif.

**L8.** Une saisie manuelle n'a pas de référence : le niveau 2 de `store_transactions` (même date, montant, sens et
devise) la reconnaît, et un import qui contient la même opération la **remplace** au lieu de la doubler. Les
ajustements sont exclus de ce niveau 2.

**L10. Une saisie à la main porte un libellé**, avec au moins une lettre ou un chiffre : c'est lui qui donne son
identité à l'opération (docs/bank-sorting.md, R24). Sans lui, elle ne poserait aucune question, ne porterait aucune
règle et compterait en silence comme le dit son sens. La modale propose les libellés déjà vus sur ce compte dans ce
sens, les plus fréquents d'abord : en reprendre un tel quel range la saisie avec ses semblables. Pas de libellé par
défaut : un même texte pour toutes les saisies les regrouperait, et une règle ou un Oui appris sur l'une rangerait les
autres. Vérifié dans la modale seulement : l'API accepte encore une saisie sans libellé.

**L11. La première synchronisation d'un compte rempli par import dit ce qu'elle a remplacé et de combien elle a
décalé la courbe.** La courbe
d'un compte synchronisé se redessine en remontant depuis le solde de la banque. Sur des lignes importées, elle ne part de
0 la veille de la première opération que si la somme des opérations stockées, importées puis synchronisées, égale ce
solde ; sinon toute l'histoire est relevée ou abaissée de l'écart, sans marche au jour du rattachement : c'est voulu.
Le passage d'amorçage le compare (`_import_notice`) et le compte le garde, en chiffres, avec ce que la banque a remplacé
(L1), jusqu'à ce que l'utilisateur le masque ou qu'un autre passage d'amorçage le remplace (`sync_notice` : `replaced`,
`replaced_from`, `answers_dropped`, `operations_total`, `bank_balance`, `curve_shift`) ; le front écrit les phrases, pour
que le mode discret masque les montants : « Vos opérations importées donnent X €, votre banque Y € : la courbe a été relevée (abaissée) de Z €. Votre
import ne remonte sans doute pas à l'ouverture du compte, ou il lui manque des opérations. » Gardé sur le compte et non
dans la seule réponse : l'amorçage tourne d'ordinaire en arrière-plan juste après le rattachement, et sa réponse
n'atteint personne. Rien quand l'écart est nul, sur un compte sans ligne importée, ni aux synchronisations suivantes.
Rien non plus quand la comparaison ne tient pas : sur un solde disponible (`estimated`), corrigé des seules opérations en
attente que la banque partage, et sur un compte carte, sans courbe. Les deux côtés ne comptent que les opérations
comptabilisées dans la devise du compte, comme la courbe. Limite : quand l'import remonte plus loin que la plus
ancienne opération servie par la banque, la courbe n'est redessinée qu'à partir de celle-ci, et l'écart y fait une
marche au lieu de décaler les jours d'avant.

## Limite connue

**L9.** Un ajustement autre que celui d'ouverture garde son montant. Si des opérations plus anciennes que lui arrivent,
hors de la période d'un import, le solde d'après bouge d'autant. Accepté : le corriger demanderait de stocker le solde
lu et non l'écart, c'est-à-dire une seconde source de vérité.

De même, une opération qui a reculé l'ajustement d'ouverture (L7) ne le ramène pas quand elle disparaît : la supprimer
décale le solde d'après de son montant, et la corriger — la modale supprime puis ressaisit — le décale de l'écart.
Vérifié le 06/11 : ouverture à 700 €, saisie de −30 € avant elle, puis supprimée : 730 € ; ressaisie à −40 € : 690 €,
au lieu de 700 € les deux fois. Accepté pour l'instant : le corriger demanderait de garder sur l'ajustement la date et
le montant affirmés au départ, pour le recalculer à chaque écriture.

## Conversion des comptes

Un compte qu'on détache de sa banque (`forget_ledger`, dans `services/banking/linking.py`) repasse par la conversion,
comme les comptes antérieurs à ce modèle. Les montants sont chiffrés par la clé maître : une migration SQL ne peut pas
les lire. La conversion se fait à la connexion (`run_lazy_catchup`) et au premier `get_user_bank_accounts`, une fois
par compte (`ledger_version`) :

- avec des opérations : un ajustement d'ouverture, égal à la valeur de l'historique la veille de la première
  opération ;
- sans opération mais avec un historique : un ajustement par jour où la valeur change, ce qui redonne la même courbe ;
- sans rien : un ajustement égal au solde stocké, à l'ouverture.

Aucun montant n'est journalisé.

## Journal

- **Avant octobre 2026** : le solde était un champ saisi, recopié chaque jour dans l'historique, et l'import
  d'opérations s'ancrait sur cet historique. Un réimport ne pouvait plus corriger un solde figé : d'où ce document.
- **9 octobre 2026** : les relevés de solde saisis, l'import de fichiers de soldes et le solde d'ouverture sont
  retirés. Leur écart calculé à une date compliquait tout le reste pour un cas que personne n'utilisait (L3).
- **11 octobre 2026** : les prévisions récurrentes (option « Synchronisation automatique », qui écrivait chaque
  échéance d'un flux déclaré sur son compte) sont retirées (L5).
- **2 novembre 2026** : l'ajustement d'ouverture est reculé par un import ou une saisie plus ancienne, jamais
  supprimé, et l'aperçu d'import le dit (L7). La limite connue se restreint aux autres ajustements (L9). La première
  entrée comptée en revenu notée comme limite (L3).
- **9 novembre 2026** : le libellé d'une saisie devient obligatoire, avec au moins un mot, et la modale propose ceux
  déjà vus sur le compte (L10). Sur le dump, aucune saisie à la main ; 17 opérations importées sans mot (« A.r.e.a. »,
  « H&L », « 00160669 », « Cgf17516mcdo1 ») restent sans question, limite R24.
- **12 novembre 2026** : un libellé sans mot garde une identité, son texte entier (docs/bank-sorting.md, R24) : la
  saisie à la main n'exige plus un mot, seulement une lettre ou un chiffre (L10).
- **12 novembre 2026** : la période qu'un import couvre reste à l'import ; la synchronisation ne stocke que les
  opérations de la banque d'après sa dernière ligne (L1). Avant, la première synchronisation d'un compte rempli par
  import y ajoutait tout l'historique de la banque : les lignes importées portent une référence synthétique, que ni le
  niveau 1 ni le niveau 2 ne rapprochent. Rejoué sur le dump du 08/11, « Compte secondaire » : avant, 2 506 lignes —
  1 207 importées (14/05/2022 → 29/05/2026) et 1 299 de la banque (14/05/2022 → 03/11/2026), 1 157 lignes importées
  ayant une jumelle de la banque le même jour et 50 à trois jours au plus. Après, 1 257 lignes : les 1 207 importées
  intactes et 50 de la banque, toutes celles d'après le 29/05/2026 ; 0 doublon le même jour ni à trois jours ;
  1 249 opérations retenues, dont les 42 à 0 €, pour un net de 5,68 € ; les 31 réponses sur les lignes importées
  restent. Le solde reste celui de la banque, et la courbe redessinée part de 0 la veille de la première opération et
  suit chaque jour la somme des opérations stockées.
- **12 novembre 2026** : la première synchronisation d'un compte rempli par import compare la somme de ses opérations
  au solde de la banque et dit de combien la courbe en est décalée ; le compte garde ce message, avec les opérations
  retenues, jusqu'à ce que l'utilisateur le masque (L11). Sur le dump, « Compte secondaire » : écart nul (la courbe
  part de 0,00 € la veille du 14/05/2022), donc aucune phrase sur la courbe ; seule reste celle des 1 249 opérations
  retenues.
- **13 novembre 2026** : la banque remplace l'import sur la période qu'elle sert, réponses comprises (L1) ; cette règle
  remplace celle du 12 novembre, qui gardait l'import et retenait les opérations de la banque. Mesuré sur le dump du
  08/11, « Compte secondaire » : des 1 157 paires relevé/banque du même jour, 107 seulement ont le même texte et 264
  (23 %) la même signature de libellé. L'export CSV de Revolut est en français, le flux en anglais (« Recharge sur Apple
  Pay via *2793 » / « Apple Pay Top-Up by *2793 », « Virement à : X » / « To X », « Sur la Pocket EUR coffre1 depuis
  EUR » / « To EUR coffre1 ») : garder l'import aurait coupé l'historique en deux formats de libellé pour toujours, et
  les règles, libellés appris et réponses de l'un n'auraient jamais atteint l'autre. Perdre une fois les réponses coûte
  moins. Le message du compte dit désormais combien de lignes sont remplacées, depuis quand, et combien de réponses
  sont à redonner (L11). Rejoué sur le dump, passage d'amorçage de « Compte secondaire » : avant, 2 506 lignes (1 207
  importées, 1 299 de la banque) et 1 581 paires même jour, montant et sens entre les deux, 713 à trois jours au plus ;
  après, les 1 299 de la banque seules, 0 doublon, les 1 207 lignes importées remplacées à partir du 14/05/2022, aucune
  gardée. 30 réponses parties : 22 paires, 1 réponse de versement, 7 types forcés ; 5 décisions de récurrent cessent de
  nommer les lignes remplacées. Les 3 réponses sur les lignes de la banque restent. À trier passe de 69 questions (50 de
  flux, 19 paires) à 68 (39 de flux, dont 1 de versement, 29 paires). Le solde reste celui de la banque, et la somme des
  opérations l'égale : aucune phrase sur la courbe.
