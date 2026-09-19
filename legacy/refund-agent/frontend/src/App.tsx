import { useEffect, useState } from "react";
import { getHealth, sendChat } from "./api";
import type { ChatMessage, Health } from "./types";
import ChatWindow from "./components/ChatWindow";
import AdminDashboard from "./components/AdminDashboard";
import LiveAgent from "./components/LiveAgent";

type View = "console" | "live";

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [engine, setEngine] = useState<string>("auto");
  const [view, setView] = useState<View>("console");
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [busy, setBusy] = useState(false);
  const [lastRunId, setLastRunId] = useState<string | null>(null);
  const [refreshTick, setRefreshTick] = useState(0);

  useEffect(() => {
    getHealth().then(setHealth).catch(() => setHealth(null));
  }, []);

  async function handleSend(text: string) {
    if (!text.trim() || busy) return;
    setMessages((m) => [...m, { role: "customer", text }]);
    setBusy(true);
    try {
      const res = await sendChat(text, conversationId, engine);
      setConversationId(res.conversation_id);
      setMessages((m) => [...m, { role: "agent", text: res.reply, meta: res }]);
      setLastRunId(res.run_id);
      setRefreshTick((t) => t + 1);
    } catch (e) {
      setMessages((m) => [...m, { role: "agent", text: `⚠️ ${String(e)}` }]);
    } finally {
      setBusy(false);
    }
  }

  function handleNew() {
    setMessages([]);
    setConversationId(null);
  }

  return (
    <div className="app">
      <header className="header">
        <div>
          <h1>Acme Refund Support Agent</h1>
          <div className="sub">AI agent that processes or denies e-commerce refunds</div>
        </div>
        <nav className="viewnav">
          <button className={`vbtn ${view === "console" ? "active" : ""}`} onClick={() => setView("console")}>
            Console
          </button>
          <button className={`vbtn ${view === "live" ? "active" : ""}`} onClick={() => setView("live")}>
            Live agent
          </button>
        </nav>
        <div className="spacer" />
        <div className="badges">
          {health ? (
            <>
              <span className={`badge ${health.sdk_available ? "ok" : "warn"}`}>
                engine: {health.default_engine}
              </span>
              <span className="badge">model: {health.model}</span>
              <span className="badge">today: {health.reference_date}</span>
            </>
          ) : (
            <span className="badge warn">backend offline</span>
          )}
          <div className="engine-select">
            <label htmlFor="eng">engine</label>
            <select id="eng" value={engine} onChange={(e) => setEngine(e.target.value)}>
              <option value="auto">auto</option>
              <option value="claude">claude (subscription)</option>
              <option value="mock">mock (offline)</option>
            </select>
          </div>
        </div>
      </header>

      {view === "console" ? (
        <div className="main">
          <ChatWindow
            messages={messages}
            busy={busy}
            onSend={handleSend}
            onNew={handleNew}
            conversationId={conversationId}
          />
          <AdminDashboard lastRunId={lastRunId} refreshTick={refreshTick} />
        </div>
      ) : (
        <div className="main single">
          <LiveAgent engine={engine} />
        </div>
      )}
    </div>
  );
}
