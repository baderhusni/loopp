import { useEffect, useState } from "react";
import { getRun, getRuns } from "../api";
import type { RunDetail, RunSummary } from "../types";
import TraceView from "./TraceView";
import PolicyView from "./PolicyView";
import CrmExplorer from "./CrmExplorer";

type Tab = "trace" | "policy" | "crm";

interface Props {
  lastRunId: string | null;
  refreshTick: number;
}

export default function AdminDashboard({ lastRunId, refreshTick }: Props) {
  const [tab, setTab] = useState<Tab>("trace");
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [runDetail, setRunDetail] = useState<RunDetail | null>(null);

  async function refreshRuns() {
    try {
      setRuns(await getRuns());
    } catch {
      /* backend may be momentarily unavailable */
    }
  }

  // Refetch the runs list whenever a new turn completes, and auto-select it.
  useEffect(() => {
    refreshRuns();
    if (lastRunId) setSelectedRunId(lastRunId);
  }, [refreshTick]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    refreshRuns();
  }, []);

  useEffect(() => {
    if (!selectedRunId) {
      setRunDetail(null);
      return;
    }
    getRun(selectedRunId).then(setRunDetail).catch(() => setRunDetail(null));
  }, [selectedRunId, refreshTick]);

  return (
    <section className="pane">
      <div className="pane-head">
        <h2>Admin dashboard</h2>
        <div className="spacer" />
        <div className="tabs">
          <button className={`tab ${tab === "trace" ? "active" : ""}`} onClick={() => setTab("trace")}>
            Agent trace
          </button>
          <button className={`tab ${tab === "crm" ? "active" : ""}`} onClick={() => setTab("crm")}>
            CRM data
          </button>
          <button className={`tab ${tab === "policy" ? "active" : ""}`} onClick={() => setTab("policy")}>
            Policy
          </button>
        </div>
      </div>

      {tab === "trace" && (
        <div className="admin-body">
          <div className="runs-list">
            {runs.length === 0 && <div className="loading">No runs yet.</div>}
            {runs.map((r) => (
              <div
                key={r.run_id}
                className={`run-item ${r.run_id === selectedRunId ? "active" : ""}`}
                onClick={() => setSelectedRunId(r.run_id)}
              >
                <div className="rid">{r.run_id} · {r.engine}</div>
                <div className="umsg">{r.user_message}</div>
                <div className="row">
                  <span className={`decision ${r.decision ?? "none"}`}>{r.decision ?? "INFO"}</span>
                  <span className="pill">{r.num_tool_calls} tools</span>
                  {r.num_errors > 0 && <span className="pill err">{r.num_errors} err</span>}
                  {r.duration_ms != null && <span className="pill">{Math.round(r.duration_ms)} ms</span>}
                </div>
              </div>
            ))}
          </div>
          <TraceView run={runDetail} />
        </div>
      )}

      {tab === "policy" && <PolicyView />}
      {tab === "crm" && <CrmExplorer />}
    </section>
  );
}
