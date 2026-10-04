import type { ReactNode } from "react";
import { ApiError } from "../api";

export const Card = ({ title, right, children, className = "" }: { title?: ReactNode; right?: ReactNode; children: ReactNode; className?: string }) => (
  <section className={`card ${className}`}>
    {(title || right) && <header className="card-h"><h3>{title}</h3><div>{right}</div></header>}
    {children}
  </section>
);

export const Badge = ({ tone = "gray", children }: { tone?: "gray" | "green" | "amber" | "red" | "blue" | "purple"; children: ReactNode }) => <span className={`badge ${tone}`}>{children}</span>;

export const toneForRisk = (level?: string | null) => (level === "HIGH" ? "red" : level === "MEDIUM" ? "amber" : level === "LOW" ? "green" : "gray");
export const toneForStatus = (s: string) => (s === "APPROVED" || s === "FINAL_AUTO" || s === "AUTO_PROCEED" ? "green" : s === "REJECTED" ? "red" : s === "PENDING" || s === "PENDING_REVIEW" || s === "HUMAN_REVIEW" ? "amber" : "blue");

export const Kv = ({ k, v }: { k: string; v: ReactNode }) => <div className="kv"><span>{k}</span><b>{v ?? "–"}</b></div>;

export const Spinner = ({ text = "Loading…" }: { text?: string }) => <div className="spinner"><i /> {text}</div>;

export function ErrorBox({ error }: { error: unknown }) {
  const e = error instanceof ApiError ? error : new ApiError(0, "error", String(error));
  return (
    <div className="error" role="alert">
      <b>{e.message}</b>
      <small>{e.code}{e.status ? ` · HTTP ${e.status}` : ""}{e.requestId ? ` · request ${e.requestId}` : ""}</small>
    </div>
  );
}

export function Table({ cols, rows, empty = "Nothing to show." }: { cols: { h: string; r: (row: any) => ReactNode; cls?: string }[]; rows: any[]; empty?: string }) { // eslint-disable-line @typescript-eslint/no-explicit-any
  if (!rows?.length) return <p className="muted">{empty}</p>;
  return (
    <div className="table-wrap">
      <table>
        <thead><tr>{cols.map((c) => <th key={c.h} className={c.cls}>{c.h}</th>)}</tr></thead>
        <tbody>{rows.map((row, i) => <tr key={i}>{cols.map((c) => <td key={c.h} className={c.cls}>{c.r(row)}</td>)}</tr>)}</tbody>
      </table>
    </div>
  );
}

/** Semi-circular risk gauge, 0-100 %. */
export function RiskGauge({ value, level }: { value: number | null; level?: string | null }) {
  const v = value == null ? 0 : Math.max(0, Math.min(1, value));
  const color = level === "very_high" || level === "high" ? "var(--red)" : level === "medium" ? "var(--amber)" : "var(--green)";
  const r = 80, c = Math.PI * r;
  return (
    <svg viewBox="0 0 200 118" className="gauge" role="img" aria-label={value == null ? "risk unavailable" : `risk ${(v * 100).toFixed(1)} percent`}>
      <path d="M20 100 A80 80 0 0 1 180 100" fill="none" stroke="var(--line)" strokeWidth="16" strokeLinecap="round" />
      {value != null && <path d="M20 100 A80 80 0 0 1 180 100" fill="none" stroke={color} strokeWidth="16" strokeLinecap="round" strokeDasharray={`${c * v} ${c}`} />}
      <text x="100" y="92" textAnchor="middle" className="gauge-n">{value == null ? "n/a" : `${(v * 100).toFixed(1)}%`}</text>
      <text x="100" y="112" textAnchor="middle" className="gauge-l">{level ?? "model unavailable"}</text>
    </svg>
  );
}

export function Steps({ steps }: { steps: { id: string; label: string; state: "wait" | "run" | "done" | "skip" }[] }) {
  return (
    <ol className="steps">
      {steps.map((s) => <li key={s.id} className={s.state}><i />{s.label}</li>)}
    </ol>
  );
}
