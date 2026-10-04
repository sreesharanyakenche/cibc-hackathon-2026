import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError, type Gate, type Json, type Trigger } from "../api";
import { Badge, Card, ErrorBox, Kv, RiskGauge, Spinner, Table } from "../components/ui";
import { label, pct, when } from "../format";

const gateTone = (s: string) => (s === "pass" ? "green" : s === "stop" ? "red" : s === "review" ? "amber" : "blue");
const stateTone = (s: string) => (s === "pending" ? "amber" : s === "escalate" ? "purple" : "green");

export default function DecisionPage() {
  const { id = "" } = useParams();
  const [d, setD] = useState<{ record: Json; state: Json; reviews: Json[]; attempts: Json[] } | null>(null);
  const [roles, setRoles] = useState<Record<string, string>>({});
  const [err, setErr] = useState<unknown>(null);
  const load = useCallback(() => api.decision(id).then(setD).catch(setErr), [id]);
  useEffect(() => {
    setD(null);
    setErr(null);
    load();
    api.health().then((h) => setRoles(h.demo_reviewers ?? {})).catch(() => undefined);
  }, [id, load]);
  if (err && !d) return <ErrorBox error={err} />;
  if (!d) return <Spinner />;
  const r = d.record;
  const ex = r.explanation as Json;
  const triggers = (r.review_triggers as Trigger[]).filter((t) => t.fired);
  const st = d.state;
  const needsReview = r.review_required;

  return (
    <>
      <div className="crumbs"><Link to="/">Agent queue</Link> › Decision {r.decision_id}</div>
      <h1>
        {r.action_label}{" "}
        <small>promise {r.ptp_id} · case {r.case_id} · <Link to={`/customers/${r.identity?.customer_key ?? r.crm_customer_id}`}>customer {r.identity?.customer_key ?? r.crm_customer_id}</Link></small>
      </h1>
      <div className="chips">
        <Badge tone={needsReview ? "amber" : "green"}>{needsReview ? "human review required" : "no review required"}</Badge>
        <Badge tone={stateTone(st.status)}>{st.status === "pending" ? "awaiting decision" : `${st.status} by ${st.reviewer_id}`}</Badge>
        {r.hardship_status !== "none" && <Badge tone="purple">hardship: {r.hardship_status}</Badge>}
        {r.routing_notes?.map((n: string) => <Badge key={n} tone="blue">{label(n)}</Badge>)}
      </div>

      <div className="grid-2">
        <Card title="Recommendation">
          <div className="hero">
            <div>
              <p className="narrative">{ex.summary}</p>
              {r.contact_slot?.from_local && (
                <Kv k="Suggested contact window" v={`${r.contact_slot.date} ${r.contact_slot.from_local}–${r.contact_slot.to_local} (${r.contact_slot.language ?? "–"})`} />
              )}
              <Kv k="Allowed by policy" v={(r.allowed_actions as string[]).map(label).join(", ")} />
              {st.final_action && <Kv k="Final action (after review)" v={<b>{label(st.final_action)}</b>} />}
            </div>
            <div>
              <RiskGauge value={r.risk} level={r.band} />
              <p className="muted small" style={{ textAlign: "center" }}>{label(r.band)} · {ex.band_stability ?? ""}</p>
            </div>
          </div>
        </Card>
        <Card title="Why this recommendation">
          <h4>Top model drivers</h4>
          <ol className="reasons">
            {(ex.top_reasons as Json[]).map((x) => (
              <li key={x.feature}>
                <span className="bar" style={{ width: `${Math.min(100, Math.round(x.strength * 160))}%` }} />
                {x.text} <small className="muted">({x.effect})</small>
              </li>
            ))}
          </ol>
          {ex.evidence && (
            <>
              <h4>Evidence from the call note</h4>
              <div className="quote">“{ex.evidence.snippet}”<br /><small className="muted">{ex.evidence.source} · {ex.evidence.note_id}</small></div>
            </>
          )}
          <h4>What would change this</h4>
          <ul className="reasons small">{(ex.what_would_change_this as string[]).map((w: string) => <li key={w}>{w}</li>)}</ul>
          <div className="chips">{(ex.policy_citations as string[]).map((c) => <Badge key={c} tone="gray">{c}</Badge>)}</div>
        </Card>
      </div>

      {triggers.length > 0 && (
        <Card title="Why a person must look at this">
          {triggers.map((t) => <div className="trigger" key={t.trigger}><Badge tone="amber">{label(t.trigger)}</Badge> {t.why}</div>)}
        </Card>
      )}

      <Card title="Policy gates" right={<small className="muted">re-checked at review time, so an override can never breach a gate</small>}>
        <Table rows={r.gates as Gate[]} cols={[
          { h: "Gate", r: (g) => label(g.gate) },
          { h: "Result", r: (g) => <Badge tone={gateTone(g.status)}>{g.status}</Badge> },
          { h: "Why", r: (g) => <small>{g.detail}</small> },
          { h: "Source", r: (g) => <small className="muted">{g.citation ?? ""}</small> },
        ]} />
      </Card>

      <div className="grid-2">
        <Card title="Identity (Golden C360)">
          <Kv k="Customer key" v={r.identity?.customer_key ?? "–"} />
          <Kv k="Match confidence" v={r.identity?.match_confidence ?? "–"} />
          <Kv k="Trust score" v={`${r.identity?.trust_score ?? "–"} (review below ${r.identity?.trust_review_threshold})`} />
          <Kv k="Conflict flags" v={(r.identity?.conflict_flags ?? []).map(label).join(", ") || "none"} />
        </Card>
        <Card title="Record integrity">
          <Kv k="Model" v={`${r.model.name} ${r.model.version} · features ${r.model.feature_version}`} />
          <Kv k="Model file hash" v={<code>{String(r.model.file_sha256).slice(0, 14)}…</code>} />
          <Kv k="Protected attributes used" v="no (audited at scoring time)" />
          <Kv k="Created" v={when(r.created_at)} />
          <Kv k="Code commit" v={r.policy_config.code_commit} />
        </Card>
      </div>

      <ReviewPanel decisionId={id} record={r} roles={roles} onDone={load} />

      <Card title="Review history">
        <Table rows={d.reviews} empty="Nobody has reviewed this record yet." cols={[
          { h: "When", r: (x) => when(x.reviewed_at) }, { h: "Reviewer", r: (x) => `${x.reviewer_id} (${x.role})` },
          { h: "Decision", r: (x) => label(x.decision) }, { h: "Final action", r: (x) => label(x.final_action) },
          { h: "Reason", r: (x) => x.reason }, { h: "Identity verified", r: (x) => (x.identity_verified ? "yes" : "no") },
        ]} />
        {d.attempts.length > 0 && (
          <>
            <h4>All attempts, including refused ones</h4>
            <Table rows={d.attempts} cols={[
              { h: "When", r: (x) => when(x.attempted_at) }, { h: "Reviewer", r: (x) => `${x.reviewer_id} (${x.role})` },
              { h: "Requested", r: (x) => `${label(x.requested_decision)}${x.requested_final_action ? ` → ${label(x.requested_final_action)}` : ""}` },
              { h: "Outcome", r: (x) => <Badge tone={x.outcome === "recorded" ? "green" : "red"}>{x.outcome}</Badge> },
              { h: "Detail", r: (x) => <small>{x.outcome === "refused" ? x.detail : ""}</small> },
            ]} />
          </>
        )}
      </Card>
      <p className="muted small">Risk {pct(r.risk)} is a calibrated probability that the promise breaks. {r.risk_note}</p>
    </>
  );
}

function ReviewPanel({ decisionId, record, roles, onDone }: { decisionId: string; record: Json; roles: Record<string, string>; onDone: () => void }) {
  const reviewers = Object.keys(roles);
  const [who, setWho] = useState("");
  const [decision, setDecision] = useState<"accept" | "change" | "escalate">("accept");
  const [finalAction, setFinalAction] = useState("");
  const [reason, setReason] = useState("");
  const [confirmHardship, setConfirmHardship] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string; code?: string } | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (!who && reviewers.length) setWho(reviewers.includes("REV-01") ? "REV-01" : reviewers[0]); }, [reviewers, who]);
  const allowed = record.allowed_actions as string[];
  useEffect(() => { if (!finalAction && allowed.length) setFinalAction(allowed[0]); }, [allowed, finalAction]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    try {
      const out = await api.review(decisionId, {
        reviewer_id: who, role: roles[who], decision, reason,
        ...(decision === "change" ? { final_action: finalAction } : {}), confirm_hardship: confirmHardship,
      });
      setMsg({ ok: true, text: `Recorded: final action is ${label(out.review.final_action)}${out.review.child_decision_id ? ` (new record ${out.review.child_decision_id} created for the confirmed hardship)` : ""}.` });
      setReason("");
      onDone();
    } catch (ex) {
      const e2 = ex as ApiError;
      setMsg({ ok: false, text: e2.message, code: e2.code });
      onDone();
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title="Human review and override" right={<Badge tone={record.review_required ? "amber" : "gray"}>{record.review_required ? "required by policy" : "optional: anyone can review anything"}</Badge>}>
      <form className="form" onSubmit={submit}>
        <label>Reviewer
          <select value={who} onChange={(e) => setWho(e.target.value)}>
            {reviewers.map((k) => <option key={k} value={k}>{k} · {roles[k]}</option>)}
          </select>
        </label>
        <label>Decision
          <select value={decision} onChange={(e) => setDecision(e.target.value as typeof decision)}>
            <option value="accept">Accept the recommendation</option>
            <option value="change">Override with a different action</option>
            <option value="escalate">Escalate to a specialist</option>
          </select>
        </label>
        {decision === "change" && (
          <label className="wide">Final action
            <select value={finalAction} onChange={(e) => setFinalAction(e.target.value)}>
              {["sms_payment_link", "email_reminder", "call_daytime", "call_evening", "offer_hardship_plan", "specialist_review", "no_action"].map((a) => (
                <option key={a} value={a}>{label(a)}{allowed.includes(a) ? "" : "  (blocked by a gate)"}</option>
              ))}
            </select>
          </label>
        )}
        <label className="wide">Reason (required, stored in the audit trail)
          <textarea rows={2} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. customer confirmed the payment date on the phone" />
        </label>
        {record.hardship_status !== "confirmed" && (
          <label className="wide" style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
            <input type="checkbox" style={{ width: "auto" }} checked={confirmHardship} onChange={(e) => setConfirmHardship(e.target.checked)} />
            I have confirmed hardship with the customer (reviewer roles only; creates a new, softer record)
          </label>
        )}
        <div className="wide"><button className="primary" disabled={busy || !who || reason.trim().length < 3}>{busy ? "Recording…" : "Record decision"}</button></div>
      </form>
      {msg && (msg.ok
        ? <div className="note"><b>{msg.text}</b></div>
        : <div className="error" role="alert"><b>Refused: {msg.text}</b><small>{msg.code}. The refused attempt is logged below.</small></div>)}
    </Card>
  );
}
