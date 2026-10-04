# Demo customers

Real records from the Maple dataset (snapshot 2026-09-28), chosen from the agent queue ("next 3 days"). Each was scored with the
production model and the live gates; the outcomes below were checked end to end through the API the UI uses.

| Case | Customer (search by key) | Promise | Risk / band | Recommendation | Review | What it shows |
|---|---|---|---|---|---|---|
| Normal, low risk | `BIF-2308` | `PTP-4011048` | 0.35 / low | SMS reminder with payment link | no | Light touch; no human needed |
| High risk | `CEB-8181` | `PTP-0510789` | 0.74 / high | Call in the evening | no | Lightest allowed option for the band, with a contact window |
| Uncertain identity | `MII-5179` | `PTP-9511522` | 0.69 / high | Call in the evening | **required** | Golden C360 trust 0.67 is below 0.70, so a human confirms identity first |
| Hardship, unconfirmed | `BRS-5065` | `PTP-2511858` | 0.75 / very high | Hand to a specialist | **required** | Note text signals hardship; reviewer confirms, which writes a new softer record linked to the first |
| Hardship, recorded | `MIL-5067` | `PTP-3510937` | 0.78 / very high | Offer an eligible hardship option | no | A person already recorded hardship: restraint, not pressure |
| Policy stop | `TEB-4744` | `PTP-2011007` | 0.75 / very high | No action | no | Hard stop plus a representative on the case: high risk, but policy forbids contact |

Extra checks worth showing in the review panel:
* Agent role (`AGT-01`) tries to confirm hardship: refused, and the refused attempt is logged.
* Override of the policy-stop case to a call: refused, naming the blocking gates.
* `LAD-6806` (`PTP-3010843`, trust 0.58): escalate to a specialist.

Ask-the-data examples: "How many open collections cases are there by bucket?" (answered, SQL shown) and
"Which customers are Indigenous?" (refused: protected attribute).

Demo reviewers are a fixed directory in `app/main.py` (`AGT-01`, `REV-01`, `SPC-01`, `TL-01`); this is not real authentication.
Before recording, start from an empty decision store (stop the API, delete `warehouse/decisions.duckdb*`, start the API).
