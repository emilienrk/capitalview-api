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

Une question n'est posée que lorsque **quelque chose est en face**, ou qu'une sortie **dépasse le seuil** choisi par
l'utilisateur.

- **Paire possible** entre deux comptes (sens opposés, même montant, au plus 2 jours ouvrés d'écart), qu'aucune règle
  ci-dessus ne confirme. Y compris entre deux comptes courants, même avec le même libellé, le même jour et le même
  montant. Une paire seulement proposée ne change aucun total.
- **Dépôt Bourse/Crypto possible** : même montant, dans les 3 jours, ou avec un écart de frais. Si une paire et un
  dépôt sont tous deux possibles, la même question montre les deux : « C'est… vers <compte> » ou « versement sur
  <compte d'investissement> » (la paire écartée, l'opération comptée en investissement). Tant que la paire est
  proposée, le dépôt n'est qu'affiché, même déjà validé pour ce libellé. Mesuré sur le dump : un cas (−20 € du
  05/02/2024), dont le dépôt s'affichait jusque-là, à tort, sur une autre sortie de 20 € deux jours plus tard.
- **Sortie sans rien en face, au-dessus du seuil** du curseur (500 € par défaut, réglable). Le seuil porte sur
  l'opération, pas sur le total du libellé. Les questions restent regroupées par libellé, « celle-ci » par défaut.
  Mesuré sur un historique de 4 ans : 6 opérations à 1 000 €, 20 à 500 €, 54 à 250 €, 139 à 100 €. À 100 €, on
  retrouve à peu près les questions d'avant le 09/10, cartes comprises : sans dictionnaire, un achat carte et un
  virement ne se distinguent pas.
- **Entrée sans rien en face** (« revenu ou remboursement ? »), groupée par nom, si le groupe dépasse 100 €. Seul
  l'utilisateur sait si les 50 € d'un ami sont un remboursement.

Les questions sont rangées par année, les plus récentes d'abord.

## Ce qui ne reçoit pas de question

**Une sortie sans rien en face, sous le seuil**, est une dépense : si tous les comptes d'épargne et d'investissement
sont dans l'appli, elle ne peut être que ça. Le curseur sert à ça : en le bougeant, on voit combien de sorties
passeraient en question, et on attrape un compte oublié (3 000 € partis vers un PEA ouvert ailleurs). Autour :

- dans l'onglet Comptes, dès qu'il y a un compte, une phrase qu'on peut fermer (fermée pour de bon sur ce
  navigateur) : « Ajoutez aussi vos livrets et vos comptes de bourse : sinon, les virements vers eux compteront comme
  des dépenses. » ;
- à côté des totaux du Réel : « X € à trier (N opérations) », avec un lien vers « À trier ».

**Les dépôts sans contrepartie** sont listés aussi, sous les questions d'« À trier » : les dépôts (et les retraits)
déclarés sur un compte Bourse, Crypto ou un placement pour lesquels aucune opération bancaire n'a été trouvée. C'est
l'inverse du curseur : il attrape un compte d'investissement oublié, cette liste attrape un compte bancaire oublié. Ils
ne reçoivent pas de question et ne changent aucun total.

- Est « en face » toute opération de sens opposé, même montant ou un écart de frais, à 3 jours au plus, quel que soit
  son type. Une paire déjà reconnue entre deux comptes bancaires ne compte pas ; une paire seulement proposée, si.
- Un pour un : une sortie de 200 € ne couvre pas deux dépôts de 200 €.
- Seuls les dépôts compris dans l'historique bancaire sont jugés, et pas les 3 derniers jours, où l'autre côté peut
  ne pas être encore passé.
- Mesuré sur le dump : 64 dépôts déclarés, 3 sans contrepartie (500 € sur un compte « trst », 90,17 € et 180 € sur le
  portefeuille crypto).

## Répondre

- **Celle-ci** : le choix par défaut.
- **Celles que je coche** : parmi les opérations du même nom, listées (« Choisir parmi les N opérations de ce
  libellé »). Liste ouverte, la réponse porte sur les opérations cochées, celle de la question cochée d'avance ;
  fermée, sur celle-ci. Chacune reçoit son propre type, comme si on avait répondu une par une : aucune règle, rien
  pour les prochaines. Seules des opérations du même compte et du même sens peuvent être cochées ensemble.
- **Les prochaines aussi** : case explicite, qui crée une règle visible dans les réglages, supprimable.
- Une règle par nom n'empêche **jamais** une paire trouvée plus tard : la paire passe avant.
- Une règle par nom écrite à la main (second temps) : « contient » ou « commence par », jamais de regex, toujours avec
  l'aperçu des opérations touchées avant d'enregistrer.

## Délier, annuler

- Le bouton qui refuse une paire dit ce qu'il fait : « Pas ensemble », et dessous « chacune restera comptée de son
  côté ». Il ne veut pas dire « compter en dépense ». La paire proposée est posée en toutes lettres sur la ligne
  (« C'est… vers <compte> / Pas ensemble »), plus par deux icônes ✓ ✗.
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
| « Celles que je coche » dans la liste | fait |
| Sortie sans contrepartie au-dessus du seuil = question, curseur (500 € par défaut, Paramètres › Modules) | fait |
| Dépôts sans contrepartie, « X € à trier », phrase à l'ajout d'une banque | fait |
| Paire proposée et dépôt possibles en même temps : une seule question | fait |
| Texte du bouton : « Pas ensemble », question écrite sur la ligne | fait |
| Historique et annulation | à faire |
| Récurrents sans dictionnaire | second temps |
