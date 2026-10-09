# Le tri des opérations bancaires

Comment chaque opération d'un compte bancaire est comptée : virement entre comptes, dépense, épargne, investissement,
ou non compté. Règles validées par Emilien les 8 et 9 octobre 2026. Tableau d'origine, avec l'avis argumenté sur
chaque règle : https://claude.ai/artifact/9TBgxbN7UCE9iM7rCSSQM2

## La règle

**Par défaut, l'appli ne fait aucun faux positif.** Elle n'automatise que ce qui repose sur une preuve : une décision
de l'utilisateur, la loi, ou une répétition mesurée. **Jamais un mot du libellé** : les libellés ne sont pas
standardisés, ils changent d'une banque et d'une langue à l'autre, et un même libellé couvre des opérations de natures
différentes (« VIR Virement interne » vers un livret, vers un ami, vers un PEA).

Quand il n'y a pas de preuve, l'opération est comptée comme le dit la banque, et l'incertitude se voit. Les
accélérateurs (règles par nom, mots appris) viendront ensuite, **en option** dans les réglages, jamais par défaut.

Chaque changement de règle se mesure sur un dump réel, avant et après : paires, types, questions, totaux.

## Ce qui est automatique

| Règle | Exemple | Pourquoi c'est sûr |
| --- | --- | --- |
| **Ta décision** : lier deux opérations, ou les délier | tu lies −200 € Boursorama et +200 € Revolut | c'est toi qui l'as dit |
| **Livret réglementé** (Livret A, LDDS, LEP, PEL, CEL) d'un côté d'une paire | courant −500 € → Livret A +500 € le même jour | la loi interdit qu'il aille ailleurs que vers ton compte |
| **Paire répétée** : même débit, même crédit, mêmes deux comptes, au moins 3 fois | tes virements Boursorama ↔ Revolut | mesuré : aucun faux virement à 3, un remboursement de tiers passait à 2 |
| **Paire déjà validée** : les deux côtés ressemblent aux deux côtés d'une paire que tu as validée, sur les mêmes comptes | une recharge Revolut validée une fois, les suivantes passent | ta validation porte sur *cette paire*, pas sur tous les virements d'un des deux noms |
| **Remboursement sur un même compte** : même montant dans les 30 jours, avec un mot rare commun | −30 € Amazon, puis +30 € Amazon | les mots trop fréquents sont calculés sur ton compte, sans dictionnaire |
| **Dépôt Bourse/Crypto déjà validé** : même jour, même montant au centime, vers le même compte d'investissement qu'un dépôt que tu as validé avec ce libellé | ton virement mensuel vers le PEA | comme une paire déjà validée |

Une paire reconnue n'est **jamais** une règle par nom : elle ne classe pas les autres opérations qui portent un des deux
libellés. Une opération n'a qu'une paire, et un dépôt ne justifie qu'une seule opération.

## Ce qui reçoit une question

Une question n'est posée que lorsque **quelque chose est en face** : l'utilisateur peut alors réellement répondre.

- **Paire possible** entre deux comptes (sens opposés, même montant, au plus 2 jours ouvrés d'écart), qu'aucune règle
  ci-dessus ne confirme. Y compris entre deux comptes courants, même avec le même libellé, le même jour et le même
  montant. Une paire seulement proposée ne change aucun total.
- **Dépôt Bourse/Crypto possible** : même montant, dans les 3 jours, ou avec un écart de frais. Si une paire et un
  dépôt sont tous deux possibles, la même question montre les deux.
- **Entrée sans rien en face** (« revenu ou remboursement ? »), groupée par nom, si le groupe dépasse 100 €. Seul
  l'utilisateur sait si les 50 € d'un ami sont un remboursement.

Les questions sont rangées par année, les plus récentes d'abord.

## Ce qui ne reçoit pas de question

**Une sortie sans rien en face** est une dépense : si tous les comptes d'épargne et d'investissement sont dans l'appli,
elle ne peut être que ça. Elle n'est pas posée en question, mais :

- la liste **« Sorties sans contrepartie »**, dans « À trier », les montre au-dessus d'un montant choisi avec un curseur.
  En bougeant le curseur, on voit combien d'opérations la liste contient. On peut les classer à la main ; sinon, elles
  restent des dépenses. Elle sert à repérer un compte oublié (3 000 € partis vers un PEA ouvert ailleurs) ;
- à l'ajout d'une banque, une phrase qu'on peut fermer : « ajoute aussi tes livrets et comptes de bourse, sinon les
  virements vers eux compteront en dépense » ;
- à côté des totaux : « X € à trier ».

**Les dépôts sans contrepartie** sont listés aussi : les dépôts déclarés sur un compte Bourse ou Crypto pour lesquels
aucune sortie bancaire n'a été trouvée.

## Répondre

- **Celle-ci** : le choix par défaut.
- **Celles que je coche** : parmi les opérations du même nom, listées. Ne crée aucune règle.
- **Les prochaines aussi** : case explicite, qui crée une règle visible dans les réglages, supprimable.
- Une règle par nom n'empêche **jamais** une paire trouvée plus tard : la paire passe avant.
- Une règle par nom écrite à la main (second temps) : « contient » ou « commence par », jamais de regex, toujours avec
  l'aperçu des opérations touchées avant d'enregistrer.

## Délier, annuler

- Le bouton qui refuse une paire dit ce qu'il fait : « Pas ensemble », et dessous « chacune restera comptée de son
  côté ». Il ne veut pas dire « compter en dépense ».
- Un refus ne vaut que pour **sa** paire. Il n'apprend rien sur les libellés. (Avant le 08/10, un seul refus bloquait
  toutes les paires aux libellés semblables, livrets compris : 8 virements vers le Compte plaisir perdus.)
- **Historique de toutes les actions** (paires liées et déliées, réponses, règles), chacune annulable. Une paire
  reconnue après coup ne remplace jamais en silence un type choisi à la main.

## Ordre de priorité d'un type

Ajustement de solde > paire reconnue > choix sur cette opération > dépôt prouvé > règle par nom > récurrent > défaut
(entrée = revenu, sortie = dépense).

Une paire avec exactement un compte d'épargne compte en Épargne ; entre deux comptes courants, ou entre deux épargnes,
elle n'est pas comptée.

## Ce qui ne décide jamais

Le dictionnaire de moyens de paiement (« CARTE », « VIR », « PRLV »…, `services/banking/operation_types.py`) ne décide
plus rien : ni quelle paire est proposée, ni quelle opération reçoit une question, ni quel dépôt est rapproché. Il
peut rester pour l'affichage. Les mots ignorés pour grouper les marchands servent à l'affichage, pas à décider d'un
regroupement de réponses.

## Récurrents (second temps)

Détection sans dictionnaire, par marchand, rythme et montant : un prix qui change, un nom qui change, une pause restent
le même récurrent. Les couches actuelles, mesurées sur des cas réels, sont gardées. Tant qu'un récurrent n'est pas
validé par l'utilisateur, il est proposé, jamais compté seul. Un loyer ou de l'argent de poche détecté est acceptable :
l'utilisateur valide ou refuse. Mesurer sur le dump avant de changer quoi que ce soit.

## État au 9 octobre 2026

| Point | État |
| --- | --- |
| Un refus ne vaut que pour sa paire | poussé (6a53229) |
| Règle « carte face à un virement reçu » (dictionnaire) | retirée |
| Libellé identique automatique, dépôt rapproché même pour une carte (session du 08/10) | défaits, jamais poussés |
| Réponse « celle-ci » par défaut, case « toutes celles de ce libellé, et les prochaines » | fait |
| Une sortie n'est demandée que lorsqu'un dépôt ou un retrait lui fait face | fait |
| Dépôt du même jour : question, puis automatique pour le même libellé vers le même compte | fait |
| « Celles que je coche » dans la liste | à faire |
| Liste « Sorties sans contrepartie » avec curseur (500 € par défaut), dépôts sans contrepartie, « X € à trier », phrase à l'ajout d'une banque | à faire |
| Paire proposée et dépôt possibles en même temps : une seule question | à faire |
| Historique et annulation, texte du bouton | à faire |
| Récurrents sans dictionnaire | second temps |
