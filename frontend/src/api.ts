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

// Stream one turn live via SSE. Calls onEvent for each `data:` frame
// (tool_start, tool, assistant, info, done, error).
export async function streamChat(
  message: string,
  conversationId: string | null,
  engine: string,
  onEvent: (ev: any) => void
): Promise<void> {
  const res = await fetch(`${BASE}/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, conversation_id: conversationId, engine }),
  });
  if (!res.ok || !res.body) throw new Error(`stream failed (${res.status})`);
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx: number;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const line = frame.split("\n").find((l) => l.startsWith("data:"));
      if (!line) continue; // keep-alive comment frame
      try {
        onEvent(JSON.parse(line.slice(5).trim()));
      } catch {
        /* ignore malformed frame */
      }
    }
  }
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
