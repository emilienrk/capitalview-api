"""Enable Banking connection schemas (BYO application_id + private key)."""

from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel

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


class CashflowType(str, Enum):
    """How an operation counts in the real cashflow, one per operation
    (services/banking/cashflow_types.py). NEUTRAL counts nowhere: money moved
    between the user's own accounts, or came back as it went."""
    INCOME = "INCOME"
    EXPENSE = "EXPENSE"
    SAVING = "SAVING"
    INVESTMENT = "INVESTMENT"
    NEUTRAL = "NEUTRAL"


class TypeSource(str, Enum):
    """What gave an operation its cashflow type, strongest first."""
    PAIR = "pair"
    OVERRIDE = "override"
    # A deposit or a withdrawal the user declared on one of their investment
    # accounts, on the very day and for the very amount, or a fee apart
    # (services/banking/contributions.py).
    CONTRIBUTION = "contribution"
    RULE = "rule"
    # A member of a recurring payment or income the user has not refused
    # (services/banking/recurring_series.py): it is reviewed there, not asked.
    RECURRING = "recurring"
    DEFAULT = "default"


class OperationType(str, Enum):
    """How an operation was made, as far as its label tells
    (services/banking/operation_types.py). Display and filtering only."""
    CARD = "CARD"
    TRANSFER = "TRANSFER"
    DIRECT_DEBIT = "DIRECT_DEBIT"
    WITHDRAWAL = "WITHDRAWAL"
    INTEREST = "INTEREST"
    UNKNOWN = "UNKNOWN"


class BankContributionMatch(BaseModel):
    """A movement declared on an investment account that this operation could be.

    `exact` means the same day and a single candidate: the operation is typed as
    an investment on it. Otherwise it is a nearby amount, shown beside the
    question for the user to judge — it types nothing.
    """
    account_name: str
    day: date
    amount: Decimal
    # A deposit into the account, as opposed to a withdrawal out of it.
    is_deposit: bool
    exact: bool


class BankFlowQuestion(BaseModel):
    """Asked on the last operation of a label nothing types but the user: a
    credit, or a transfer sent. Answered by typing the label."""
    choices: list[CashflowType]
    # The operations of the label the answer types.
    operation_count: int
    # What those operations add up to: what the answer can move.
    amount: Decimal
    # How many of them a deposit declared a few days away could be.
    hints: int = 0


# ---------------------------------------------------------------------------
# Recurring payments and income (services/banking/recurring.py)
# ---------------------------------------------------------------------------


class RecurringDirection(str, Enum):
    """What comes back: a payment in the debits, an income in the credits."""
    EXPENSE = "expense"
    INCOME = "income"


class RecurringCadence(str, Enum):
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    FOURWEEKLY = "fourweekly"
    MONTHLY = "monthly"
    BIMONTHLY = "bimonthly"
    QUARTERLY = "quarterly"
    SEMIANNUAL = "semiannual"
    ANNUAL = "annual"


class RecurringState(str, Enum):
    # Found sure enough to count without asking; not decided by the user.
    AUTO = "auto"
    CONFIRMED = "confirmed"
    # Found, asked about.
    CANDIDATE = "candidate"
    REFUSED = "refused"


class RecurringStatus(str, Enum):
    ACTIVE = "active"
    LATE = "late"
    ENDED = "ended"
    # The account is known only up to `covered_until`, before the next due date.
    STALE = "stale"


class RecurringRole(str, Enum):
    # Paid at a due date.
    REGULAR = "regular"
    # Paid off a due date: a prorata, a regularisation, a debit billed twice, a
    # salary paid early for the holidays.
    EXTRA = "extra"
    # Its refund or its rejection was paired with it: it counts for nothing.
    CANCELLED = "cancelled"
    # A credit from the recurring payment's merchant; for an income, a debit
    # the user attached as taken back.
    REFUND = "refund"
    # Attached by the user.
    MANUAL = "manual"


class RecurringNature(str, Enum):
    """What a payment is for, or where an income comes from, as the user filed
    it. Never guessed: it groups what is paid, it does not judge what could be
    stopped. Each direction takes its own natures, OTHER both
    (services/banking/natures.py)."""
    HOUSING = "housing"
    ENERGY = "energy"
    INSURANCE = "insurance"
    CREDIT = "credit"
    TELECOM = "telecom"
    TRANSPORT = "transport"
    SPORT = "sport"
    LEISURE = "leisure"
    SOFTWARE = "software"
    SALARY = "salary"
    # State or social aid: family allowance, housing benefit, unemployment.
    ALLOWANCE = "allowance"
    PENSION = "pension"
    # A rent received.
    RENTAL = "rental"
    # Money a relative sends.
    SUPPORT = "support"
    INTEREST = "interest"
    OTHER = "other"


class BankRecurringTag(BaseModel):
    """The counted recurring payment or income an operation belongs to."""
    # The user's decision; None for one counted without asking and never decided.
    id: str | None
    # Stable while the series keeps its first operation, decided or not.
    key: str
    direction: RecurringDirection
    name: str
    cadence: RecurringCadence
    role: RecurringRole
    state: RecurringState


class BankRecurringQuestion(BaseModel):
    """Asked on the last operation of a series found but not sure enough to
    count: is this a recurring payment, or a recurring income? Answered by
    POST /banking/recurring/decisions."""
    direction: RecurringDirection
    cadence: RecurringCadence
    amount: Decimal
    variable: bool
    occurrence_count: int
    since: date
    annual_estimate: Decimal
    # The names it was paid under before its current one.
    renamed_from: list[str] = []


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
    cashflow_type: CashflowType = CashflowType.EXPENSE
    type_source: TypeSource = TypeSource.DEFAULT
    # The rule typing it, exact or reached from a nearby label.
    type_rule_id: str | None = None
    flow_question: BankFlowQuestion | None = None
    # What the user's investment accounts say about it: the evidence that typed
    # it, or a nearby deposit to judge the question by.
    contribution: BankContributionMatch | None = None
    recurring: BankRecurringTag | None = None
    recurring_question: BankRecurringQuestion | None = None


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
    # Pairs offered to the user this month, not deducted, and flow questions
    # carried by an operation of this month.
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
    """GET /banking/transfer-questions — pairs offered to the user and flow
    questions across the whole history, month by month."""
    total: int
    months: list[BankTransferQuestionMonth]


class BankReviewKind(str, Enum):
    FLOW = "flow"
    TRANSFER = "transfer"
    RECURRING = "recurring"


class BankReviewItem(BaseModel):
    """One question waiting for the user, on the operation that carries it."""
    kind: BankReviewKind
    transaction: BankTransactionItem
    # What the answer can move: the operations of the label for a flow
    # question, the pair's amount for a suggested transfer. For a
    # recurring payment, what it costs a year: the answer moves no total, only
    # what the expenses say is fixed.
    amount: Decimal
    operation_count: int


class BankReviewYear(BaseModel):
    year: int
    # Recurring payment questions are counted, not added: their answer moves no total.
    amount: Decimal
    count: int


class BankReviewQueue(BaseModel):
    """GET /banking/review-queue — every open question, heaviest first."""
    # What the flow and transfer questions can still move.
    total_amount: Decimal
    total_count: int
    recurring_count: int = 0
    # Over the whole history, whatever the year asked for.
    years: list[BankReviewYear]
    questions: list[BankReviewItem]


class BankLedgerAccount(BaseModel):
    id: str
    name: str
    type: str
    institution: str | None
    currency: str
    balance: Decimal
    first_day: date | None
    # Its last sync when linked, its last operation otherwise.
    covered_until: date | None
    linked: bool


class BankLedgerGroup(BaseModel):
    """The operations of one counterpart in one direction (label_groups.py)."""
    key: str
    name: str
    is_credit: bool


class BankLedgerRow(BaseModel):
    id: str
    # Indexes into `accounts` and `groups`: repeated names would outweigh the rows.
    account: int
    group: int
    day: date | None
    # Unsigned, direction in `is_credit`, as the bank reports it.
    amount: Decimal
    currency: str
    is_credit: bool
    is_pending: bool
    label: str | None
    operation_type: OperationType
    cashflow_type: CashflowType
    type_source: TypeSource
    type_rule_id: str | None = None
    transfer_status: BankTransferStatus | None
    # Whether the real cashflow counts it, and by how much in its type's own
    # direction: summing `signed` over the counted rows of a completed month
    # gives that month's real cashflow, type by type.
    counted: bool
    signed: Decimal
    # The question it carries, if any, and whether an answer still to come can
    # change how it counts.
    question: BankReviewKind | None
    open: bool
    # Index into `recurring`: set on counted rows whose spending is a counted
    # recurring payment's, or whose income a counted recurring income's, so
    # their `signed` add up to its figure.
    recurring: int | None = None


class BankLedgerRecurring(BaseModel):
    id: str | None
    key: str
    direction: RecurringDirection
    name: str
    cadence: RecurringCadence


class BankLedger(BaseModel):
    """GET /banking/ledger — every stored operation, typed, for the reader to
    filter and group on its own side."""
    currency: str
    accounts: list[BankLedgerAccount]
    groups: list[BankLedgerGroup]
    rows: list[BankLedgerRow]
    recurring: list[BankLedgerRecurring] = []


class TypeScope(str, Enum):
    """What a type correction reaches: every operation reading like this one
    on its account and direction, past and future, or this one alone."""
    LABEL = "label"
    OPERATION = "operation"


class BankTransactionTypeUpdate(BaseModel):
    """PUT /banking/transactions/{id}/type."""
    type: CashflowType
    scope: TypeScope = TypeScope.LABEL


class BankTransactionTypeResult(BaseModel):
    transaction: BankTransactionItem
    # Operations the label's rule now types across the whole history, pairs
    # left out; 1 for a correction of this operation alone.
    covered_count: int


class BankTypeRuleItem(BaseModel):
    """GET /banking/type-rules."""
    id: str
    account_id: str
    account_name: str
    is_credit: bool
    signature: str
    # The most recent operation it types, to read the rule by; None when it
    # types nothing any more.
    label: str | None = None
    type: CashflowType
    operation_count: int
    created_at: datetime


class RecurringDecisionKind(str, Enum):
    CONFIRM = "confirm"
    REFUSE = "refuse"


class BankRecurringDecisionCreate(BaseModel):
    """POST /banking/recurring/decisions — answer for the series an
    operation belongs to."""
    transaction_id: str
    decision: RecurringDecisionKind
    name: str | None = None


class BankRecurringCreate(BaseModel):
    """POST /banking/recurring — mark an operation the detection missed."""
    transaction_id: str
    cadence: RecurringCadence | None = None
    name: str | None = None


class BankRecurringUpdate(BaseModel):
    """PATCH /banking/recurring/{id}. A field left out is unchanged; null
    clears it."""
    name: str | None = None
    cadence: RecurringCadence | None = None
    # What it is for; null files it back under « À classer ».
    nature: RecurringNature | None = None
    # The day the user ended it.
    ended_on: date | None = None


class RecurringOperationAction(str, Enum):
    INCLUDE = "include"
    EXCLUDE = "exclude"


class BankRecurringOperation(BaseModel):
    transaction_id: str
    action: RecurringOperationAction


class BankRecurringMerge(BaseModel):
    """The other recurring payment, by its decision or by any of its operations
    when it was never decided."""
    other_id: str | None = None
    other_transaction_id: str | None = None


class BankRecurringPriceChange(BaseModel):
    date: date
    before: Decimal
    after: Decimal
    percent: Decimal


class BankRecurringEpisode(BaseModel):
    start: date
    end: date


class BankRecurringRename(BaseModel):
    date: date
    before: str
    after: str


class BankRecurringRefund(BaseModel):
    id: str
    date: date
    amount: Decimal
    label: str | None


class BankRecurringRefunds(BaseModel):
    total: Decimal
    items: list[BankRecurringRefund]


class BankRecurringItem(BaseModel):
    # The user's decision; None for a series never decided.
    id: str | None
    key: str
    direction: RecurringDirection
    # Its last operation, to act on it: answer, decide, list its operations.
    transaction_id: str
    name: str
    # What it is for, or where it comes from; None until the user files it.
    nature: RecurringNature | None = None
    state: RecurringState
    confidence: str | None
    status: RecurringStatus
    # When `status` is stale: the day its account is known complete up to.
    covered_until: date | None = None
    cadence: RecurringCadence
    variable: bool
    amount: Decimal
    currency: str
    monthly_equivalent: Decimal
    annual_estimate: Decimal
    # What was actually paid, or received, over the last twelve months, extras
    # included, cancelled operations left out.
    paid_last_12_months: Decimal
    first_date: date
    # Set when the first operation is within a due date of the account's first
    # operation: it may have started before the history does.
    since_at_least: bool = False
    last_date: date
    next_date: date
    occurrence_count: int
    extra_count: int
    accounts: list[str]
    payment_method: OperationType
    price_changes: list[BankRecurringPriceChange] = []
    episodes: list[BankRecurringEpisode] = []
    renamed: list[BankRecurringRename] = []
    refunds: BankRecurringRefunds
    ended_on: date | None = None


class BankRecurringResponse(BaseModel):
    """GET /banking/recurring — one direction at a time."""
    direction: RecurringDirection
    currency: str
    # The active ones counted, in `currency`: what is fixed.
    monthly_total: Decimal
    annual_total: Decimal
    items: list[BankRecurringItem]


# ---------------------------------------------------------------------------
# Real cashflow
# ---------------------------------------------------------------------------


class RealCashflowTotals(BaseModel):
    """What moved, by cashflow type, in the response's currency.

    `income` and `expenses` are net of their own reversals: a refund lowers the
    expenses. `saving` and `investment` are net too: money taken back from a
    savings account lowers `saving`. `neutral` is only informative, and never
    part of any other figure. `net` is what is left once spent, set aside and
    invested.
    """
    income: Decimal = Decimal("0")
    expenses: Decimal = Decimal("0")
    saving: Decimal = Decimal("0")
    investment: Decimal = Decimal("0")
    neutral: Decimal = Decimal("0")
    net: Decimal = Decimal("0")
    # The part of `expenses` spent on counted recurring payments, their refunds
    # taken off: already in `expenses`, never added to anything else.
    recurring: Decimal = Decimal("0")
    # The rest of `expenses`. Taken month by month, so a median month's is a
    # month's, never the difference of two medians.
    one_off: Decimal = Decimal("0")
    # The same split of `income`: what counted recurring income brought, and
    # the rest.
    recurring_income: Decimal = Decimal("0")
    one_off_income: Decimal = Decimal("0")
    # Percent of the income: what was not spent, and the part of it set aside
    # or invested. None without income to divide by.
    savings_rate: Decimal | None = None
    placed_rate: Decimal | None = None


class RealCashflowMonth(RealCashflowTotals):
    period: str  # YYYY-MM
    operation_count: int = 0
    # Suggested pairs and operations waiting on a flow question: what can
    # still move this month's figures.
    open_questions: int = 0
    # What those operations weigh.
    open_amount: Decimal = Decimal("0")
    # Spent far more than the year's other months.
    atypical: bool = False


class RealCashflowRecurring(BaseModel):
    """What one recurring payment, or income, weighed in a month."""
    id: str | None
    key: str
    name: str
    nature: RecurringNature | None = None
    amount: Decimal
    count: int


class RealCashflowUpcoming(BaseModel):
    """A due date of an active recurring payment, or income, still to come
    this month."""
    id: str | None
    key: str
    name: str
    date: date
    amount: Decimal


class RealCashflowExpense(BaseModel):
    id: str
    operation_date: date | None
    label: str | None
    amount: Decimal
    account_name: str


class RealCashflowCounterpart(BaseModel):
    """Where money went, or came from, read off the labels of one group."""
    group_key: str
    name: str
    amount: Decimal
    operation_count: int
    # Percent of what the listed direction weighs over the period.
    share: Decimal


class RealCashflowCoverageGap(BaseModel):
    """An account whose stored operations leave part of the period out: a
    transfer to it there cannot pair, and counts as spent."""
    account_id: str
    account_name: str
    first_day: date
    # The last day its operations are known complete: the last sync of a
    # linked account, the last operation of an imported one.
    covered_until: date
    starts_late: bool
    ends_early: bool


class RealCashflowSafetyNet(BaseModel):
    """How many months of spending the money at hand would cover."""
    # Current and savings accounts, at their stored balance.
    available: Decimal
    savings: Decimal
    # Median over the last twelve completed months carrying operations.
    monthly_expenses: Decimal
    months: Decimal | None
    savings_months: Decimal | None
    # Balances not refreshed by a sync in the last week.
    stale_accounts: list[str]


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
    open_questions: int = 0
    open_amount: Decimal = Decimal("0")
    monthly_mean: RealCashflowTotals
    monthly_median: RealCashflowTotals
    # Shown, never removed from the totals.
    top_expenses: list[RealCashflowExpense]
    top_sources: list[RealCashflowCounterpart] = []
    top_destinations: list[RealCashflowCounterpart] = []
    other_currencies: list[BankFlowCurrencyTotal]
    # The previous year over the same months; None when it has none covered.
    previous_year_to_date: RealCashflowTotals | None = None
    # The current year only: its totals plus the median month for each month left.
    projection: RealCashflowTotals | None = None
    safety_net: RealCashflowSafetyNet | None = None
    coverage_gaps: list[RealCashflowCoverageGap] = []
    # The current year only: what the active recurring payments cost a month,
    # and what the active recurring income brings.
    running_recurring: Decimal | None = None
    running_recurring_income: Decimal | None = None


class RealCashflowMonthDetail(BaseModel):
    """GET /banking/real-cashflow/months/{period} — one completed month."""
    period: str
    currency: str
    totals: RealCashflowTotals
    operation_count: int
    open_questions: int = 0
    open_amount: Decimal = Decimal("0")
    # The nearest completed months carrying data either side, if any.
    previous_period: str | None = None
    next_period: str | None = None
    other_currencies: list[BankFlowCurrencyTotal]
    top_expenses: list[RealCashflowExpense] = []
    top_sources: list[RealCashflowCounterpart] = []
    top_destinations: list[RealCashflowCounterpart] = []
    coverage_gaps: list[RealCashflowCoverageGap] = []
    recurring: list[RealCashflowRecurring] = []
    recurring_income: list[RealCashflowRecurring] = []


class RealCashflowPacePoint(BaseModel):
    day: int
    # None past today.
    spent: Decimal | None
    median: Decimal | None


class RealCashflowCurrent(BaseModel):
    """GET /banking/real-cashflow/current — the month in progress against the
    months before it, day by day."""
    period: str
    currency: str
    day: int
    # Pending card payments included: they are spent already.
    spent_to_date: Decimal
    pending_to_date: Decimal
    # Over the last twelve completed months carrying operations; None without any.
    median_to_date: Decimal | None
    median_month: Decimal | None
    projection: Decimal | None
    open_amount: Decimal
    curve: list[RealCashflowPacePoint]
    upcoming: list[RealCashflowUpcoming] = []
    upcoming_amount: Decimal = Decimal("0")
    # The income still expected this month, apart: it moves no spending.
    upcoming_income: list[RealCashflowUpcoming] = []
    upcoming_income_amount: Decimal = Decimal("0")
