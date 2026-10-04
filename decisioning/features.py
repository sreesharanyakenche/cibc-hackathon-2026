"""Layer 3: feature registry. DEFINE ONCE, USE TWICE.

Every feature is one SQL expression in `FEATURES`. `compute_features(con, where)` assembles them into a single
query. Training calls it for all resolved promises, live scoring calls it with `ptp_id = '...'`; there is no
second implementation to drift. `tests/test_features.py` checks batch == single-row for a sample (parity).

Point-in-time rule (prediction time = the moment the promise is created):
  * contact / payment history uses DAY-level cut-offs strictly before the promise's creation date. Promise and
    contact timestamps differ by up to about 13 hours in this data, so same-day events are never used.
  * earlier promises (the customer's, and the case's prior-broken count) count only if they were RESOLVED
    (status_ts) before the creation date. The stored promises_to_pay.prior_broken_ptp_count is NOT used: it holds
    final outcomes, including ones not yet known when the promise was made (4.2% of repeat promises).
  * account snapshots, bureau pulls, salary credits and transactions are taken as-of the last period that ended
    before the creation date; snapshot / pull age is available as a diagnostic.
  * EXCEPTION, stated plainly: text features read the agent note written at the promise's own contact. That note is
    saved a median of ~8 minutes (up to 13 hours) AFTER the promise timestamp, so it is learned just after creation, not
    before it. About 0.8% of such notes were edited after the outcome was recorded; the text may then reflect the outcome.
  * NEVER used: ptp_status, status_ts, amount_paid_against, paid_date, broken_reason_agent_coded,
    system_event_type in (ptp_kept, ptp_broken), any case-level snapshot counters (promises_kept, outcome, cure_*...).

Purpose tags:  model = used by the PTP-break model;  gate_only = used only by deterministic gates (never a model input);
  excluded = computed for analysis only. The amount features are excluded because open promises (outcome not yet decided)
  almost never have ptp_to_overdue_ratio = 1.0 (0.3%) while promises that turned out kept do (31% of recent ones): the amount fields
  look generated AFTER the outcome, so the relationship does not transfer to promises being scored (see docs/decisioning.md);
  txn_benefit_3m is also excluded: benefit deposits can reveal age, disability or family status (protected grounds).
  diagnostic = drifts by construction (snapshot age grows over time) and is shown but not modelled.
Protected attributes (gender, marital status, citizenship, household, newcomer, accessibility, vulnerability,
accent) are not in the registry at all; `audit_registry()` enforces that. Proxy-risk fields (province, postal
code / FSA, branch, language) are also absent.
"""
from __future__ import annotations

from dataclasses import dataclass

import duckdb

from . import config

FORBIDDEN_SOURCE_COLUMNS = {
    # outcome / post-creation fields
    "ptp_status", "status_ts", "amount_paid_against", "paid_date", "broken_reason_agent_coded",
    "promises_kept", "promises_broken", "promises_open", "cure_flag", "cure_date", "days_to_cure", "outcome",
    "outcome_date", "payments_since_open_amount", "payments_since_open_count", "roll_forward_count",
    "b.prior_broken_ptp_count", "p.prior_broken_ptp_count",      # stored count includes outcomes unknown at creation
    # protected attributes and proxy-risk fields
    "gender_code", "marital_status", "citizenship_status", "household_size", "newcomer_program_flag",
    "accessibility_needs_flag", "accessibility_type", "vulnerability_flag", "vulnerable_customer_flag",
    "customer_accent_region", "province_code", "postal_code", "fsa", "home_branch_transit", "preferred_language",
    "language_of_service", "message_language", "note_language", "language_used",
}


@dataclass(frozen=True)
class Feature:
    name: str
    expr: str                   # SQL over the CTE aliases b, h, c, a, s, n
    description: str
    source: str
    as_of: str
    purpose: str = "model"      # model | gate_only | excluded | diagnostic
    kind: str = "numeric"       # numeric | categorical
    tag: str = "none"           # none | proxy_risk | protected (must stay none for model features)


CHANNEL = "case b.ptp_channel when 'call' then 0 when 'ivr' then 1 when 'web' then 2 when 'app' then 3 when 'sms_reply' then 4 end"
METHOD = ("case b.payment_method_promised when 'online_banking' then 0 when 'debit_card' then 1 when 'e_transfer' then 2 "
          "when 'pad' then 3 when 'branch' then 4 end")
PRODUCT = ("case b.product when 'card' then 0 when 'personal_loan' then 1 when 'auto_loan' then 2 when 'loc' then 3 "
           "when 'overdraft' then 4 when 'mortgage' then 5 when 'multi' then 6 end")

T = "lower(coalesce(n.note_text, ''))"

FEATURES: list[Feature] = [
    # ---- the promise itself (known when it is made) -------------------------------------------------
    Feature("days_to_due", "b.days_to_due_at_creation", "Days between promise and due date", "promises_to_pay", "creation"),
    Feature("ptp_amount", "b.ptp_amount", "Promised amount (CAD)", "promises_to_pay", "creation", purpose="excluded"),
    Feature("overdue_at_creation", "b.overdue_at_creation", "Overdue amount when promised", "promises_to_pay", "creation", purpose="excluded"),
    Feature("ptp_to_overdue_ratio", "b.ptp_to_overdue_ratio", "Promised amount / overdue amount", "promises_to_pay", "creation", purpose="excluded"),
    Feature("dpd_at_creation", "b.dpd_at_creation", "Days past due when promised", "promises_to_pay", "creation"),
    Feature("prior_broken_ptp_count", "coalesce(ch.n_broken, 0)",
            "Broken promises earlier in this case that were already resolved before this promise was created "
            "(recomputed from history: the stored column counts final outcomes, including ones not yet known)",
            "promises_to_pay", "status_ts < creation date"),
    Feature("ptp_sequence_in_case", "b.ptp_sequence_in_case", "Nth promise in the case", "promises_to_pay", "creation"),
    Feature("is_instalment", "cast(b.ptp_type <> 'single' as int)", "Instalment promise (1 of n)", "promises_to_pay", "creation"),
    Feature("channel_code", CHANNEL, "Channel the promise was made on", "promises_to_pay", "creation", kind="categorical"),
    Feature("payment_method_code", METHOD, "Payment method promised (NULL = not stated)", "promises_to_pay", "creation", kind="categorical"),
    Feature("product_code", PRODUCT, "Product the promise applies to", "promises_to_pay", "creation", kind="categorical"),
    Feature("reminder_scheduled", "cast(b.reminder_scheduled_flag as int)", "Reminder scheduled before due date", "promises_to_pay", "creation"),
    Feature("has_income_date", "cast(b.customer_stated_income_date is not null as int)",
            "Customer stated a pay date (always equals the due date when present)", "promises_to_pay", "creation"),
    Feature("is_self_serve", "cast(b.agent_id is null as int)", "Made without an agent (IVR/app/web/SMS)", "promises_to_pay", "creation"),
    Feature("due_dow", "dayofweek(b.ptp_due_date)", "Day of week of the due date (0 = Sunday)", "promises_to_pay", "creation"),
    Feature("due_dom", "day(b.ptp_due_date)", "Day of month of the due date (payday proximity)", "promises_to_pay", "creation"),
    Feature("created_hour", "hour(b.ptp_created_ts)", "Hour the promise was made", "promises_to_pay", "creation"),
    # ---- the case (static at open) ------------------------------------------------------------------
    Feature("case_age_days", "date_diff('day', b.case_open_date, b.created_date)", "Days since the case opened", "collections_cases.case_open_date", "creation"),
    Feature("entry_dpd", "b.entry_dpd", "DPD when the case opened", "collections_cases.entry_dpd", "case open"),
    Feature("entry_overdue", "b.entry_overdue", "Overdue amount when the case opened", "collections_cases.entry_overdue", "case open", purpose="excluded"),
    Feature("cust_tenure_months", "date_diff('month', u.customer_since_date, b.created_date)", "Months as a customer at the promise date", "customers.customer_since_date", "creation"),
    Feature("acct_n_accounts", "a.n_acct", "Accounts with a snapshot at the last quarter-end", "account_monthly_snapshot", "last quarter-end < creation"),
    # ---- the customer's earlier promises (resolved before creation date) ---------------------------
    Feature("cust_prior_resolved", "coalesce(h.n_res, 0)", "Customer's earlier resolved promises", "promises_to_pay", "status_ts < creation date"),
    Feature("cust_prior_broken_rate", "h.n_broken * 1.0 / nullif(h.n_res, 0)", "Share of earlier promises broken", "promises_to_pay", "status_ts < creation date"),
    Feature("cust_days_since_last_broken", "date_diff('day', h.last_broken_date, b.created_date)", "Days since the customer's last broken promise", "promises_to_pay", "status_ts < creation date"),
    # ---- contact history on the case, last 30 days before the creation date -------------------------
    Feature("case_outbound_30d", "coalesce(c.outbound_30d, 0)", "Outbound contacts on the case, prior 30 days", "contact_history", "date < creation date"),
    Feature("case_calls_30d", "coalesce(c.calls_30d, 0)", "Outbound calls, prior 30 days", "contact_history", "date < creation date"),
    Feature("case_sms_30d", "coalesce(c.sms_30d, 0)", "SMS sent, prior 30 days", "contact_history", "date < creation date"),
    Feature("case_rpc_30d", "coalesce(c.rpc_30d, 0)", "Right-party contacts, prior 30 days", "contact_history", "date < creation date"),
    Feature("case_payments_30d", "coalesce(c.payments_30d, 0)", "Payments received on the case, prior 30 days", "contact_history (system payment_received)", "date < creation date"),
    Feature("case_days_since_contact", "date_diff('day', c.last_contact_date, b.created_date)", "Days since last outbound contact", "contact_history", "date < creation date"),
    # ---- the customer's accounts, last quarter-end snapshot before creation ------------------------
    Feature("acct_max_dpd", "a.max_dpd", "Worst DPD across the customer's accounts", "account_monthly_snapshot", "last quarter-end < creation"),
    Feature("acct_delinquent_n", "a.n_delinq", "Accounts 1+ days past due", "account_monthly_snapshot", "last quarter-end < creation"),
    Feature("acct_utilisation", "a.bal / nullif(a.lim, 0)", "Total balance / total limit or original amount", "account_monthly_snapshot", "last quarter-end < creation"),
    Feature("acct_paid_vs_min", "a.paid / nullif(a.min_due, 0)", "Payments made / minimum due", "account_monthly_snapshot", "last quarter-end < creation"),
    Feature("acct_snapshot_age_days", "date_diff('day', a.me, b.created_date)", "Age of the snapshot used (staleness)", "account_monthly_snapshot", "last quarter-end < creation", purpose="diagnostic"),
    # ---- bureau pulls (Apr / Oct), latest pull before the creation date ------------------------------
    Feature("bureau_score", "bu.risk_score", "Latest bureau score before the promise", "bureau_history", "last pull < creation"),
    Feature("bureau_score_delta", "bu.risk_score - bu.prev_score", "Change vs the previous bureau pull", "bureau_history", "last two pulls < creation"),
    Feature("bureau_utilisation", "bu.revolving_utilisation", "Bureau revolving utilisation", "bureau_history", "last pull < creation"),
    Feature("bureau_delinq_trades", "bu.delinquent_trades_count", "Delinquent trades on the bureau", "bureau_history", "last pull < creation"),
    Feature("bureau_inquiries_6m", "bu.inquiries_hard_6m", "Hard inquiries in the last 6 months", "bureau_history", "last pull < creation"),
    Feature("bureau_age_days", "date_diff('day', bu.pull_date, b.created_date)", "Age of the bureau pull used (staleness)", "bureau_history", "last pull < creation", purpose="diagnostic"),
    # ---- deposit transactions, 3 full months before the creation month ----------------------------------
    Feature("txn_high_cost_lender_3m", "tx.n_high_cost", "Payments to high-cost lenders, prior 3 months", "transactions", "months < creation month"),
    Feature("txn_payroll_3m", "tx.n_payroll", "Payroll deposits, prior 3 months", "transactions", "months < creation month"),
    # EXCLUDED from the model: benefit deposits (EI, CPP/OAS, child benefit, disability) can reveal age, disability or family
    # status, i.e. protected grounds (POL-COLL-004 §4.6). Still computed so the exclusion can be audited and tested.
    Feature("txn_benefit_3m", "tx.n_benefit", "Government-benefit deposits, prior 3 months (protected-ground proxy)", "transactions",
            "months < creation month", purpose="excluded", tag="proxy_risk"),
    Feature("txn_months_seen_3m", "coalesce(tx.months_seen, 0)", "Months with transaction data, prior 3 months", "transactions", "months < creation month"),
    # ---- salary credits, 3 full months before the creation month -----------------------------------
    Feature("sal_avg_3m", "s.avg_amt", "Average monthly salary credit, prior 3 months", "salary_credit_history", "months < creation month"),
    Feature("sal_gap_months_3m", "s.gap_months", "Months with a salary gap, prior 3 months", "salary_credit_history", "months < creation month"),
    Feature("sal_months_seen_3m", "coalesce(s.months_seen, 0)", "Months with salary data, prior 3 months", "salary_credit_history", "months < creation month"),
    # ---- text: the note written at the promise's own contact ---------------------------------------
    Feature("t_has_note", "cast(n.note_id is not null as int)", "A note exists for the promise's contact", "agent_notes", "same contact"),
    Feature("t_note_words", "n.word_count", "Words in the note", "agent_notes", "same contact"),
    Feature("t_payday", f"cast(regexp_matches({T}, 'payday|pay day|paday|paid on|salary|paycheque|paycheck|get paid|pay after|direct deposit|next pay') as int)",
            "Note ties the promise to a pay date", "agent_notes", "same contact"),
    Feature("t_forgot", f"cast(regexp_matches({T}, 'forgot|slipped|overlooked') as int)", "Note says the customer forgot", "agent_notes", "same contact"),
    Feature("t_dispute", f"cast(regexp_matches({T}, 'disput|wrong charge|late fee|not my charge|billing error') as int)", "Note mentions a dispute", "agent_notes", "same contact"),
    Feature("t_link_sent", f"cast(regexp_matches({T}, 'pmt link|payment link|link sent|sent link') as int)", "Note says a payment link was sent", "agent_notes", "same contact"),
    Feature("t_maybe", f"cast(regexp_matches({T}, 'try to|maybe|not sure|hopefully|should be able') as int)", "Hedged language in the note", "agent_notes", "same contact"),
    # whole-word match: the previous unanchored 'sue' matched inside "issue" (6,316 notes), 'court' inside 'courte', ...
    Feature("t_pressure", f"cast(regexp_matches({T}, '\\b(sue|sued|suing|lawsuit|garnish\\w*|court|legal action|poursui\\w*|tribunal|saisi\\w*)\\b') as int)",
            "Note records a threat of legal action (QA concern)", "agent_notes", "same contact"),
    # ---- gate-only: NEVER model inputs (hardship may only soften treatment) -------------------------
    Feature("g_hardship_text",
            f"cast(regexp_matches({T}, 'hardship|laid off|lost (my )?job|hours (were |got )?(cut|reduced)|shifts cut|reduced hours|salaire r|"
            f"difficult|sick|illness|separat|evacuat|rate reset|payment went up|cust crying|can.?t afford|cannot afford') as int)",
            "Note suggests possible financial hardship (unconfirmed)", "agent_notes", "same contact", purpose="gate_only"),
    Feature("g_hardship_flag_case", "cast(b.hardship_flag as int)", "Case hardship flag as currently recorded (snapshot)",
            "collections_cases.hardship_flag", "snapshot", purpose="gate_only"),
]

MODEL_FEATURES = [f for f in FEATURES if f.purpose == "model"]
MODEL_FEATURE_NAMES = [f.name for f in MODEL_FEATURES]
CATEGORICAL = [f.name for f in MODEL_FEATURES if f.kind == "categorical"]


def audit_registry() -> list[str]:
    """Return problems (empty = clean): forbidden columns or non-'none' tags on model features."""
    problems = []
    for f in FEATURES:
        for col in FORBIDDEN_SOURCE_COLUMNS:
            if col in f.expr.lower() and not (f.name == "g_hardship_flag_case" and col == "hardship_flag"):
                problems.append(f"{f.name}: uses forbidden column {col}")
        if f.purpose == "model" and f.tag != "none":
            problems.append(f"{f.name}: model feature tagged {f.tag}")
    return problems


# ---------------------------------------------------------------------------------------------- SQL

BASE_CTE = """
b as (
  select p.ptp_id, p.case_id, p.crm_customer_id, p.contact_id, p.agent_id, p.ptp_created_ts,
         cast(p.ptp_created_ts as date) as created_date, p.ptp_due_date,
         p.days_to_due_at_creation, p.ptp_amount, p.overdue_at_creation, p.ptp_to_overdue_ratio, p.dpd_at_creation,
         p.ptp_sequence_in_case, p.ptp_type, p.ptp_channel, p.payment_method_promised,
         p.product, p.reminder_scheduled_flag, p.customer_stated_income_date,
         k.case_open_date, k.entry_dpd, k.entry_overdue, k.hardship_flag
  from cur_promises p join cur_cases k on k.case_id = p.case_id
  where {where}
)"""

HIST_CTE = """
h as (
  select b.ptp_id, count(*) as n_res, sum(case when q.is_broken then 1 else 0 end) as n_broken,
         max(case when q.is_broken then cast(q.status_ts as date) end) as last_broken_date
  from b join cur_promises q
    on q.crm_customer_id = b.crm_customer_id and q.ptp_id <> b.ptp_id and q.is_resolved
   and cast(q.status_ts as date) < b.created_date
  group by b.ptp_id
)"""

CASE_HIST_CTE = """
ch as (
  select b.ptp_id, count(*) filter (where q.is_broken) as n_broken
  from b join cur_promises q
    on q.case_id = b.case_id and q.ptp_id <> b.ptp_id and q.is_resolved
   and cast(q.status_ts as date) < b.created_date
  group by b.ptp_id
)"""

CONTACT_CTE = """
c as (
  select b.ptp_id,
         count(*) filter (where x.direction = 'outbound' and not x.is_system_event) as outbound_30d,
         count(*) filter (where x.direction = 'outbound' and x.channel_eff = 'call') as calls_30d,
         count(*) filter (where x.direction = 'outbound' and x.channel_eff = 'sms') as sms_30d,
         count(*) filter (where x.rpc_flag) as rpc_30d,
         count(*) filter (where x.system_event_type = 'payment_received') as payments_30d,
         max(x.contact_date_local) filter (where x.direction = 'outbound' and not x.is_system_event) as last_contact_date
  from b join cur_contacts x
    on x.case_id = b.case_id and x.contact_date_local < b.created_date and x.contact_date_local >= b.created_date - 30
  group by b.ptp_id
)"""

ACCT_CTE = """
a as (
  select b.ptp_id, m.me, m.n_acct, m.max_dpd, m.n_delinq, m.bal, m.lim, m.paid, m.min_due
  from b asof left join cust_month m on m.crm_customer_id = b.crm_customer_id and b.created_date > m.me
)"""

SALARY_CTE = """
s as (
  select b.ptp_id, avg(m.amt) as avg_amt, sum(m.gap) as gap_months, count(*) as months_seen
  from b join salary_month m
    on m.crm_customer_id = b.crm_customer_id
   and m.ym < strftime(b.created_date, '%Y-%m')
   and m.ym >= strftime(b.created_date - interval 3 month, '%Y-%m')
  group by b.ptp_id
)"""

BUREAU_CTE = """
bu as (
  select b.ptp_id, m.pull_date, m.risk_score, m.prev_score, m.revolving_utilisation, m.delinquent_trades_count, m.inquiries_hard_6m
  from b asof left join bureau_crm m on m.crm_customer_id = b.crm_customer_id and b.created_date > m.pull_date
)"""

TXN_CTE = """
tx as (
  select b.ptp_id, sum(m.n_high_cost) as n_high_cost, sum(m.n_payroll) as n_payroll, sum(m.n_benefit) as n_benefit,
         count(*) as months_seen
  from b join txn_month m
    on m.crm_customer_id = b.crm_customer_id
   and m.ym < strftime(b.created_date, '%Y-%m')
   and m.ym >= strftime(b.created_date - interval 3 month, '%Y-%m')
  group by b.ptp_id
)"""

CUST_CTE = """
u as (
  select b.ptp_id, k.customer_since_date from b join raw_customers k on k.crm_customer_id = b.crm_customer_id
)"""

NOTES_CTE = """
n as (
  select b.ptp_id, x.note_id, x.note_text, x.word_count
  from b join cur_notes x on x.contact_id = b.contact_id
)"""


def feature_sql(where: str = "true", names: list[str] | None = None) -> str:
    feats = [f for f in FEATURES if names is None or f.name in names]
    select = ",\n       ".join(f"{f.expr} as {f.name}" for f in feats)
    ctes = ",\n".join([BASE_CTE.format(where=where), HIST_CTE, CASE_HIST_CTE, CONTACT_CTE, ACCT_CTE, SALARY_CTE, CUST_CTE, BUREAU_CTE, TXN_CTE, NOTES_CTE])
    return (f"with {ctes}\n"
            f"select b.ptp_id, b.case_id, b.crm_customer_id, b.created_date, b.ptp_due_date,\n       {select}\n"
            f"from b left join h on h.ptp_id = b.ptp_id left join ch on ch.ptp_id = b.ptp_id left join c on c.ptp_id = b.ptp_id "
            f"left join a on a.ptp_id = b.ptp_id left join s on s.ptp_id = b.ptp_id left join u on u.ptp_id = b.ptp_id left join bu on bu.ptp_id = b.ptp_id "
            f"left join tx on tx.ptp_id = b.ptp_id left join n on n.ptp_id = b.ptp_id")


# ---------------------------------------------------------------------------------------------- connections

def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Feature connection: own DB file (derived tables) + maple.duckdb attached read-only."""
    config.WAREHOUSE.mkdir(exist_ok=True)
    con = duckdb.connect(str(config.FEATURE_DB), read_only=read_only)
    if not con.execute("select 1 from duckdb_databases() where database_name = 'maple'").fetchone():
        con.execute(f"attach '{config.MAIN_DB.as_posix()}' as maple (read_only)")
    for v in ("cur_promises", "cur_cases", "cur_contacts", "cur_notes", "cur_snapshot", "raw_salary_credit_history",
              "raw_customers", "cur_agents", "meta_snapshot"):
        con.execute(f"create or replace temp view {v} as select * from maple.main.{v}")
    return con


def build_derived_tables(con: duckdb.DuckDBPyConnection) -> None:
    """Customer-month rollups used by the as-of joins (refreshed nightly in a real deployment)."""
    con.execute("""
        create or replace table cust_month as
        select crm_customer_id, month_end_parsed as me, count(*) as n_acct, max(dpd) as max_dpd,
               sum(case when dpd > 0 then 1 else 0 end) as n_delinq, sum(balance) as bal,
               sum(limit_or_original_amount) as lim, sum(minimum_due) as min_due, sum(payment_made) as paid
        from cur_snapshot where month_end_parsed is not null and crm_customer_id is not null
        group by 1, 2""")
    # bridge: core-banking CIF -> CRM id through the account ids that the snapshot table carries (no name matching;
    # 10-digit CIF restored with leading zeros; if a CIF maps to two CRM records the lowest id is used)
    con.execute("""
        create or replace table acct_crm as
        select account_id, min(crm_customer_id) as crm_customer_id from cur_snapshot
        where crm_customer_id is not null group by 1""")
    con.execute("""
        create or replace table cif_crm as
        select lpad(d.src_customer_ref, 10, '0') as cif, min(a.crm_customer_id) as crm_customer_id
        from maple.main.raw_deposit_accounts d join acct_crm a on a.account_id = d.deposit_account_id
        group by 1""")
    # One row per (CRM id, pull date). When two CIFs map to the same CRM id (2,187 CRM ids) both have a pull on the same
    # date; the previous version let the as-of join pick either one at random. Deterministic rule: the lowest CIF wins.
    con.execute("""
        create or replace table bureau_crm as
        select crm_customer_id, pull_date, risk_score, revolving_utilisation, delinquent_trades_count, inquiries_hard_6m,
               lag(risk_score) over (partition by crm_customer_id order by pull_date) as prev_score
        from (
          select c.crm_customer_id, h.pull_date, h.risk_score, h.revolving_utilisation, h.delinquent_trades_count,
                 h.inquiries_hard_6m,
                 row_number() over (partition by c.crm_customer_id, h.pull_date order by c.cif) as rn
          from maple.main.raw_bureau_history h join cif_crm c on c.cif = lpad(h.bank_subject_ref, 10, '0')
        ) where rn = 1""")
    con.execute("""
        create or replace table txn_month as
        select a.crm_customer_id, strftime(t.posting_date, '%Y-%m') as ym,
               count(*) filter (where t.category = 'high_cost_lender') as n_high_cost,
               count(*) filter (where t.txn_type = 'payroll') as n_payroll,
               count(*) filter (where t.txn_type = 'government_benefit') as n_benefit
        from maple.main.raw_transactions t join acct_crm a on a.account_id = t.account_id
        group by 1, 2""")
    con.execute("""
        create or replace table salary_month as
        select crm_customer_id, credit_month as ym, sum(credit_amount_total) as amt,
               max(case when gap_flag then 1 else 0 end) as gap
        from raw_salary_credit_history where crm_customer_id is not null group by 1, 2""")


DERIVED_KEYS = {"cust_month": "crm_customer_id, me", "bureau_crm": "crm_customer_id, pull_date",
                "txn_month": "crm_customer_id, ym", "salary_month": "crm_customer_id, ym",
                "acct_crm": "account_id", "cif_crm": "cif"}


def duplicate_keys(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Number of duplicated keys per derived table. Must be all zeros: a duplicated key makes an as-of join non-deterministic."""
    return {t: con.execute(f"select count(*) from (select {k} from {t} group by {k} having count(*) > 1)").fetchone()[0]
            for t, k in DERIVED_KEYS.items()}


def compute_features(con: duckdb.DuckDBPyConnection, where: str = "true", names: list[str] | None = None):
    """Run the registry SQL. Returns a Polars DataFrame."""
    return con.execute(feature_sql(where, names)).pl()
