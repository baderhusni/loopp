export interface Usage {
  input_tokens: number;
  output_tokens: number;
  cache_read_input_tokens: number;
  cache_creation_input_tokens: number;
  total_tokens: number;
}

export type Decision = "APPROVED" | "DENIED" | "ESCALATED" | null;

export interface ChatResponse {
  conversation_id: string;
  run_id: string;
  reply: string;
  decision: Decision;
  engine: string;
  model: string | null;
  usage: Usage | null;
  cost_usd: number | null;
  duration_ms: number | null;
  wall_ms: number | null;
  num_tool_calls: number;
  fallback_reason: string | null;
}

export interface RunSummary {
  run_id: string;
  conversation_id: string;
  started_at: string;
  user_message: string;
  decision: Decision;
  engine: string;
  duration_ms: number | null;
  num_tool_calls: number;
  num_errors: number;
  usage: Usage | null;
}

export interface TraceEvent {
  seq: number;
  t_ms: number;
  type: "tool" | "assistant" | "info";
  name?: string;
  input?: Record<string, unknown>;
  output?: Record<string, unknown>;
  latency_ms?: number;
  is_error?: boolean;
  text?: string;
  message?: string;
}

export interface RunDetail {
  run_id: string;
  conversation_id: string;
  started_at: string;
  user_message: string;
  reply: string;
  engine: string;
  model: string | null;
  decision: Decision;
  events: TraceEvent[];
  usage: Usage | null;
  cost_usd: number | null;
  duration_ms: number | null;
  wall_ms: number | null;
  num_turns: number | null;
  fallback_reason: string | null;
}

export interface Health {
  status: string;
  sdk_available: boolean;
  default_engine: string;
  reference_date: string;
  model: string;
}

export interface Item {
  id: string;
  name: string;
  sku: string;
  category: string;
  unit_price: number;
  quantity: number;
  final_sale: boolean;
  refunded: boolean;
}

export interface Order {
  id: string;
  status: string;
  placed_at: string;
  delivered_at: string | null;
  items: Item[];
}

export interface Customer {
  id: string;
  name: string;
  email: string;
  tier: string;
  member_since: string;
  orders: Order[];
}

export interface Tool {
  name: string;
  description: string;
  params: string[];
  required: string[];
}

export interface ChatMessage {
  role: "customer" | "agent";
  text: string;
  meta?: ChatResponse;
}
