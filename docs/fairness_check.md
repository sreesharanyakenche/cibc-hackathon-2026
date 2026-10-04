# Fairness / proxy disparity check (held-out window, model ptp_break v1)

Overall: n=35,323, calibration gap +0.031, share in high/very_high bands 56.9%. Flag rule: |group gap - overall gap| > 0.05 or |band share - overall| > 0.05, n >= 500 (thresholds are assumptions). Protected attributes are used for this audit only, never as model inputs.

## proxy fields

| field | group | n | mean risk | observed | gap | high-band share | flagged |
|---|---|---:|---:|---:|---:|---:|---|
| province_code | AB | 4,594 | 0.621 | 0.599 | +0.022 | 55.9% | no |
| province_code | ON | 13,264 | 0.622 | 0.592 | +0.030 | 56.5% | no |
| province_code | SK | 952 | 0.618 | 0.598 | +0.021 | 55.8% | no |
| province_code | MB | 1,089 | 0.627 | 0.614 | +0.013 | 57.6% | no |
| province_code | QC | 7,750 | 0.629 | 0.592 | +0.037 | 58.6% | no |
| province_code | BC | 5,548 | 0.620 | 0.587 | +0.033 | 56.4% | no |
| province_code | NB | 571 | 0.629 | 0.585 | +0.044 | 58.3% | no |
| province_code | NS | 771 | 0.621 | 0.568 | +0.053 | 58.0% | no |
| preferred_language | EN | 26,921 | 0.621 | 0.595 | +0.026 | 56.3% | no |
| preferred_language | FR | 6,857 | 0.632 | 0.588 | +0.044 | 59.3% | no |
| preferred_language | ZH | 584 | 0.628 | 0.563 | +0.065 | 57.5% | no |
| urban_rural | urban | 26,089 | 0.621 | 0.590 | +0.032 | 56.4% | no |
| urban_rural | rural | 9,234 | 0.629 | 0.601 | +0.028 | 58.6% | no |

## protected fields

| field | group | n | mean risk | observed | gap | high-band share | flagged |
|---|---|---:|---:|---:|---:|---:|---|
| gender_code | M | 17,438 | 0.624 | 0.596 | +0.028 | 57.4% | no |
| gender_code | F | 16,892 | 0.623 | 0.588 | +0.035 | 56.7% | no |
| age_band | 25-34 | 9,005 | 0.631 | 0.603 | +0.028 | 59.5% | no |
| age_band | 55-64 | 2,831 | 0.604 | 0.565 | +0.039 | 52.9% | no |
| age_band | 35-44 | 7,972 | 0.627 | 0.609 | +0.018 | 57.3% | no |
| age_band | 65+ | 1,989 | 0.622 | 0.563 | +0.059 | 56.6% | no |
| age_band | 18-24 | 6,876 | 0.617 | 0.576 | +0.040 | 55.3% | no |
| age_band | 45-54 | 6,480 | 0.625 | 0.597 | +0.028 | 56.7% | no |
| marital_status | separated | 3,174 | 0.655 | 0.624 | +0.032 | 65.4% | **YES** |
| marital_status | undisclosed | 1,527 | 0.625 | 0.592 | +0.033 | 57.7% | no |
| marital_status | married | 10,922 | 0.623 | 0.590 | +0.032 | 56.9% | no |
| marital_status | single | 9,364 | 0.618 | 0.582 | +0.035 | 55.7% | no |
| marital_status | common_law | 4,389 | 0.614 | 0.587 | +0.027 | 54.5% | no |
| marital_status | widowed | 999 | 0.623 | 0.574 | +0.049 | 55.5% | no |
| marital_status | divorced | 2,070 | 0.622 | 0.611 | +0.011 | 56.6% | no |
| marital_status | unknown | 2,878 | 0.623 | 0.605 | +0.018 | 55.7% | no |
| citizenship_status | citizen | 27,051 | 0.626 | 0.596 | +0.030 | 57.6% | no |
| citizenship_status | study_permit | 1,169 | 0.609 | 0.571 | +0.039 | 55.5% | no |
| citizenship_status | work_permit | 1,539 | 0.611 | 0.589 | +0.022 | 56.2% | no |
| citizenship_status | permanent_resident | 5,196 | 0.616 | 0.582 | +0.034 | 54.1% | no |
| newcomer_program_flag | false | 31,792 | 0.626 | 0.596 | +0.029 | 57.5% | no |
| newcomer_program_flag | true | 3,531 | 0.602 | 0.562 | +0.040 | 52.3% | no |
| vulnerability_flag | true | 5,564 | 0.675 | 0.669 | +0.005 | 70.3% | **YES** |
| vulnerability_flag | false | 29,759 | 0.614 | 0.578 | +0.035 | 54.4% | no |

## Result

2 of 37 groups flagged. Flagged groups: marital_status=separated; vulnerability_flag=true.
Fields marked as proxies stay excluded from the model regardless of the result.