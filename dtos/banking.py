"""Enable Banking connection schemas (BYO application_id + private key)."""

from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, model_validator

from dtos.bank import ReconciliationStatus


class BankConnectionUpdate(BaseModel):
    """Update the Enable Banking application_id and/or private key.

    Field absent from the payload = unchanged; empty string = deletion of that
    field. Mirrors AIProviderUpdate / update_ai_provider.
    """
    application_id: str | None = None
    private_key: str | None = None


class BankConnectionStatus(BaseModel):
    """State of a user's Enable Banking connection (read-only).

    The private key is never returned, only whether it is configured.
    """
    has_credentials: bool
    application_id: str | None


# ---------------------------------------------------------------------------
# Linking flow DTOs (spec §C)
# ---------------------------------------------------------------------------


class BankConfigCheck(BaseModel):
    """GET /banking/check — the pre-flight diagnostic (spec §C1).

    One GET /application call tells us, in one shot, whether the key is
    valid, the application is active, and CapitalView's callback URL is
    among the declared redirect URLs.
    """
    configured: bool
    key_valid: bool
    application_active: bool
    callback_url_declared: bool
    callback_url: str
    # SANDBOX or PRODUCTION, straight from GET /application. Matched by NAME:
    # the contract's x-enum-descriptions for Environment are misaligned with
    # their values, exactly as for SessionStatus. `None` when unreachable.
    environment: str | None = None
    error: str | None = None


class AspspSummary(BaseModel):
    """One bank entry from GET /aspsps, trimmed to what the flow needs."""
    name: str
    country: str
    logo: str | None = None
    beta: bool = False
    maximum_consent_validity: int


class BankAuthorizeRequest(BaseModel):
    """Body of POST /banking/authorize — the bank the user picked."""
    aspsp_name: str
    aspsp_country: str


class BankAuthorizeResponse(BaseModel):
    """Response of POST /banking/authorize — where to send the browser next."""
    auth_url: str


class BankSessionAccount(BaseModel):
    """One account discovered in a bank session, for the rattachement step.

    The display fields come from the accounts payload of POST /sessions, kept
    in bank_sessions.accounts_enc: a later GET /sessions/{id} returns only
    uid + identification hashes, so nothing here could be re-read from the API.
    Without them the picker can only show opaque base64 hashes.
    """
    identification_hash: str
    name: str | None = None
    product: str | None = None
    currency: str | None = None
    cash_account_type: str | None = None
    usage: str | None = None
    # IBAN when the bank provides one, otherwise the "other" identification
    # (BBAN and friends) — AccountResource.account_id carries either.
    account_id: str | None = None
    linked: bool
    bank_account_uuid: str | None = None


class BankSessionLinkedAccount(BaseModel):
    """One CapitalView account attached to a bank session."""
    bank_account_uuid: str
    name: str
    last_synced_at: date | None = None


class BankSessionSummary(BaseModel):
    """GET /banking/sessions — one authorization the user has granted.

    Retired sessions stay in the list: their `BankAccountLink`s survive consent
    expiry by design, so the status is what tells the user a reconnection is
    the only thing missing.
    """
    uuid: str
    aspsp_name: str | None = None
    aspsp_country: str | None = None
    status: str
    status_message: str
    active: bool
    consent_valid_until: datetime
    authorized_at: datetime
    accounts: list[BankSessionLinkedAccount] = []


class BankAccountLinkRequest(BaseModel):
    """Body of POST /banking/sessions/{uuid}/link — rattachement to a CapitalView account."""
    identification_hash: str
    bank_account_uuid: str


class BankAccountLinkResult(BaseModel):
    """Response of POST /banking/sessions/{uuid}/link."""
    bank_account_uuid: str
    identification_hash: str
    reconnected: bool


class BankAccountUnlinkResult(BaseModel):
    """Response of DELETE /banking/accounts/{uuid}/link.

    `reseeded_accounts` are the accounts that were being deduplicated against
    the detached one and are now scheduled for a full re-seed: whatever the
    detached account had shadowed can finally be stored on them.
    """
    bank_account_uuid: str
    transactions_deleted: int
    reseeded_accounts: list[str]


# ---------------------------------------------------------------------------
# Synchronisation DTOs (spec §D)
# ---------------------------------------------------------------------------


class SyncStatus(str, Enum):
    """The branch one account's sync took."""
    SYNCED = "synced"
    # The once-a-day cap, a no-op and never an error. A failed attempt spends
    # the day too (ruling R25).
    SKIPPED_DAILY_CAP = "skipped_daily_cap"
    # The consent is gone; the link is preserved.
    RECONNECT_REQUIRED = "reconnect_required"
    ERROR = "error"


class BankAccountSyncResult(BaseModel):
    """What one linked account's sync did."""
    bank_account_uuid: str
    status: SyncStatus
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    # Rows the bank sent without an amount, a direction or a status. Dropped,
    # never fatal — the reconciliation gap is what makes them visible.
    malformed: int = 0
    removed: int = 0
    snapshots_written: int = 0
    reconciliation_gap: Decimal | None = None
    # None when no check could run yet (the seeding pass has no bank anchor to
    # compare against).
    reconciliation_status: ReconciliationStatus | None = None
    # The balance type this sync could read: CLBD, OTHR (card) or ITAV. Reported
    # so a support answer does not require a database read.
    balance_type: str | None = None
    # How many rows of the feed carried `balance_after_transaction`. Nothing
    # reads it yet: a bank that fills it hands over each day's balance directly,
    # which would make the walked-back curve unnecessary. Empty on every row of
    # the real Boursorama production capture, so it is measured before anything
    # is built on it.
    balance_after_rows: int = 0
    detail: str | None = None


class BankSyncResponse(BaseModel):
    """Response of POST /banking/sync. The front re-reads the accounts payload
    afterwards rather than depending on this shape (ruling R16)."""
    synced: int
    results: list[BankAccountSyncResult]


# ---------------------------------------------------------------------------
# Observed flows (the real counterpart of the declared cashflows)
# ---------------------------------------------------------------------------


class BankFlowMonth(BaseModel):
    """One calendar month of observed movement, in the response's currency."""
    period: str  # YYYY-MM
    inflow: Decimal
    outflow: Decimal
    net: Decimal
    inflow_count: int = 0
    outflow_count: int = 0


class BankFlowCurrencyTotal(BaseModel):
    """Movements in a currency other than the headline one, reported apart.

    Amounts arrive unconverted and with no exchange rate, so they are never
    folded into the main total.
    """
    currency: str
    inflow: Decimal
    outflow: Decimal


class BankFlowsResponse(BaseModel):
    """GET /banking/flows — what actually moved on the linked accounts."""
    currency: str
    months: list[BankFlowMonth]
    inflow: Decimal
    outflow: Decimal
    net: Decimal
    # Averaged over the months carrying data, not over the requested window.
    monthly_inflow: Decimal
    monthly_outflow: Decimal
    covered_months: int
    account_count: int
    # Named so a total spanning several accounts can be checked at a glance.
    account_names: list[str] = []
    # Movements paired as one transfer between two of the user's own accounts:
    # counted, reported, and kept out of the totals.
    internal_transfers_excluded: int
    internal_transfers_amount: Decimal
    # An operation and its cancellation or refund on one account.
    reversals_excluded: int = 0
    reversals_amount: Decimal = Decimal("0")
    # Not yet booked, so deliberately outside the monthly figures.
    pending_count: int
    pending_inflow: Decimal
    pending_outflow: Decimal
    other_currencies: list[BankFlowCurrencyTotal]


class BankTransferStatus(str, Enum):
    """How two operations came to be paired, and whether they count."""
    # Seen once, between two accounts nothing vouches for: offered to the user,
    # and both operations keep counting until they settle it.
    SUGGESTED = "suggested"
    # Touches a regulated savings account, which only its holder's account feeds.
    SAVINGS = "savings"
    # The same two accounts and labels have paired often across the history.
    RECURRING = "recurring"
    # Both labels read like operations the user already confirmed.
    LEARNED = "learned"
    # Bound by the user.
    CONFIRMED = "confirmed"
    # An operation and its cancellation on one account, bound by the user.
    REVERSAL = "reversal"
    # A payment and its refund on one account, found on a shared merchant word.
    REFUND = "refund"


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


class CategoryNature(str, Enum):
    """How the operations of a category count in the real cashflow."""
    EXPENSE = "EXPENSE"
    INCOME = "INCOME"
    SAVING = "SAVING"
    INVESTMENT = "INVESTMENT"


class CategoryOrigin(str, Enum):
    """Where a category was created, which decides where it is offered."""
    CASHFLOW = "cashflow"
    BANK = "bank"
    AI = "ai"


class CategoryScope(str, Enum):
    """The screen asking which categories to offer."""
    BANK = "bank"
    PLANNED = "planned"


class AvailableCategory(BaseModel):
    """A category a screen may offer. `id` is None for a category that only
    exists as the text of a declared cashflow, until it is picked in Banque."""
    id: str | None = None
    name: str
    nature: CategoryNature
    origin: CategoryOrigin


class RuleSource(str, Enum):
    """Who wrote a category rule."""
    USER = "user"
    AI = "ai"


class CategorySource(str, Enum):
    """What filed an operation under its category."""
    MANUAL = "manual"
    USER_RULE = "user_rule"
    AI_RULE = "ai_rule"


class OperationNature(str, Enum):
    """How an operation counts in the real cashflow. Only EXPENSE and INCOME
    count as such; SAVING and INVESTMENT are totalled apart, INTERNAL and
    NEUTRALIZED only reported."""
    EXPENSE = "EXPENSE"
    INCOME = "INCOME"
    SAVING = "SAVING"
    INVESTMENT = "INVESTMENT"
    INTERNAL = "INTERNAL"
    NEUTRALIZED = "NEUTRALIZED"


class OperationType(str, Enum):
    """How an operation was made, as far as its label tells
    (services/banking/operation_types.py). Display and filtering only."""
    CARD = "CARD"
    TRANSFER = "TRANSFER"
    DIRECT_DEBIT = "DIRECT_DEBIT"
    WITHDRAWAL = "WITHDRAWAL"
    INTEREST = "INTEREST"
    UNKNOWN = "UNKNOWN"


class BankTransactionItem(BaseModel):
    """One stored movement, as the bank reported it."""
    id: str
    account_id: str
    account_name: str
    operation_date: date | None
    # Unsigned: the direction is `is_credit`, as in the bank's own contract.
    amount: Decimal
    currency: str
    is_credit: bool
    is_pending: bool
    label: str | None
    # Set when the movement pairs as a transfer between two of the user's own
    # accounts: the account on the other side.
    transfer_account_id: str | None = None
    transfer_account_name: str | None = None
    # The movement on the other side, and how the pair was made.
    transfer_id: str | None = None
    transfer_status: BankTransferStatus | None = None
    operation_type: OperationType = OperationType.UNKNOWN
    nature: OperationNature | None = None
    # The category the operation is filed under, and what filed it. A manual
    # source with no category is the user saying "none".
    category_id: str | None = None
    category_name: str | None = None
    category_source: CategorySource | None = None
    rule_id: str | None = None


class BankTransactionsResponse(BaseModel):
    """GET /banking/transactions — one month of operations, with that month's
    totals computed as GET /banking/flows computes them."""
    period: str  # YYYY-MM
    currency: str
    inflow: Decimal
    outflow: Decimal
    net: Decimal
    internal_transfers_excluded: int
    internal_transfers_amount: Decimal
    # Pairs offered to the user this month, not deducted.
    transfer_questions: int = 0
    reversals_excluded: int = 0
    reversals_amount: Decimal = Decimal("0")
    pending_count: int
    pending_inflow: Decimal
    pending_outflow: Decimal
    other_currencies: list[BankFlowCurrencyTotal]
    transactions: list[BankTransactionItem]


class ExportImportStatus(str, Enum):
    """The branch one account of an Enable Banking export import took."""
    IMPORTED = "imported"
    # No link points at this account: nothing to import it into.
    UNLINKED = "unlinked"
    ERROR = "error"
    # Operations stored, curve not written: no usable balance in the export.
    BALANCE_UNAVAILABLE = "balance_unavailable"
    CURVE_ERROR = "curve_error"


class BankExportImportResult(BaseModel):
    bank_account_uuid: str
    status: ExportImportStatus
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    malformed: int = 0
    snapshots_written: int = 0
    detail: str | None = None


class BankExportImportResponse(BaseModel):
    imported_accounts: int
    results: list[BankExportImportResult]


class BankTransferDecisionKind(str, Enum):
    TRANSFER = "transfer"
    NOT_TRANSFER = "not_transfer"
    REVERSAL = "reversal"


class BankTransferDecisionCreate(BaseModel):
    """POST /banking/transfer-decisions — settle two movements, in either order."""
    transaction_id: str
    other_transaction_id: str
    kind: BankTransferDecisionKind


class BankTransferQuestionMonth(BaseModel):
    period: str  # YYYY-MM
    count: int


class BankTransferQuestionsResponse(BaseModel):
    """GET /banking/transfer-questions — pairs offered to the user across the
    whole history, month by month."""
    total: int
    months: list[BankTransferQuestionMonth]


# ---------------------------------------------------------------------------
# Category management
# ---------------------------------------------------------------------------


class BankCategoryItem(BaseModel):
    id: str
    name: str
    nature: CategoryNature
    origin: CategoryOrigin
    rule_count: int = 0


class BankCategoryCreate(BaseModel):
    """POST /banking/categories. `from_cashflow` materialises the category a
    declared cashflow carries under that name, its nature read from them."""
    name: str
    nature: CategoryNature | None = None
    from_cashflow: bool = False

    @model_validator(mode="after")
    def _nature_unless_from_cashflow(self):
        if self.nature is None and not self.from_cashflow:
            raise ValueError("nature is required")
        return self


class BankCategoryUpdate(BaseModel):
    name: str | None = None
    nature: CategoryNature | None = None


class BankCategoryRuleItem(BaseModel):
    id: str
    tokens: list[str]
    category_id: str
    category_name: str | None = None
    source: RuleSource
    created_at: datetime


class BankCategoryAssign(BaseModel):
    """PUT /banking/transactions/{id}/category.

    Without `apply_to_similar`, files this one operation (None = no category).
    With it, writes the user's rule on `tokens` — the proposed words when
    omitted — and drops this operation's own override.
    """
    category_id: str | None = None
    apply_to_similar: bool = False
    tokens: list[str] | None = None


class BankRuleWords(BaseModel):
    """GET /banking/transactions/{id}/rule-tokens — what a rule for this
    operation could require: every word of its label, rarest first, and the
    ones proposed."""
    words: list[str]
    proposed: list[str]


class BankCategoryAssignResult(BaseModel):
    transaction: BankTransactionItem
    # Operations the rule now files, across the whole history; 1 or 0 without a rule.
    filed_count: int


class BankUncategorizedGroup(BaseModel):
    """Operations reading alike that nothing files yet."""
    signature: str
    # The most recent of them, to file the group from.
    transaction_id: str
    label: str
    is_credit: bool
    count: int
    currency: str
    # In `currency`, over the operations in it.
    total: Decimal
    median: Decimal
    last_date: date | None = None
    tokens: list[str]


class BankUncategorizedResponse(BaseModel):
    total_groups: int
    total_operations: int
    groups: list[BankUncategorizedGroup]


class BankAICategorizeResult(BaseModel):
    """POST /banking/categorize/ai — one batch of the heaviest groups left to file."""
    processed: int
    rules_created: int
    categories_created: int
    # Groups at the head of the queue this run already left unfiled: the next
    # call passes it back as `skip`, or it would be handed the same groups again.
    skip: int
    remaining: int


# ---------------------------------------------------------------------------
# Real cashflow
# ---------------------------------------------------------------------------


class RealCashflowTotals(BaseModel):
    """What moved, by nature, in the response's currency.

    `income` and `expenses` are net of their own reversals (a refund filed under
    an expense category lowers the expenses). `saving` and `investment` are net
    too: money taken back from a savings account lowers `saving`. `internal`
    and `neutralized` are only informative, and never part of any other figure.
    """
    income: Decimal = Decimal("0")
    expenses: Decimal = Decimal("0")
    saving: Decimal = Decimal("0")
    investment: Decimal = Decimal("0")
    internal: Decimal = Decimal("0")
    neutralized: Decimal = Decimal("0")


class RealCashflowMonth(RealCashflowTotals):
    period: str  # YYYY-MM
    operation_count: int = 0


class RealCashflowCategoryShare(BaseModel):
    # None for the operations nothing files.
    category_id: str | None = None
    name: str
    amount: Decimal
    count: int


class RealCashflowBreakdown(BaseModel):
    income: list[RealCashflowCategoryShare] = []
    expenses: list[RealCashflowCategoryShare] = []
    saving: list[RealCashflowCategoryShare] = []
    investment: list[RealCashflowCategoryShare] = []


class RealCashflowExpense(BaseModel):
    id: str
    operation_date: date | None
    label: str | None
    amount: Decimal
    account_name: str
    category_name: str | None = None


class RealCashflowYear(BaseModel):
    """GET /banking/real-cashflow — one year of completed months."""
    year: int
    currency: str
    # From the year of the first stored operation to the current one.
    years_available: list[int]
    # Only completed months: the current one and later ones are left out.
    months: list[RealCashflowMonth]
    totals: RealCashflowTotals
    # Over the months carrying data, not the months elapsed.
    covered_months: int
    monthly_mean: RealCashflowTotals
    monthly_median: RealCashflowTotals
    by_category: RealCashflowBreakdown
    # Shown, never removed from the totals.
    top_expenses: list[RealCashflowExpense]
    other_currencies: list[BankFlowCurrencyTotal]


class RealCashflowMonthDetail(BaseModel):
    """GET /banking/real-cashflow/months/{period} — one completed month."""
    period: str
    currency: str
    totals: RealCashflowTotals
    operation_count: int
    by_category: RealCashflowBreakdown
    # The nearest completed months carrying data either side, if any.
    previous_period: str | None = None
    next_period: str | None = None
    other_currencies: list[BankFlowCurrencyTotal]
