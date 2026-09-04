# Reprise du chantier Enable Banking — état au 2026-08-25

Ce document est autosuffisant : il dit où en est le travail, ce qui a été décidé et pourquoi, et ce
qu'il reste à faire. Il est suivi par git et voyage avec la branche.

> **Le ledger détaillé est dans `.superpowers/sdd/2026-08-17-enable-banking-connection/progress.md`,
> qui est git-ignoré et donc LOCAL à la machine d'origine.** Il contient l'historique complet, les
> rapports de chaque tâche et les paquets de revue. Si tu reprends sur une autre machine, ce document-ci
> est ta seule source ; s'il est disponible, le ledger le complète.

## Documents de référence

| Quoi | Où |
| --- | --- |
| Plan d'implémentation (12 tâches) | `docs/superpowers/plans/2026-08-17-enable-banking-connection.md` |
| Spec de conception (le pourquoi) | `docs/superpowers/specs/2026-08-17-enable-banking-connection-design.md` |
| Contrat d'API figé | `.superpowers/sdd/2026-08-17-enable-banking-connection/api-contract.md` (local) |
| Contraintes globales | `.superpowers/sdd/2026-08-17-enable-banking-connection/constraints.md` (local) |
| Doc vendeur, contrat OpenAPI, jeux de données réels | `../vendor-docs/` (hors dépôt git) |
| **Flux observés et virements internes (lot 3)** | **`REPRISE-flux-observes.md`, à côté de ce fichier** |

**Méthode employée :** `superpowers:subagent-driven-development` — un sous-agent implémenteur par tâche,
une revue après chaque tâche, des rounds de correction, puis une revue finale de branche.
Tous les dispatches sur **Opus 5** (préférence explicite d'Emilien ; Sonnet réservé au trivial).

## État

Branche `feat/enable-banking-connection` dans **les deux dépôts**.

| Tâche | État | Commit |
| --- | --- | --- |
| 1 — identifiants BYO chiffrés | ✅ revue clean | `801d4d9` |
| 2 — client Enable Banking | ✅ après 1 round | `a313e79` |
| 3 — tables session/lien/autorisation | ✅ après 1 round | `01a72a0` |
| 4 — parcours de liaison | ✅ après 1 round | `6b1b48b` |
| 5 — transactions + déduplication | ✅ après 1 round | `502dc61` |
| 6 — synchro, ancres, réconciliation | ⚠️ implémentée, **re-revue jamais faite** | `6fae804` |
| 9+10 — front (groupées) | ✅ revue clean (2 rounds) | `26e6825` (web) |
| R18 front — alerte pilotée par `reconciliation_status` | ⚠️ implémentée, **non revue** | `8b83791` (web) |
| 7+8 — consentements + suppression compte | ⚠️ implémentées, **non revues** | `d77a48f` |
| 11 — import d'un export | ⚠️ implémentée, **non revue** | `17d6de5` |
| 12 — validation à l'échelle réelle | ⚠️ implémentée, **non revue**, et les 2 ⚠️ non levés | `4d53304` |
| Revue finale de branche | ❌ à faire, **sur Opus, sur tout le diff** | — |
| 13 — opt-in `open_banking_enabled` + listing des connexions | ⚠️ implémentée, **non revue** | voir ci-dessous |

Suite API : **1061 tests verts** (vérifiés le 25/08). Front : `pnpm type-check` propre, **85 tests**.

## Ajout du 2026-08-26 — la fonctionnalité devient opt-in

Demandé par Emilien : la connexion d'un vrai compte bancaire ne doit pas être un élément principal
de l'app, tout en restant faisable. Ajouté par-dessus les 12 tâches, **non revu comme le reste**.

- **`user_settings.open_banking_enabled`**, défaut `false`, migration `c4b81e07d5a3`. Distinct de
  `bank_module_enabled` (défaut `true`, qui n'affiche/masque que le module Banque) — ne pas confondre.
- **`require_open_banking`** (`routes/banking.py`) refuse en 403 tout ce qui **ouvre, prolonge ou
  alimente** une connexion : `PUT /credentials`, `GET /aspsps`, `POST /authorize`,
  `GET|POST /sessions/{uuid}/…`, `POST /sync`, `POST /import-export`.
  **Délibérément non appliqué** à `GET /status`, `GET /check`, `GET /sessions`,
  `DELETE /sessions/{uuid}` et au callback : se désactiver doit laisser voir et démonter ce qui est
  déjà rattaché, et ne doit pas abandonner un parcours en vol chez la banque.
- **`GET /banking/sessions`** (`list_bank_sessions`) : une ligne par autorisation, avec son statut,
  son message, sa date d'expiration et les comptes CapitalView rattachés. Les sessions retirées y
  restent — leurs `BankAccountLink` survivent à l'expiration par conception.
- Front : toggle en tête de l'onglet Banque (patron `ai_feature_enabled`), tout le reste derrière ;
  panneau « Connexions bancaires » affiché **même feature éteinte** ; boutons « Connecter une banque »
  et « Synchroniser » retirés de `Bank.vue` et auto-synchro coupée quand l'opt-in est éteint.
- Tests : +10 dans `tests/routes/test_banking_linking.py`, dont le gate **prouvé non-vide par
  mutation** (retirer la dépendance de `POST /sync` rend le cas rouge). Suite API **1095 verts**,
  front type-check propre et 85 tests.
- ~~**Non fait, volontairement** : aucune UI de déconnexion, aucune UI d'import d'export.~~
  **Faites le 2026-08-26** (`caa312e` web). La déconnexion appelle bien `DELETE /sessions/{uuid}` —
  le ruling R3 reste vrai pour autant : la fermeture du consentement côté Enable Banking n'a
  toujours jamais été exercée pour de vrai, seulement contre un double.

## ⚠️ Mise à jour du 2026-08-25 — implémentation complète, revue absente

Les 12 tâches sont implémentées. **Suite API : 1061 tests verts** (relancés et vérifiés le 25/08).
Front : type-check propre, 85 tests. Arbres de travail propres dans les deux dépôts.

**Mais rien de ce qui a été produit après le 17/08 n'a été revu**, et le process de revue est ce qui a
trouvé tous les défauts graves de ce chantier. Concrètement, il manque :

1. **La re-revue de la Task 6.** Elle n'a jamais tourné. Le correctif `6fae804` **change la sémantique
   de `anchor_balance_enc`** — de « le solde au moment où on a regardé » à « le solde de clôture de la
   veille ». Personne n'a vérifié la cohérence entre synchros successives (l'ancre de clôture de N doit
   être l'ancre d'ouverture de N+1), le cas d'amorçage, ni les autres lecteurs du champ.
2. **Tasks 7, 8, 11, 12 — aucune revue, aucun rapport.** Commits `d77a48f`, `17d6de5`, `4d53304`.
3. **La revue finale de branche**, et le tri des ~25 findings Minor différés.
4. **Les deux ⚠️ ne sont pas levés.** `tests/integration/test_banking_e2e.py:143` code
   `"cash_account_type": "CARD"` **en dur dans un double** : le test *suppose* ce qu'il fallait
   *vérifier*. Aucune capture nouvelle dans `vendor-docs/spike/`, aucun `GET /aspsps`. R12, R18 et R19
   reposent toujours sur un champ jamais confronté à la vraie banque.
5. **Aucune des deux branches n'est poussée.**

**Vert ≠ correct.** La Task 5 l'a démontré : son test de transition en attente→comptabilisée passait sur
une forme de donnée que la banque n'a jamais produite en 294 lignes réelles.

## Premier geste à la reprise

**Lancer la revue finale de branche** (Opus, sur tout le diff `main..HEAD`), puis la re-revue de la Task 6 sur `60d764d..6fae804`. Elle a été coupée en plein travail.
Le paquet de revue existe déjà (`review-60d764d..6fae804.diff`) si le workspace local est intact ;
sinon le régénérer avec `git diff -U10 60d764d..6fae804`.

Ce qu'elle devait juger en priorité : la **nouvelle sémantique d'ancre**. Le correctif est allé plus
loin que prescrit — `anchor_balance_enc` ne signifie plus « le solde au moment où on a regardé » mais
« le solde de clôture de la veille » (`comptable − mouvements comptabilisés aujourd'hui`). À vérifier :
la soustraction dans les deux sens, la cohérence entre synchros successives (l'ancre de clôture de la
période N doit être l'ancre d'ouverture de N+1, sans jour compté deux fois ni sauté), le cas de la
passe d'amorçage sans ancre précédente, le cas « aucun mouvement aujourd'hui », et tout autre lecteur
de `anchor_balance_enc` / `anchor_date`.

Ensuite : les revues manquantes des Tasks 7+8, 11, 12 et du commit front `8b83791`, puis le tri des
findings Minor différés. **Il ne reste aucune implémentation à écrire — seulement à vérifier.**

## Revue de la Task 6 — faite le 2026-08-26

Menée à la main (les sous-agents ont échoué sur une limite de session). **La sémantique d'ancre est
correcte**, vérifiée algébriquement et non à l'œil :

- L'écriture pose `ancre = comptable(T) − mouvements(T)` ; la lecture calcule
  `écart = comptable(T′) − (ancre + Σ_{d≥T})`, qui se réduit à
  `comptable(T′) − comptable(T) − Σ_{d>T}` — nul par définition. **Continuité bonne entre synchros
  successives** : aucun jour compté deux fois ni sauté.
- Le cas redouté fonctionne : des lignes datées de T **comptabilisées après** la synchro de T
  s'annulent terme à terme, parce que `comptable(T′) − comptable(T)` en contient la différence.
- « Aucun mouvement aujourd'hui » : l'ancre vaut le comptable, la période suivante repart de là.
- Les **cinq** lecteurs/écrivains ont été retrouvés et vérifiés (`linking.py:647`, `sync.py:288`,
  `sync.py:582`, `export_import.py:250`, `bank.py:51` + `sync.py:203`).

### 🔴 Majeur trouvé et corrigé — l'import d'export annulait l'amorçage

`export_import.py` avançait `last_synced_at` jusqu'à rejoindre `anchor_date`. Or `seeding` s'en
déduit (`last_synced_at < anchor_date`, `sync.py:203`), et `sync.py:219-220` choisit sur lui
`longest`+`SEED_DATE_FROM` contre `default`+ancre.

Le garde `ref_date < anchor_date` protège les exports historiques, **mais pas celui daté du jour** —
qui est le cas courant : on connecte la banque, puis on importe l'export qu'on vient de télécharger.
`seeding` basculait à faux, la première synchro n'allait plus chercher l'historique profond, et le
marqueur ne redevenait jamais vrai. Tout ce que l'export ne contenait pas était perdu sans un mot
(piège n°6 : `longest` seul s'auto-limite à 2 ans).

**Correctif** : l'import ne touche plus `last_synced_at` — un import n'est pas une synchro. Deux
tests le figent, **prouvés non-vides par mutation** (réintroduire la ligne les rougit tous les deux).

### Mineur non corrigé

L'ancre créée dans `linking.py:648` vaut le solde CapitalView « à l'instant », pas « à la clôture de
la veille » — une **troisième convention**. Elle n'est jamais lue, la réconciliation étant sautée
pendant l'amorçage. Correct aujourd'hui, mais le couplage est implicite : rendre l'amorçage
réconciliable casserait le calcul en silence.

## Revue du cœur monétaire — partielle, 2026-08-26

Les sous-agents ont échoué deux fois sur une limite de session. Fait à la main, sur invariantes
plutôt que par lecture ligne à ligne.

**Ce qui tient**, vérifié en rejouant les 4 240 opérations réelles :
- **Idempotence** : trois passes du même export donnent 2 804 lignes et un net de −73,63 € à
  l'identique. Une resynchro ne corrompt rien.
- **Ordre du flux** : mélanger l'ordre des transactions dans un compte ne change rien. Le
  commentaire « the feed's order is never relied upon » est exact.

### 🔴 Majeur trouvé et corrigé — l'import d'export n'appliquait pas R12

Mesuré, pas supposé :

| ordre de stockage | lignes | net |
| --- | --- | --- |
| courant puis carte | 2 804 | −73,63 € |
| **carte puis courant** | **2 798** | **+136,07 €** |

**Six opérations réelles disparaissent et 209,70 € d'écart**, selon le seul ordre de stockage. R12
avait vu l'asymétrie mais l'avait chiffrée comme « la courbe d'un des deux comptes est fausse » —
c'est en réalité de l'argent qui s'évapore au niveau utilisateur.

`sync_user_accounts` applique bien `_in_sync_order`. **`import_enablebanking_export` bouclait sur
`accounts_data` dans l'ordre du fichier**, sans aucun tri. L'export d'Emilien liste le compte courant
en premier, par chance ; rien ne le garantit pour un autre export ou un autre utilisateur.

**Correctif** : `_in_import_order()` applique le même prédicat `is_card_account` que la synchro, les
comptes non rattachés restant en fin (ils ne stockent rien). Un test rejoue le vrai export **à
l'envers** et exige le même résultat ; retirer le tri le rougit.

### Niveaux 1-2 de déduplication — mesurés, deux mineurs

Quatre cas limites exercés sur une base neuve :

| cas | résultat |
| --- | --- |
| deux opérations distinctes, réfs différentes, même jour/montant | **les deux survivent** ✅ |
| deux opérations distinctes sans réf, dans le **même** appel | **les deux survivent** ✅ (jeu `touched`) |
| deux opérations distinctes sans réf, en **deux** appels | une seule survit ⚠️ |
| une opération en attente + une **autre** comptabilisée, même montant | l'attente est absorbée ⚠️ |

**Les deux ⚠️ se réparent d'eux-mêmes**, vérifié : dès que la banque renvoie les deux lignes dans un
même flux — ce que la fenêtre de synchro garantit, puisqu'elle s'ouvre **sur** l'ancre et re-couvre
donc le jour courant — les lignes perdues reviennent. Ce sont des sous-comptages **transitoires**
entre deux synchros du même jour, pas des pertes définitives, et l'écart de réconciliation les
signale. **Mineurs**, pas de correctif.
Boursorama remplit `entry_reference` à 100 % : le 3e cas ne peut pas se produire chez Emilien.

### Sécurité — vérifiée, rien à signaler

- **Aucune fuite de clé privée** : elle n'apparaît ni dans les journaux, ni dans une réponse d'API
  (`BankConnectionStatus` ne porte qu'un booléen), ni dans une URL. Testé : une clé malformée,
  une clé publique et une chaîne vide donnent toutes `InvalidKeyError: Could not parse the provided
  public key` — PyJWT n'écho jamais le matériel fourni, donc le `error=f"...{exc}"` renvoyé au
  navigateur par `check_configuration` est sûr en pratique. *Reste un `except Exception` fourre-tout :
  risque latent si un autre type d'exception y atterrit un jour.*
- **`state` du callback** : `secrets.token_urlsafe(32)`, stocké en index aveugle dérivé de la Master
  Key (donc inutilisable sans le cookie du bon utilisateur), expiration filtrée en SQL,
  **consommé à l'usage** (`linking.py:336`) et purgé au refus (`:398`).
- **Propriété des ressources** : `_load_owned_session` sur les trois routes de session,
  `link_account` vérifie en plus que le compte cible appartient à l'utilisateur, et
  `PUT /cashflow/{id}/match` contrôle le sien.

### Front — deux silences corrigés

`updateCashflow` avale son erreur et répond `null`. Les deux actions des cartes de comparaison
ignoraient ce retour : « mettre le prévu à X » se redessinait comme fait, et « désactiver ce flux »
retirait la carte alors que le flux restait actif et continuait d'alimenter la prévision.

### Reste à faire

- Lecture ligne à ligne de `flows.py` et `matching.py` (écrits aujourd'hui, jamais relus par un tiers).
- Machine à états de `BankLinkModal` : chemins de sortie relus, `close`/`discard` correctement
  distingués, mais **toujours aucun test** — le dépôt n'a ni `@vue/test-utils` ni environnement DOM.
- Tri des ~25 findings Mineur différés du chantier d'origine.

## Les rulings — décisions prises au nom d'Emilien

Chacun est une décision que le plan ou la spec ne tranchait pas. **Ils sont tous rejouables et réversibles.**

| # | Décision | Coût si faux |
| --- | --- | --- |
| R1 | Le spike est `vendor-docs/spike/eb_spike.py`, pas `scratchpad/` (chemin faux dans le plan) | nul |
| R2 | Le client s'obtient par une fabrique module-level `build_client(...)` que les tests monkeypatchent, pas par un conteneur d'injection | un point d'appel à refactorer |
| R3 | Le code de fermeture de session Enable Banking est écrit mais **jamais exercé pour de vrai** | un consentement fermé = nouvelle authentification forte |
| R4 | La validation « bout en bout » (Task 12) se fait **hors réseau**, en rejouant les jeux réels locaux | le parcours réseau réel reste couvert par la seule validation manuelle du 17/08 |
| R5 | Les `BankAccountLink` sont créés à l'étape de rattachement, pas au callback (`bank_account_uuid_bidx` est UNIQUE vers un compte CapitalView qui doit exister d'abord) | une étape d'UI en plus ou en moins |
| R6 | `BankAccountResponse` porte les métadonnées de lien lues par le front | le champ atterrit ailleurs |
| R7 | Le marquage « estimé » est **dérivé** de `anchor_date`, pas stocké | une colonne à ajouter |
| R8 | Les consommateurs de `get_connection()` sont T4 et T6, pas T2/T5 comme l'annonçait le plan | nul |
| R9 | Branches créées dans les deux dépôts depuis `main` | nul |
| **R10** | **`bank_sessions.accounts_enc`** : le callback persiste la charge complète des comptes. `POST /sessions` renvoie des `AccountResource` riches, `GET /sessions/{id}` ne renvoie que `{uid, identification_hash, identification_hashes}` — le §C4 « fourni une seule fois » était littéralement inversé | une table à extraire si ces métadonnées devaient un jour être interrogées champ par champ |
| **R11** | **La devise entre dans `dedup_bidx`**, contre la lettre de §A5. La donnée réelle contient un débit CHF 12,63 ; un EUR 12,63 le même jour aurait partagé son empreinte et une opération aurait disparu | empreinte plus discriminante que prévu — comportement correct de toute façon |
| ~~R12~~ | ~~Ordre de synchro stable, compte courant avant compte carte.~~ **RETIRÉ le 2026-09-02** : l'invariant ne tenait qu'**à l'intérieur d'un run**. La carte d'Emilien s'est amorcée le 1er septembre pendant que son compte courant échouait ; amorcé le 2, celui-ci a perdu **1 442 de ses 2 776 mouvements** et sa courbe est passée de +541,81 € à −5 288,06 €. Remplacé par R21 et R22 | — |
| R13 | Un Minor (`ValueError` sur sens/statut manquants) ajouté au fix round de T5 car il partageait la mécanique du Critical | périmètre de fix un peu large |
| R14 | Le contrat d'API est **figé** dans `api-contract.md` et fait autorité **dans les deux sens** — c'est ce qui a permis de développer le front avant la synchro | le front s'ajuste sur les champs concernés |
| R15 | Parallélisation **entre les deux dépôts**, jamais à l'intérieur d'un dépôt (deux implémenteurs sur une même branche git s'écrasent) | aucun |
| R16 | **`POST /banking/sync` : corps vide, déclencheur global.** Un déclencheur par compte rendrait l'ordre de R12 au front, c'est-à-dire à personne | une route par compte s'ajouterait sans casser celle-ci |
| R17 | Quatre Minor ajoutés au fix round front (dont deux qui racontaient une contre-vérité à l'utilisateur) | périmètre un peu large |
| **R18** | **La réconciliation a trois issues** : `reconciled` / `gap` / `not_reconcilable`. Un compte carte a un écart **permanent et normal** ; une alerte permanente sur un fonctionnement normal détruit le signal sur *tous* les comptes | un état d'affichage à retirer |
| **R19** | **Sur un compte non réconciliable, la synchro n'écrit AUCUNE courbe rétrospective.** Justification refondue le 2026-09-02 : ce n'est plus « la dédup n'en laisse qu'une fraction » (devenu faux avec R22) mais **le solde d'une carte à débit immédiat n'est pas une grandeur de stock**. La banque publie un unique `OTHR` et aucun `CLBD` ; reconstruire à rebours depuis cet `OTHR` de 0 fabrique **+27 887 € dix-huit mois en arrière**, l'historique des dépenses relu comme un solde | un compte carte sans courbe rétrospective — réversible, contrairement à la destruction |
| **R21** | **Les comptes carte ne sont plus rattachables du tout.** `list_session_accounts` les écarte et `link_account` les refuse en 409. `cash_account_type` est `required` au contrat, donc le marqueur est présent chez toutes les banques | un compte carte qu'on ne peut plus suivre — réversible, contrairement à la perte de données |
| **R22** | **Le niveau 3 de déduplication (croisé, cadré utilisateur) est supprimé.** Il n'avait aucun consommateur : toute lecture de `BankTransaction` filtre sur un seul `account_id_bidx`, les soldes viennent de la banque et chaque courbe n'utilise que ses propres lignes | les opérations d'un lien carte hérité existent en double dans les données et l'export — irréversible pour les lignes déjà stockées, le rallumer ne filtrerait que les insertions |

## Pièges vérifiés sur le terrain — ne pas les rouvrir

1. **`import_bank_account_history(overwrite=True)` (`services/bank.py`) supprime TOUT l'historique du
   compte**, pas la fenêtre. `replace_history_window()` existe pour ça. Le test bloquant a été **prouvé
   non-vide par mutation** : substituer `overwrite=True` rend 4 tests rouges.
2. L'`uid` de compte expire avec la session ; **`identification_hash` est la clé durable**.
3. **Une page de pagination vide n'est pas la fin** — on s'arrête sur l'absence de `continuation_key`,
   qui voyage **avec** les paramètres d'origine.
4. **Les descriptions d'énumérations du fichier OpenAPI sont désalignées de leurs valeurs.** Mapper par
   nom, jamais par position.
5. **93 % des transactions carte existent aussi sur le compte courant, sans référence commune.**
   ~~La dédup croisée est scopée **utilisateur**, pas compte.~~ **La mesure reste vraie** (98,1 %
   recalculé sur l'export), **la conclusion est retirée** : voir R21 et R22. On ne déduplique plus
   entre comptes, on n'accepte plus de rattacher un compte carte.
6. `strategy=longest` **seul** s'auto-limite à 2 ans : le premier import exige `longest` **et** un
   `date_from` très ancien.
7. **Ne jamais appeler `DELETE /sessions/{id}` pour de vrai.**

## Points ouverts à traiter avant de clore

- ~~**⚠️ `cash_account_type == "CARD"` n'est pas vérifié contre la vraie banque.**~~ **Levé le
  2026-08-26.** Le marqueur était déjà dans les données réelles : `export-boursorama-2022-2026.json`,
  `.accounts[].info.cash_account_type` vaut **`CACC`** sur le compte courant (« CAV - BOURSOBANK ») et
  **`CARD`** sur le compte carte (« Carte Visa Ultim », `details: "immediat_debit"`). Le contrat le
  confirme : `cash_account_type` est dans le bloc `required` de `AccountResource`
  (`enablebanking-api.yaml:1866`), et `AuthorizeSessionResponse.accounts` est un `AccountResource[]` —
  `POST /sessions` ne peut donc pas l'omettre. R12, R18 et R19 reposent sur un champ réel et
  correctement valué. *Reste non prouvé* : qu'une **autre** banque étiquette pareil sa carte —
  `card_marker_missing` garde donc son utilité.
  Au passage, `currency` vaut bien `"XXX"` sur les deux comptes, ce que le code suppose déjà.
- ~~**⚠️ Le sélecteur de banque** : l'hypothèse de préfixe des caisses régionales n'est pas
  confirmée.~~ **Levé le 2026-08-26** par `vendor-docs/spike/aspsps-FR-2026-08-26.json` (capture
  réelle, HTTP 200, 129 établissements) : **aucune entrée générique n'existe**. Ni « Crédit Agricole »,
  ni « Banque Populaire », ni « Caisse d'Épargne » seuls — les 68 caisses régionales sont listées
  comme entrées à part entière (« Crédit Agricole du Languedoc », « Banque Populaire du Nord », …).
  `BankLinkModal` ne déclenche son étape `region` que sur une égalité **exacte** avec un nom de
  `REGIONAL_NETWORKS`, qui ne peut donc jamais se produire : **l'étape régionale est du code mort**
  (`REGIONAL_NETWORKS`, `loadRegionalOptions`, `retryRegionalOptions`, `filteredRegionalOptions`,
  `selectRegional`, l'état `region`). À supprimer.
  *Dernière vérification à faire à la main* : ouvrir le widget et cliquer un Crédit Agricole, pour
  écarter le cas où il grouperait par réseau et émettrait le nom du groupe.
- **`locale="FR"` sur le sélecteur** reste non documenté pour ce composant (inoffensif s'il est ignoré).
- ~~L'étape régionale de `BankLinkModal` est à supprimer~~ **Fait le 2026-08-26** (−131 lignes).
  Confirmé par sonde manuelle : l'événement `selected` porte
  `{country: "FR", name: "Crédit Mutuel", beta: false, psuType: "personal", sandbox: false, service: "AIS"}`
  — le **nom du catalogue**, jamais un intitulé de réseau.
- ~~**🔴 DÉCISION EN ATTENTE — `no-beta` masque 73 % des banques françaises.**~~ **Tranché le
  2026-08-26 par Emilien : drapeau retiré, sans badge.** Un marqueur « bêta » aurait porté sur 94
  entrées sur 129 — trois sur quatre, donc aucun signal (raisonnement de R18). Le contexte
  ci-dessous reste pour mémoire. La même sonde a montré
  que le sélecteur n'affiche que **35 des 129 établissements** : `element.setAttribute('no-beta', '')`
  (`BankLinkModal.vue:100`) écarte les 94 marqués `beta: true`. Sont **inaccessibles** :
  **BNP Paribas**, **Société Générale** (particuliers — seules « Entreprises » et « Professionnels »
  passent), **La Banque Postale**, **AXA Banque**, les **39 Crédit Agricole** et les **15 Caisse
  d'Épargne**. Passent : Boursorama, Crédit Mutuel, CIC, LCL, les Banque Populaire, N26, Revolut…
  Le rapport de spike avait retenu `no-beta` comme « garde-fou qualité » sans mesurer son coût.
  **Le retirer** ouvre les quatre grandes banques de détail au prix d'intégrations que le fournisseur
  dit encore instables ; **le garder** rend la fonctionnalité inutilisable pour la majorité des
  Français. Trancher avant d'ouvrir la fonctionnalité à quelqu'un d'autre qu'Emilien (Boursorama
  passe, donc son propre cas fonctionne).
- **La machine à états de `BankLinkModal` n'a aucun test** — le dépôt n'a ni `@vue/test-utils` ni
  environnement DOM. Elle a été validée par relecture exhaustive des branches, pas par exécution.
- **`Bank.vue:144-159`** (pré-existant) ouvre une confirmation depuis une modale déjà ouverte : même
  patron d'empilement Teleport qu'un défaut déjà corrigé ailleurs. Pour la revue finale.
- **Divergence export ↔ API**, mesurée : la même opération est `CHF 12.63 / DBIT / PDNG` via l'API et
  `EUR -12.63 / DBIT / OTHR` dans l'export. Devise, **signe** et statut diffèrent. La dédup entre les
  deux chemins ne tient que par `entry_reference`. **Crucial pour les Tasks 11 et 12.**
- **`TZ` du conteneur de production** : `last_synced_at` est la date civile locale du serveur. En UTC
  avec un utilisateur à Paris, une synchro entre 00 h et 02 h se lit périmée côté front — un appel
  gaspillé, jamais de double synchro. `TZ=Europe/Paris` l'élimine. Décision de déploiement.
- Environ **25 findings Minor** différés à la revue finale, listés dans le ledger local.

## Tester en bac à sable — ajout du 2026-08-27

Enable Banking sépare deux environnements, **non transférables l'un vers l'autre**
(`vendor-docs/eb-docs-split/api-reference.md:277`) : une application `SANDBOX` n'atteint que des
banques simulées, une application `PRODUCTION` n'atteint que les vraies. Il faut donc **deux
applications** et **deux clés privées** — y compris quand c'est le même compte Enable Banking
(`linked-accounts.md:34`).

### Ce qui bloquait, et qui est levé

Le sélecteur de banque est le widget `<enablebanking-aspsp-list>`, et son attribut `sandbox` doit
correspondre à l'environnement de l'application (`widgets.md:157`, et la FAQ nomme la discordance
comme cause d'échec, `faq.md:756`). Il était absent : la sonde manuelle du 2026-08-26 renvoyait
`sandbox: false` dans l'événement `selected`. Avec une application bac à sable, l'utilisateur aurait
donc choisi une vraie banque dans la liste, et `POST /auth` aurait échoué.

`GET /application` porte déjà le champ `environment` (obligatoire dans `GetApplicationResponse`,
`enablebanking-api.yaml:2901`). Il est désormais remonté par `GET /banking/check` jusqu'au front, qui
en déduit l'attribut du widget. Aucune variable d'environnement : la même image sert les deux cas,
et un utilisateur en production n'a rien à configurer.

**Piège reconduit** : `x-enum-descriptions` d'`Environment` est décalé comme celui de `SessionStatus`
— `SANDBOX` y est décrit « Live production environment ». Apparier par **nom**, jamais par position.
Une valeur inconnue est ramenée à `None`, donc à production : montrer de vraies banques est le
défaut sûr, pointer une application vivante vers des banques simulées ne l'est pas.

### Ce qu'il reste à faire dans le portail (côté Emilien)

1. <https://enablebanking.com/cp/applications> → nouvelle application, environnement **Sandbox**.
   En bac à sable, l'enregistrement ne demande qu'un nom et les URL de redirection
   (`control-panel.md:59`) — ni description, ni politique de confidentialité.
2. Déclarer `http://localhost:8000/banking/callback` en redirect URL, **à l'identique**, sans
   paramètre de requête (spec §C3 : le portail refuse une URL qui en porte un).
3. Laisser le navigateur générer la clé : le `.pem` tombe dans les téléchargements, nommé par l'ID de
   l'application.
4. Dans CapitalView → Paramètres → Banque : activer la fonctionnalité, coller l'ID, déposer le `.pem`,
   puis « Vérifier ». Le diagnostic doit passer au vert et un bandeau « Application bac à sable »
   doit apparaître.

Pas d'étape d'activation en bac à sable : « Activate by linking accounts » ne concerne que les
applications de production (`control-panel.md`, section *Activation of Production Applications*).

### Banques simulées utiles

`GET /aspsps` porte aussi, en bac à sable uniquement, un bloc `sandbox.users`
(`SandboxInfo`, `enablebanking-api.yaml:3881`) : identifiant, mot de passe et OTP de la banque
simulée, servis par l'API elle-même. Non exploité — le sélecteur est le widget du fournisseur, qui
va chercher sa propre liste ; notre `GET /banking/aspsps` n'est appelé par personne.

Le sélecteur est figé sur `country="FR"` (`BankLinkModal.vue:28`). En bac à sable, deux entrées au
moins devraient s'y trouver — **Mock ASPSP** (annoncé « All countries », aucun identifiant demandé)
et **BBVA** (FR listé ; `user1` / `1234`, OTP `012345`). Les identifiants de tous les bacs à sable
sont dans `vendor-docs/eb-docs-split/sandbox.md`. **Non vérifié empiriquement** : que la liste FR ne
soit pas vide en bac à sable. C'est le premier écran à regarder.

Mock ASPSP se peuple depuis le panneau de contrôle et accepte un export JSON de vraie banque — celui
d'Emilien (`vendor-docs/spike/export-boursorama-2022-2026.json`, 4 240 opérations) rejouerait donc
ses propres données à travers le vrai chemin réseau.

### Ce que le bac à sable ne prouvera pas

Il exerce le parcours — autorisation, callback, session, rattachement, synchro — pas la fidélité des
données. Le fournisseur écrit lui-même que ses bacs à sable ne simulent pas fidèlement le vivant, et
Mock ASPSP rend les transactions **par lots de 10, les plus récentes d'abord** (`sandbox.md`,
*Limitations*) : la pagination et l'ordre y sont donc différents d'une vraie banque. Les rulings
appuyés sur des données réelles (R12, R18, R19, l'ancre, la dédup) restent adossés au rejeu du
spike, pas au bac à sable.

## Contraintes d'environnement

- Tests API : `uv run pytest` **exige `dangerouslyDisableSandbox: true`** (cache uv bloqué).
- Front : `pnpm type-check`. Éviter `pnpm build` (run-p).
- Tests sur **SQLite in-memory**, production **PostgreSQL**. `pg_insert` contourné par la fixture
  `sqlite_pg_insert` (`tests/services/test_bank.py`).
- ~~**`PRAGMA foreign_keys` n'est jamais activé**~~ **Faux, constaté le 2026-08-26** : écrire un
  `BankAccountLink` dont le `session_uuid` ne pointe sur aucune `BankSession` lève bien
  `IntegrityError: FOREIGN KEY constraint failed`. Les contraintes de clé étrangère **sont**
  exercées par les tests — toute fixture créant un lien doit d'abord créer sa session.
- **Aucun appel réseau réel dans les tests** — le client est injecté et doublé.
- Le tag `sdd-rescue-task10-preFix` protège un commit web orphelin d'une reprise antérieure.
