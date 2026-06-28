// Stable colour / label / icon per tool, used consistently in the Tools panel,
// the live feed, and the admin trace so it's always obvious which tool ran.

export interface ToolMeta {
  label: string;
  color: string;
  icon: string;
}

const MAP: Record<string, ToolMeta> = {
  find_customer: { label: "Look up customer", color: "#2563eb", icon: "🔍" },
  get_order: { label: "Fetch order", color: "#0891b2", icon: "📦" },
  check_refund_eligibility: { label: "Check policy", color: "#7c3aed", icon: "⚖️" },
  issue_refund: { label: "Process refund", color: "#16a34a", icon: "💸" },
  escalate_to_human: { label: "Escalate to human", color: "#d97706", icon: "🙋" },
};

export function toolMeta(name: string): ToolMeta {
  return MAP[name] ?? { label: name, color: "#64748b", icon: "🔧" };
}
