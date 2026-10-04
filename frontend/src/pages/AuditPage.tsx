import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Json } from "../api";
import { Badge, Card, ErrorBox, Kv, Spinner, Table } from "../components/ui";
import { label, pct, when } from "../format";
import { bandTone } from "./QueuePage";

export default function AuditPage() {
  const [rows, setRows] = useState<Json[] | null>(null);
  const [chain, setChain] = useState<Json | null>(null);
  const [err, setErr] = useState<unknown>(null);
  useEffect(() => {
    api.decisions(60).then((r) => setRows(r.decisions)).catch(setErr);
    api.verify().then(setChain).catch(setErr);
  }, []);
  if (err) return <ErrorBox error={err} />;
  if (!rows) return <Spinner />;
  return (
    <>
      <h1>Decisions &amp; audit <small>every recommendation is an immutable, hash-chained Decision Record</small></h1>
      <Card title="Hash chain" right={chain && <Badge tone={chain.ok ? "green" : "red"}>{chain.ok ? "intact" : "TAMPERING DETECTED"}</Badge>}>
        {chain && (
          <>
            <Kv k="Records verified" v={chain.checked} />
            <Kv k="Records failing verification" v={chain.bad?.length ? chain.bad.join(", ") : "none"} />
            <Kv k="Earlier records without a hash (pre-chain)" v={chain.legacy_unhashed} />
            <p className="muted small">Each record stores its feature vector, gate inputs, model file hash, thresholds and holiday calendar, so any decision can be replayed. Records are never edited: reviews live in their own table and every attempt, including refused ones, is logged.</p>
          </>
        )}
      </Card>
      <Card title="Recent decisions">
        <Table rows={rows} empty="No decisions yet. Generate one from the agent queue." cols={[
          { h: "Record", r: (x) => <Link to={`/decision/${x.decision_id}`}>{x.decision_id}</Link> },
          { h: "Created", r: (x) => when(x.created_at) },
          { h: "Promise", r: (x) => x.ptp_id },
          { h: "Risk", cls: "num", r: (x) => pct(x.risk) },
          { h: "Band", r: (x) => <Badge tone={bandTone(x.band)}>{label(x.band)}</Badge> },
          { h: "Recommended", r: (x) => label(x.recommended_action) },
          { h: "Review", r: (x) => (x.review_required ? <Badge tone="amber">required</Badge> : "no") },
          { h: "Hardship", r: (x) => (x.hardship_status === "none" ? "–" : label(x.hardship_status)) },
        ]} />
      </Card>
    </>
  );
}
