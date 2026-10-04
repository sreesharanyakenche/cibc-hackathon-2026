import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type Json, type Upcoming } from "../api";
import { Badge, Card, ErrorBox, Spinner, Table } from "../components/ui";
import { label, pct } from "../format";

export const bandTone = (b?: string | null) => (b === "very_high" || b === "high" ? "red" : b === "medium" ? "amber" : "green");

export default function QueuePage() {
  const nav = useNavigate();
  const [days, setDays] = useState(3);
  const [rows, setRows] = useState<Upcoming[] | null>(null);
  const [asOf, setAsOf] = useState("");
  const [err, setErr] = useState<unknown>(null);
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<Json[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    setRows(null);
    setErr(null);
    api.upcoming(days).then((r) => { setRows(r.promises); setAsOf(r.as_of); }).catch(setErr);
  }, [days]);

  const search = (e: React.FormEvent) => {
    e.preventDefault();
    if (q.trim().length < 2) return;
    setErr(null);
    api.search(q.trim()).then((r) => setHits(r.results)).catch(setErr);
  };
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

  return (
    <>
      <h1>Agent queue <small>promises to pay due soon, scored in full, riskiest first{asOf ? ` · as of ${asOf}` : ""}</small></h1>
      <Card title="Find a customer" right={<small className="muted">name, customer key or CRM id</small>}>
        <form className="row" onSubmit={search}>
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="e.g. Desjardins, or ZOD-3695" aria-label="customer search" />
          <button className="primary" type="submit">Search</button>
        </form>
        {hits && (
          <Table empty="No customer matches." rows={hits} cols={[
            { h: "Customer", r: (c) => <Link to={`/customers/${c.customer_key}`}>{c.first_name} {c.last_name}</Link> },
            { h: "Key", r: (c) => c.customer_key },
            { h: "Born", r: (c) => c.date_of_birth ?? "–" },
            { h: "Prov.", r: (c) => c.province_code ?? "–" },
            { h: "Open promises", cls: "num", r: (c) => c.n_ptp_open },
            { h: "Identity trust", r: (c) => <Badge tone={c.identity_review_required ? "amber" : "green"}>{c.trust_score}{c.identity_review_required ? " · review" : ""}</Badge> },
          ]} />
        )}
      </Card>
      {err ? <ErrorBox error={err} /> : null}
      <Card
        title="Due soon"
        right={
          <select value={days} onChange={(e) => setDays(+e.target.value)} aria-label="horizon">
            <option value={0}>due today</option>
            <option value={3}>next 3 days</option>
            <option value={7}>next 7 days</option>
          </select>
        }
      >
        {!rows ? <Spinner text="Scoring every open promise in the window…" /> : (
          <Table rows={rows} empty="No open promises in this window." cols={[
            { h: "Promise", r: (p) => p.ptp_id },
            { h: "Due", r: (p) => p.due },
            { h: "Break risk", cls: "num", r: (p) => pct(p.risk) },
            { h: "Band", r: (p) => <Badge tone={bandTone(p.band)}>{label(p.band)}</Badge> },
            { h: "", r: (p) => <button className="primary" disabled={busy === p.ptp_id} onClick={() => recommend(p.ptp_id)}>{busy === p.ptp_id ? "Working…" : "Recommend"}</button> },
          ]} />
        )}
        <p className="muted small">Each recommendation runs score, policy gates, ranking and explanation, then writes an immutable Decision Record.</p>
      </Card>
    </>
  );
}
