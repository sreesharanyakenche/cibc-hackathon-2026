import { useState } from "react";
import { api, type Answer } from "../api";
import { Badge, Card, ErrorBox, Spinner, Table } from "../components/ui";
import { label } from "../format";

const EXAMPLES = [
  "How many open collections cases are there by bucket?",
  "What is the promise-to-pay kept rate by channel last month?",
  "What is the maximum number of outbound call attempts in a rolling 7 days?",
  "Can we call a customer on a statutory holiday?",
  "What is the weather in Toronto?",
  "Which customers are Indigenous?",
];

/** The engine returns small tables as CSV text; show them as tables. */
function parseCsv(text: string): string[][] | null {
  const lines = text.split("\n");
  if (lines.length < 2 || !lines[0].includes(",") || lines[0].includes(". ")) return null;
  const w = lines[0].split(",").length;
  const rows = lines.map((l) => l.split(","));
  return rows.every((r) => r.length === w) ? rows : null;
}

export default function AskPage() {
  const [q, setQ] = useState("");
  const [a, setA] = useState<Answer | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const ask = async (text: string) => {
    setQ(text);
    setBusy(true);
    setErr(null);
    setA(null);
    try { setA(await api.ask(text)); } catch (e) { setErr(e); } finally { setBusy(false); }
  };
  const table = a && !a.refused ? parseCsv(a.answer) : null;
  return (
    <>
      <h1>Ask the data <small>plain-English questions; every answer shows its SQL or sources, and out-of-scope questions are refused</small></h1>
      <Card>
        <form className="row" onSubmit={(e) => { e.preventDefault(); if (q.trim().length >= 3) ask(q.trim()); }}>
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask about cases, promises, contacts or collections policy" aria-label="question" />
          <button className="primary" disabled={busy}>Ask</button>
        </form>
        <div className="ex">{EXAMPLES.map((x) => <button key={x} onClick={() => ask(x)}>{x}</button>)}</div>
      </Card>
      {busy && <Spinner text="Planning, validating and running the query…" />}
      {err ? <ErrorBox error={err} /> : null}
      {a && (
        <Card title="Answer" right={<><Badge tone={a.refused ? "amber" : "green"}>{a.refused ? "refused" : "answered"}</Badge> <Badge tone="blue">route: {a.route}</Badge></>}>
          {a.refused ? (
            <div className="refusal"><b>{a.answer}</b><br /><small className="muted">reason: {label(a.refusal_reason)}</small></div>
          ) : table ? (
            <Table rows={table.slice(1)} cols={table[0].map((h, i) => ({ h: label(h), r: (row: string[]) => row[i], cls: /^-?[\d.,]+%?$/.test(table[1][i]) ? "num" : "" }))} />
          ) : (
            <div className="answer"><pre>{a.answer}</pre></div>
          )}
          {a.sql_or_sources && (
            <>
              <h4>{a.route === "sql" || a.sql_or_sources.trim().toLowerCase().startsWith("select") || a.sql_or_sources.trim().toLowerCase().startsWith("with") ? "SQL that produced this" : "Sources"}</h4>
              <pre className="sql">{a.sql_or_sources}</pre>
            </>
          )}
          {Object.keys(a.understood ?? {}).length > 0 && (
            <div className="chips">{Object.entries(a.understood).filter(([, v]) => v != null).map(([k, v]) => <Badge key={k} tone="gray">{label(k)}: {typeof v === "object" ? JSON.stringify(v) : String(v)}</Badge>)}</div>
          )}
          {a.notes?.length > 0 && <ul className="reasons small">{a.notes.map((n) => <li key={n}>{n}</li>)}</ul>}
        </Card>
      )}
    </>
  );
}
