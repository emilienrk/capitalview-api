"""
What a recurring payment is for, guessed from its merchant's words.

A nature is never detected from the amounts: it is read off the merchant, on a
closed list of names, and the user's own answer always wins over the guess.
Nothing is stored from here — the guess is made on every read, so growing the
list below improves old payments without a rebuild.

The list holds only names that mean one thing in France: `totalenergies` but
not `total` (a filling station), `sfr` but not `red`, `pret` but not `credit`
(every bank's label carries it). A word nobody recognises leaves OTHER, which
the user corrects in one click.
"""

from __future__ import annotations

from dtos.banking import RecurringNature

# Merchant word -> nature. Words are what `merchants.merchant_words` yields:
# lowercase, unaccented, no digits.
_WORDS: dict[str, RecurringNature] = {}


def _add(nature: RecurringNature, *words: str) -> None:
    _WORDS.update({word: nature for word in words})


_add(
    RecurringNature.HOUSING,
    "loyer", "loyers", "bail", "locataire", "location", "syndic", "copropriete", "immobilier",
    "immobiliere", "foncia", "nexity", "citya", "sergic", "sci", "hlm", "habitat", "logement",
)
_add(
    RecurringNature.ENERGY,
    "edf", "engie", "totalenergies", "eni", "ekwateur", "enercoop", "iberdrola", "vattenfall",
    "electricite", "energie", "energies", "gaz", "veolia", "suez", "saur", "eaux", "assainissement",
)
_add(
    RecurringNature.TELECOM,
    "orange", "sfr", "bouygues", "free", "sosh", "prixtel", "coriolis", "lebara", "lycamobile",
    "telecom", "telecoms", "fibre", "mobile",
)
_add(
    RecurringNature.INSURANCE,
    "macif", "maif", "maaf", "matmut", "gmf", "axa", "allianz", "allsecur", "groupama", "mma",
    "generali", "swisslife", "luko", "lemonade", "acheel", "assurance", "assurances", "assur",
    "mutuelle", "harmonie", "mgen", "malakoff", "apivia", "aesio", "prevoyance",
)
_add(
    RecurringNature.CREDIT,
    "pret", "prets", "emprunt", "cofidis", "cetelem", "sofinco", "younited", "franfinance",
    "cofinoga", "oney", "floa",
)
_add(
    RecurringNature.TRANSPORT,
    "sncf", "ouigo", "navigo", "ratp", "keolis", "transdev", "tisseo", "velib", "citiz",
    "blablacar", "trainline", "autoroute", "peage",
)
_add(
    RecurringNature.SPORT,
    "fitness", "fit", "gym", "sport", "sports", "basicfit", "keepcool", "neoness", "crossfit", "yoga",
    "musculation", "piscine", "escalade", "arkose",
)
_add(
    RecurringNature.LEISURE,
    "netflix", "spotify", "deezer", "disney", "canal", "pathe", "ugc", "cgr", "cinema", "cinepass",
    "crunchyroll", "molotov", "audible", "twitch", "patreon", "playstation", "xbox", "nintendo",
    "steam", "supercell", "ubisoft", "blizzard",
)
_add(
    RecurringNature.SOFTWARE,
    "anthropic", "claude", "openai", "chatgpt", "github", "gitlab", "ovh", "ovhcloud", "scaleway",
    "hetzner", "cloudflare", "digitalocean", "vercel", "netlify", "adobe", "jetbrains", "figma",
    "notion", "dropbox", "icloud", "apple", "microsoft", "google", "gandi", "namecheap", "canva",
    "midjourney", "zoom", "slack",
)


# What a month cannot avoid: a roof, the power it takes, the insurances the
# two demand and a credit already signed. The rest can be stopped tonight.
FIXED = frozenset({
    RecurringNature.HOUSING, RecurringNature.ENERGY, RecurringNature.INSURANCE, RecurringNature.CREDIT,
})


def is_fixed(nature: RecurringNature) -> bool:
    return nature in FIXED


def of(nature: str | None, words: tuple[str, ...] | list[str]) -> RecurringNature:
    """What the user said it is for, else what its merchant says."""
    return RecurringNature(nature) if nature else guess(words)


def guess(words: tuple[str, ...] | list[str]) -> RecurringNature:
    """The nature the merchant's words name, the first one that tells.

    Words keep the label's order: a merchant says what it is before the rest
    (`ARVERNE FITNESS BREZET` is a gym, whatever Arverne and Brezet are)."""
    for word in words:
        nature = _WORDS.get(word)
        if nature is not None:
            return nature
    return RecurringNature.OTHER
