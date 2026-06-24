import type {
  ChatResponse,
  Customer,
  Health,
  RunDetail,
  RunSummary,
} from "./types";

// Always relative: dev proxies /api -> backend; prod serves dist from backend.
const BASE = "/api";

async function jget<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) throw new Error(`GET ${path} -> ${res.status}`);
  return res.json() as Promise<T>;
}

export async function getHealth(): Promise<Health> {
  return jget<Health>("/health");
}

export async function sendChat(
  message: string,
  conversationId: string | null,
  engine: string
): Promise<ChatResponse> {
  const res = await fetch(`${BASE}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message,
      conversation_id: conversationId,
      engine,
    }),
  });
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`chat failed (${res.status}): ${detail}`);
  }
  return res.json() as Promise<ChatResponse>;
}

export async function getRuns(): Promise<RunSummary[]> {
  const data = await jget<{ runs: RunSummary[] }>("/runs");
  return data.runs;
}

export async function getRun(runId: string): Promise<RunDetail> {
  return jget<RunDetail>(`/runs/${runId}`);
}

export async function getPolicy(): Promise<string> {
  const res = await fetch(`${BASE}/policy`);
  return res.text();
}

export async function getCustomers(): Promise<{
  reference_date: string;
  customers: Customer[];
}> {
  return jget("/customers");
}
