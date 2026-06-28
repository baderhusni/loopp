import type { RunDetail, TraceEvent } from "../types";
import { toolMeta } from "../toolMeta";

function fmt(obj: unknown): string {
  return JSON.stringify(obj, null, 2);
}

function Metric({ k, v }: { k: string; v: string }) {
  return (
    <div className="metric">
      <div className="k">{k}</div>
      <div className="v">{v}</div>
    </div>
  );
}

function ToolStep({ e }: { e: TraceEvent }) {
  return (
    <div className={`step tool ${e.is_error ? "err" : ""}`}>
      <div className="step-head">
        <code className="tname-badge" style={{ background: toolMeta(e.name!).color }}>{e.name}</code>
        <span className="tlabel">{toolMeta(e.name!).label}</span>
        {e.is_error ? <span className="tag-err">ERROR</span> : <span className="tag-ok">ok</span>}
        {e.latency_ms != null && <span className="muted">{e.latency_ms} ms</span>}
        <span className="t">@ {Math.round(e.t_ms)} ms</span>
      </div>
      <div className="step-body">
        <div className="io-label">input</div>
        <pre className="io">{fmt(e.input)}</pre>
        <div className="io-label">output</div>
        <pre className="io">{fmt(e.output)}</pre>
      </div>
    </div>
  );
}

export default function TraceView({ run }: { run: RunDetail | null }) {
  if (!run) {
    return <div className="trace-detail"><div className="loading">Select a run to inspect its trace.</div></div>;
  }

  const tools = run.events.filter((e) => e.type === "tool");
  const errors = tools.filter((e) => e.is_error).length;
  const tokens = run.usage?.total_tokens;
  const dur = run.duration_ms ?? run.wall_ms;

  return (
    <div className="trace-detail">
      <div className="trace-summary">
        <Metric k="decision" v={run.decision ?? "—"} />
        <Metric k="engine" v={run.engine} />
        <Metric k="model" v={run.model ?? "—"} />
        <Metric k="tool calls" v={String(tools.length)} />
        <Metric k="errors / retries" v={String(errors)} />
        <Metric k="latency" v={dur != null ? `${Math.round(dur)} ms` : "—"} />
        <Metric k="tokens" v={tokens != null ? tokens.toLocaleString() : "—"} />
        <Metric k="SDK cost (USD)" v={run.cost_usd != null ? `$${run.cost_usd.toFixed(4)}` : "$0"} />
        {run.num_turns != null && <Metric k="model turns" v={String(run.num_turns)} />}
      </div>

      {run.fallback_reason && (
        <div className="step tool err">
          <div className="step-head"><span className="name">fallback</span></div>
          <div className="step-body">{run.fallback_reason}</div>
        </div>
      )}

      <div className="io-label" style={{ marginBottom: 8 }}>
        customer said: “{run.user_message}”
      </div>

      {run.events.map((e) => {
        if (e.type === "tool") return <ToolStep key={e.seq} e={e} />;
        if (e.type === "assistant") {
          return (
            <div className="step assistant" key={e.seq}>
              <div className="step-head">
                <span className="name">agent reasoning / reply</span>
                <span className="t">@ {Math.round(e.t_ms)} ms</span>
              </div>
              <div className="step-body assistant-text">{e.text}</div>
            </div>
          );
        }
        return (
          <div className="step" key={e.seq}>
            <div className="step-head"><span className="name">note</span><span className="t">@ {Math.round(e.t_ms)} ms</span></div>
            <div className="step-body">{e.message}</div>
          </div>
        );
      })}
    </div>
  );
}
