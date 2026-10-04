export const money = (v: unknown) => (typeof v === "number" ? v.toLocaleString("en-CA", { style: "currency", currency: "CAD", maximumFractionDigits: 2 }) : "–");
export const num = (v: unknown, d = 0) => (typeof v === "number" ? v.toLocaleString("en-CA", { maximumFractionDigits: d }) : "–");
export const pct = (v: unknown, d = 1) => (typeof v === "number" ? `${(v * 100).toFixed(d)}%` : "–");
export const when = (v: unknown) => { if (typeof v !== "string") return "–"; const d = new Date(v); return isNaN(d.getTime()) ? v : d.toLocaleString("en-CA", { dateStyle: "medium", timeStyle: v.length > 10 ? "short" : undefined }); };
export const day = (v: unknown) => (typeof v === "string" ? v.slice(0, 10) : "–");
export const label = (s: string | null | undefined) => (s ?? "–").replace(/_/g, " ");
export const ACTION_LABEL: Record<string, string> = { sms: "SMS reminder", email: "Email reminder", call: "Phone call", callback: "Scheduled callback", no_action: "No action", payment_plan_discussion: "Payment-plan discussion", specialist_review: "Specialist review", human_review: "Hold for human review" };
export const actionLabel = (a: string | null | undefined) => (a ? ACTION_LABEL[a] ?? label(a) : "–");
