// Typed client for the collections API (app/main.py). Errors carry the server's code and request id so they can be shown and traced.
export type Json = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public requestId?: string, public details?: unknown) {
    super(message);
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { ...init, headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) } });
  } catch {
    throw new ApiError(0, "network_error", "Cannot reach the API. Is it running on port 8000?");
  }
  const text = await res.text();
  const body = text ? JSON.parse(text) : {};
  if (!res.ok) {
    const e = body?.error ?? {};
    throw new ApiError(res.status, e.code ?? "http_error", e.message ?? res.statusText, e.request_id ?? res.headers.get("x-request-id") ?? undefined, e.details);
  }
  return body as T;
}
const post = <T,>(path: string, data: unknown) => req<T>(path, { method: "POST", body: JSON.stringify(data) });

export interface Upcoming { ptp_id: string; due: string; risk: number; band: string }
export interface Gate { gate: string; status: string; detail: string; citation: string | null; allowed: string[] | null }
export interface Trigger { trigger: string; fired: boolean; why: string; kind: string }
export interface Answer {
  question: string; answer: string; sql_or_sources: string; refused: boolean; route: string; refusal_reason: string | null;
  plan: string | null; understood: Json; evidence: Json[]; notes: string[]; request_id: string;
}

export const api = {
  health: () => req<Json>("/api/health"),
  search: (q: string) => req<{ results: Json[]; count: number }>(`/api/customers?q=${encodeURIComponent(q)}&limit=20`),
  customer: (id: string) => req<Json>(`/api/customers/${encodeURIComponent(id)}`),
  upcoming: (days: number, limit = 25) => req<{ promises: Upcoming[]; count: number; as_of: string }>(`/api/promises/upcoming?days=${days}&limit=${limit}`),
  recommend: (ptp_id: string) => post<{ record: Json; state: Json }>("/api/recommend", { ptp_id }),
  decisions: (limit = 40) => req<{ decisions: Json[] }>(`/api/decisions?limit=${limit}`),
  decision: (id: string) => req<{ record: Json; state: Json; reviews: Json[]; attempts: Json[] }>(`/api/decisions/${encodeURIComponent(id)}`),
  review: (id: string, body: Json) => post<{ review: Json; state: Json }>(`/api/decisions/${encodeURIComponent(id)}/review`, body),
  verify: () => req<Json>("/api/audit/verify"),
  ask: (question: string) => post<Answer>("/api/ask", { question }),
};
