import { useEffect, useState } from "react";
import { getTools } from "../api";
import type { Tool } from "../types";
import { toolMeta } from "../toolMeta";

interface Props {
  variant?: "strip" | "full";
  activeTool?: string | null;
  usedCounts?: Record<string, number>;
}

export default function ToolsPanel({ variant = "full", activeTool = null, usedCounts = {} }: Props) {
  const [tools, setTools] = useState<Tool[]>([]);
  useEffect(() => {
    getTools().then(setTools).catch(() => setTools([]));
  }, []);

  if (variant === "strip") {
    return (
      <div className="tools-strip">
        <span className="tools-strip-label">Agent tools ({tools.length})</span>
        {tools.map((t) => {
          const m = toolMeta(t.name);
          const active = activeTool === t.name;
          const used = usedCounts[t.name] || 0;
          return (
            <span
              key={t.name}
              className={`tool-chip ${active ? "active" : ""} ${used ? "used" : ""}`}
              style={{ ["--tc" as string]: m.color }}
              title={`${m.label} — ${t.description}`}
            >
              <span className="tdot" />
              <span className="ticon">{m.icon}</span>
              <code>{t.name}</code>
              {used > 0 && <span className="tcount">×{used}</span>}
            </span>
          );
        })}
      </div>
    );
  }

  return (
    <div className="tools-full">
      <p className="muted" style={{ fontSize: 13, margin: "0 0 12px" }}>
        The agent can call <b>only</b> these {tools.length} tools — no shell, no file access, no network.
        The refund guardrail lives inside them: <code>issue_refund</code> re-checks the policy and refuses
        anything not approved.
      </p>
      {tools.map((t) => {
        const m = toolMeta(t.name);
        return (
          <div className="tool-card" key={t.name} style={{ borderLeftColor: m.color }}>
            <div className="tool-card-head">
              <span className="ticon">{m.icon}</span>
              <code className="tname-badge" style={{ background: m.color }}>{t.name}</code>
              <span className="tlabel">{m.label}</span>
            </div>
            <div className="tool-desc">{t.description}</div>
            {t.params.length > 0 && (
              <div className="tool-params">
                {t.params.map((p) => (
                  <span key={p} className={`param ${t.required.includes(p) ? "req" : ""}`}>
                    {p}{t.required.includes(p) ? " *" : ""}
                  </span>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
