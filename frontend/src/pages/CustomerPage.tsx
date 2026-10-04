import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, type Json } from "../api";
import { Badge, Card, ErrorBox, Kv, Spinner, Table } from "../components/ui";
import { label, money, pct } from "../format";
import { bandTone } from "./QueuePage";

const tierTone = (t: string) => (t === "A" ? "green" : t === "B" || t === "C" ? "blue" : "amber");

export default function CustomerPage() {
  const { id = "" } = useParams();
  const nav = useNavigate();
  const [d, setD] = useState<Json | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState<string | null>(null);
  useEffect(() => {
    setD(null);
    setErr(null);
    api.customer(id).then(setD).catch(setErr);
  }, [id]);
  if (err) return <ErrorBox error={err} />;
  if (!d) return <Spinner />;
  const c = d.c360 as Json;
  const recommend = async (ptp: string) => {
    setBusy(ptp);
    try {
      const r = await api.recommend(ptp);
      nav(`/decision/${r.record.decision_id}`);
    } catch (e) {
      setErr(e);
      setBusy(null);
    }
  };
  const conflicts = (d.attributes as Json[]).filter((a) => a.conflict_status === "conflict");
  const stop = [c.internal_do_not_call_flag && "internal do-not-call", c.cease_communication_flag && "cease contact", c.bankruptcy_flag && "bankruptcy", c.deceased_flag && "deceased"].filter(Boolean).join(", ");
  return (
    <>
      <div className="crumbs"><Link to="/">Agent queue</Link> › Customer 360</div>
      <div className="cust-head">
        <h1>{c.first_name} {c.last_name} <small>{c.customer_key} · {c.province_code ?? "–"} · {c.segment ?? "–"} · {c.customer_status}</small></h1>
        <div>
          <Badge tone={c.identity_review_required ? "amber" : "green"}>identity trust {c.trust_score}</Badge>{" "}
          {c.hardship_flag && <Badge tone="purple">hardship flag on case</Badge>}
        </div>
      </div>
      {c.identity_review_required && <div className="note-warn">Identity trust is below the review threshold: a human must confirm who this is before any contact.</div>}
      <div className="grid-2">
        <Card title="Identity resolution" right={<Badge tone="blue">{c.identity_basis}</Badge>}>
          <Kv k="Match confidence (weakest link)" v={c.match_confidence} />
          <Kv k="Trust score" v={c.trust_score} />
          <Kv k="CRM records merged" v={`${c.n_crm_records} (${(c.member_crm_ids ?? []).join(", ")})`} />
          <Kv k="Accounts linked / pending review" v={`${c.n_linked_accounts} / ${c.n_accounts_pending_review}`} />
          <Kv k="Shared mobile number" v={c.shared_mobile_blocked ? "yes: phone not used as evidence" : "no"} />
          <h4>Conflict flags</h4>
          <div className="chips">
            {(c.conflict_flags ?? []).length ? (c.conflict_flags as string[]).map((f) => <Badge key={f} tone="amber">{label(f)}</Badge>) : <span className="muted">none</span>}
          </div>
          {conflicts.length > 0 && (
            <>
              <h4>Values that disagree across systems (kept side by side)</h4>
              <Table rows={conflicts} cols={[
                { h: "Field", r: (a) => label(a.field) },
                { h: "Value", r: (a) => a.value },
                { h: "Source", r: (a) => a.source },
              ]} />
            </>
          )}
        </Card>
        <Card title="Collections position">
          <Kv k="Active cases" v={c.n_active_cases} />
          <Kv k="Worst bucket / max DPD" v={`${c.worst_bucket ?? "–"} / ${c.max_current_dpd ?? "–"}`} />
          <Kv k="Total overdue" v={money(Number(c.total_overdue))} />
          <Kv k="Promises (kept / broken / open)" v={`${c.n_ptp_kept} / ${c.n_ptp_broken} / ${c.n_ptp_open}`} />
          <Kv k="Outbound contacts last 7 days (cap)" v={`${c.n_outbound_7d} (${c.contact_cap_7d ?? 3})`} />
          <Kv k="Last contact" v={`${c.last_contact_channel ?? "–"} · ${String(c.last_contact_ts_utc ?? "").slice(0, 10) || "–"}`} />
          <Kv k="Consent call / SMS / email" v={[c.consent_call, c.consent_sms, c.consent_email].map((x) => (x ? "yes" : "no")).join(" / ")} />
          <Kv k="Stop flags (cease, bankruptcy, deceased, internal DNC)" v={stop || "none"} />
          <Kv k="National DNCL (telemarketing only; does not block collection calls)" v={c.national_dncl_flag ? "listed" : "not listed"} />
        </Card>
      </div>
      <Card title="Promises to pay">
        <Table rows={d.promises} empty="No promises on record." cols={[
          { h: "Promise", r: (p) => p.ptp_id },
          { h: "Status", r: (p) => <Badge tone={p.ptp_status === "open" ? "blue" : p.ptp_status === "broken" ? "red" : "green"}>{label(p.ptp_status)}</Badge> },
          { h: "Due", r: (p) => String(p.ptp_due_date).slice(0, 10) },
          { h: "Channel", r: (p) => p.ptp_channel ?? "–" },
          { h: "", r: (p) => (p.ptp_status === "open" ? <button className="primary" disabled={busy === p.ptp_id} onClick={() => recommend(p.ptp_id)}>{busy === p.ptp_id ? "Working…" : "Recommend"}</button> : null) },
        ]} />
      </Card>
      <Card title="Linked accounts and evidence" right={<small className="muted">every link carries per-field evidence</small>}>
        <Table rows={d.linked_accounts} empty="No source accounts linked." cols={[
          { h: "System", r: (a) => a.source_system },
          { h: "Account", r: (a) => a.account_id },
          { h: "Tier", r: (a) => <Badge tone={tierTone(a.tier)}>{a.tier}</Badge> },
          { h: "Confidence", cls: "num", r: (a) => pct(a.match_confidence, 0) },
          { h: "Status", r: (a) => label(a.link_status) },
          { h: "Evidence", r: (a) => <small>{a.match_evidence}</small> },
        ]} />
      </Card>
      <Card title="Earlier decisions">
        <Table rows={d.decisions} empty="No decision records for this customer yet." cols={[
          { h: "Record", r: (x) => <Link to={`/decision/${x.decision_id}`}>{x.decision_id}</Link> },
          { h: "Promise", r: (x) => x.ptp_id },
          { h: "Band", r: (x) => <Badge tone={bandTone(x.band)}>{label(x.band)}</Badge> },
          { h: "Recommended", r: (x) => label(x.recommended_action) },
          { h: "Review", r: (x) => (x.review_required ? <Badge tone="amber">required</Badge> : "no") },
        ]} />
      </Card>
    </>
  );
}
