# CapitalView API — consignes pour les sessions de développement

## Documentation de référence, à lire avant de coder

| Avant de toucher | Lire |
| --- | --- |
| `services/banking/`, `routes/banking.py`, `dtos/banking.py`, `mcp_server/tools.py` (partie banque) | `docs/bank-sorting.md` : règles R1 à R53, textes d'interface, journal |
| `services/bank_ledger.py`, `services/imports/bank_csv.py`, `routes/bank.py` | `docs/bank-ledger.md` : règles L1 à L9, journal |
| `services/crypto_transaction.py`, `services/crypto_account.py` | `docs/crypto-ledger.md` |
| tout ce qui touche une devise | `docs/currencies.md` |

Ces documents décrivent le comportement voulu, et le code s'y conforme. Un écart entre les deux est un bug à trancher,
jamais à suivre en silence : on corrige le code ou la règle, dans le même commit. Ils ne contiennent que ce qui est en
place, jamais de plan ni de tâche à venir. Les docstrings des modules renvoient à la règle qu'ils portent
(« Rules: docs/bank-sorting.md — R… ») et gardent le *pourquoi* local ; les mesures et les décisions datées vivent dans
la doc, pas dans le code.

## Tout changement de comportement

1. Trouver la règle concernée (R… ou L…). Si elle change, la réécrire ; si c'est une nouvelle, la numéroter à la suite.
2. Ajouter une entrée datée au journal de la doc : la décision, et la mesure sur le dump de développement, avant et
   après (paires, types, questions, totaux). Rien ne se retire de la doc : ce qui n'est plus une règle va au journal.
3. Respecter les deux invariants techniques : une seule lecture d'une opération (`flows._filed`, R51), et tout ce que
   la lecture consomme entre dans `transfer_patterns.source_digest`, avec `_VERSION` qui monte quand la dérivation
   change (R52).
4. Un test par règle touchée, nommé comme une phrase (`test_a_file_reaching_over_the_opening_balance_carries_it_back`).

## Conventions

- Code, commentaires et messages de commit en anglais ; docs utilisateur et textes d'interface en français.
- Commits : `type(scope): sujet` à l'impératif, en minuscules (`fix(bank): …`, `docs(bank): …`, `feat(placements): …`),
  un corps d'une ou deux phrases qui dit le pourquoi.
- Migrations : toujours `alembic revision --autogenerate`, jamais un id écrit à la main (voir README).
- Tests : `ENV=test SECRET_KEY=… ENCRYPTION_KEY=… uv run pytest -q` (voir `.github/workflows/ci.yml` pour les
  variables). Les tests banking vivent dans `tests/services/test_banking_*.py` et `tests/services/test_bank_ledger.py`.
- Rien de sensible en clair : les montants, dates et libellés sont chiffrés, les jointures passent par des blind index.
