# Identity matching: metric reconciliation

*Generated 2026-10-04 11:55 UTC by `data_pipeline/identity_validate.py`. The matcher was not run or modified.*

## What is measured

- **Population:** 1,844,500 source accounts (cards, loans, deposits). The matcher assigns each account to a `customer_key` or withholds it.
- **Ground truth:** the collections system's own account -> CRM-customer record (accounts that appear in `contact_history` or `promises_to_pay` with exactly one non-orphan CRM id). It was created without any name/DOB/phone/postal matching, so it is independent of the matcher, but it is **not an audited gold label**. 215 accounts with conflicting CRM ids were excluded.
- **Evaluation population:** the 283,851 accounts that have a ground truth (15.4% of all accounts) — accounts that were contacted or promised on, i.e. delinquent/collections-active accounts. **This is not a random sample of all accounts.**
- **Correct:** predicted `customer_key` equals the `customer_key` of the ground-truth CRM id (the anchor of its duplicate cluster).
- **Precision** = correct auto-links ÷ auto-linked accounts (both within the evaluation population). **Recall** = correct auto-links ÷ all evaluable accounts.

## Headline numbers and their denominators

| Metric | Value | Numerator / denominator |
|---|---:|---|
| Auto-link rate, all accounts | 98.26% | 1,812,472 / 1,844,500 |
| Auto-link rate, evaluable accounts | 97.54% | 276,856 / 283,851 |
| **Precision of auto-links (evaluable)** | **99.84%** | 276,424 / 276,856 |
| Recall (evaluable) | 97.38% | 276,424 / 283,851 |
| Withheld (candidate exists, not attributed) | 3,547 accounts | 799 of them evaluable |
| Unmatched (no candidate; mostly no DOB to block on) | 28,481 accounts | 6,196 of them evaluable |

## By tier

| Tier | Candidates | Auto-linked | Withheld | Evaluable & auto-linked | Correct | Precision | Share of evaluable correctly linked here (recall contribution) | Evaluable & withheld | …would have been correct | Ground-truth source |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| - | 28,481 | 0 | 0 | 0 | 0 | n/a (none auto-linked) | 0.0% | 0 | – | collections account->CRM pairs |
| A | 767,691 | 767,490 | 201 | 183,699 | 183,401 | 99.84% | 64.61% | 43 | 26 (60.47%) | collections account->CRM pairs |
| B | 727,430 | 727,197 | 233 | 27,706 | 27,669 | 99.87% | 9.75% | 12 | 7 (58.33%) | collections account->CRM pairs |
| C | 318,058 | 317,785 | 273 | 65,451 | 65,354 | 99.85% | 23.02% | 38 | 18 (47.37%) | collections account->CRM pairs |
| D | 468 | 0 | 468 | 0 | 0 | n/a (none auto-linked) | 0.0% | 99 | 90 (90.91%) | collections account->CRM pairs |
| E | 2,372 | 0 | 2,372 | 0 | 0 | n/a (none auto-linked) | 0.0% | 607 | 606 (99.84%) | collections account->CRM pairs |

Tier `-` = no candidate. A, B and C are auto-link tiers; D and E are always withheld (`review`); an A-C account is also withheld (`ambiguous`) when two different customers tie for the best tier.

### Why several numbers read 99.84%

Headline figures can coincide when rounded; they are different fractions: all auto-links (276,424/276,856), tier A, and tier E (606/607). **Tier E is not auto-linked**: its figure is the accuracy of the withheld candidate (what would have been correct if released), not a precision of linked accounts, and it rests on a small sample.

## Robustness checks

| Evaluation slice | Evaluable | Auto-linked | Correct | Precision |
|---|---:|---:|---:|---:|
| truth_crm_record_is_singleton (independent of CRM merges) | 278,273 | 271,396 | 270,993 | 99.85% |
| truth_crm_record_is_in_a_merged_cluster | 5,578 | 5,460 | 5,431 | 99.47% |
| source system CARDS | 147,309 | 143,926 | 143,712 | 99.85% |
| source system CORE | 29,634 | 29,433 | 29,394 | 99.87% |
| source system LENDING | 106,908 | 103,497 | 103,318 | 99.83% |

## Is the evaluation population representative?

| Tier | Share of all accounts | Share of evaluable accounts |
|---|---:|---:|
| - | 1.54% | 2.18% |
| A | 41.62% | 64.73% |
| B | 39.44% | 9.76% |
| C | 17.24% | 23.07% |
| D | 0.03% | 0.03% |
| E | 0.13% | 0.21% |

If these two columns differ materially, precision measured on evaluable accounts may not transfer to the remaining accounts. Accounts outside the evaluation population (never contacted) have **no ground truth**, so their precision is unmeasured.

## How to cite these numbers

Use: *"On the 283,851 accounts that have a collections-recorded customer link, 99.84% of auto-linked accounts (276,424/276,856) were linked to the correct customer; 97.38% of those accounts were correctly linked."* Do not quote the precision without the evaluation population.

