import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronRight, History, MessageCircle, Minus, Plus, Send, X } from "lucide-react";
import { api } from "../lib/api.js";
import { runReportRoute } from "../lib/auth.js";
import { CHAT_CONVERSATION_KEY, CHAT_MAX_INPUT, awaitingReport, latestSuggestions, pendingJob, reportLabel, safeExternalUrl } from "../lib/chatModel.js";
import { companyByTicker } from "../lib/reportModel.js";

function readKey(key) {
  try {
    return window.localStorage.getItem(key) || "";
  } catch {
    return "";
  }
}

function writeKey(key, value) {
  try {
    if (value) window.localStorage.setItem(key, value);
    else window.localStorage.removeItem(key);
  } catch {
    // storage unavailable: history is still on the server
  }
}

const isSec = (ticker) => Boolean(companyByTicker(ticker));

/**
 * Company-research assistant. Floating on Overview; with `inline` and `scope`
 * ({ ticker, run_id, version }) it is a compact, collapsible chat inside the
 * Executive Summary, bound server-side to that exact report and summary
 * version. Same backend, guardrails and persistence; everything is rendered
 * as plain text (no HTML).
 */
export function ChatWidget({ token, navigate, onAuthError, scope = null, inline = false }) {
  const storageKey = scope ? `${CHAT_CONVERSATION_KEY}:${scope.run_id}:${scope.version || ""}` : CHAT_CONVERSATION_KEY;
  const scopeBody = scope ? { ticker: scope.ticker, run_id: scope.run_id } : undefined;
  const [open, setOpen] = useState(false);
  const [conversation, setConversation] = useState(null);
  const [messages, setMessages] = useState([]);
  const [history, setHistory] = useState(null);
  const [showHistory, setShowHistory] = useState(false);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [job, setJob] = useState(null);
  const listRef = useRef(null);

  const fail = useCallback(
    (err) => {
      if (err?.status === 401) onAuthError?.();
      setError(err?.message || "The research assistant is unavailable.");
    },
    [onAuthError]
  );

  const load = useCallback(
    async (id) => {
      setError("");
      try {
        let body = id ? await api.chatMessages(token, id) : await api.newChat(token, scopeBody);
        // A remembered chat must belong to this exact report (or to Overview).
        if (id && (body.scope_run_id || null) !== (scope?.run_id || null)) body = await api.newChat(token, scopeBody);
        setConversation({ id: body.id, ticker: body.ticker, title: body.title });
        setMessages(body.messages || []);
        writeKey(storageKey, body.id);
      } catch (err) {
        if (id && err?.status === 404) return load(null);
        fail(err);
      }
      return undefined;
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [token, fail, storageKey]
  );

  useEffect(() => {
    if (open && !conversation) load(readKey(storageKey) || null);
  }, [open, conversation, load, storageKey]);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight });
  }, [messages, busy, open]);

  // On-demand report: poll the conversation until the server posts the result.
  const generating = awaitingReport(messages);
  useEffect(() => {
    if (!generating || !conversation?.id) return undefined;
    const timer = window.setInterval(async () => {
      try {
        const body = await api.chatMessages(token, conversation.id);
        setMessages((current) => (body.messages?.length > current.length ? body.messages : current));
      } catch (err) {
        if (err?.status === 401) onAuthError?.();
      }
    }, 4000);
    return () => window.clearInterval(timer);
  }, [generating, conversation?.id, token, onAuthError]);

  // Genuine research progress for a run started (or already running) from chat.
  useEffect(() => {
    const target = pendingJob(messages);
    setJob(target);
    if (!target) return undefined;
    let stopped = false;
    let timer;
    const poll = async () => {
      try {
        const status = await api.job(token, target.run_id);
        if (stopped) return;
        setJob({ ...target, ...status });
        if (status.status === "completed" || status.status === "failed") return;
      } catch (err) {
        if (err?.status === 401) return onAuthError?.();
      }
      if (!stopped) timer = window.setTimeout(poll, 4000);
      return undefined;
    };
    poll();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [messages, token, onAuthError]);

  async function send(body) {
    if (busy || !conversation) return;
    setBusy(true);
    setError("");
    const optimistic = body.text ? [{ id: `local-${Date.now()}`, role: "user", content: body.text, payload: {} }] : [];
    setMessages((m) => [...m, ...optimistic]);
    try {
      const result = await api.sendChat(token, conversation.id, body);
      setMessages((m) => [
        ...m.filter((x) => !optimistic.includes(x)),
        ...(result.user_message ? [result.user_message] : []),
        result.message,
      ]);
      if (result.message?.ticker) setConversation((c) => ({ ...c, ticker: result.message.ticker }));
    } catch (err) {
      setMessages((m) => m.filter((x) => !optimistic.includes(x)));
      fail(err);
    } finally {
      setBusy(false);
    }
  }

  function submit(event) {
    event.preventDefault();
    const text = input.trim();
    if (!text) return;
    setInput("");
    send({ text });
  }

  async function openHistory() {
    setShowHistory((v) => !v);
    try {
      const body = await api.chatConversations(token, undefined, scope?.run_id);
      setHistory(body.conversations || []);
    } catch (err) {
      fail(err);
    }
  }

  async function downloadPdf(download) {
    try {
      const { blob, filename } = await api.downloadReportPdf(token, download.ticker, download.run_id);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (err) {
      fail(err);
    }
  }

  const openReport = (report) => {
    const route = runReportRoute({ ...report, report_available: true }, isSec);
    if (route && navigate) navigate(route);
  };

  if (inline && !open) {
    return (
      <div className="chat-inline">
        <button type="button" className="chat-inline-toggle" onClick={() => setOpen(true)} aria-expanded="false">
          <span>
            <MessageCircle size={15} aria-hidden="true" /> Ask about this executive summary
          </span>
          <ChevronRight size={15} aria-hidden="true" />
        </button>
      </div>
    );
  }

  if (!open) {
    return (
      <button type="button" className="chat-fab" onClick={() => setOpen(true)} aria-label="Open research assistant" title="Research assistant">
        <MessageCircle size={22} aria-hidden="true" />
      </button>
    );
  }

  const suggestions = busy ? [] : latestSuggestions(messages);

  const panel = (
    <section
      className={inline ? "chat-window card chat-window-inline" : "chat-window card"}
      role={inline ? "region" : "dialog"}
      aria-label={inline ? "Executive summary assistant" : "InsightIQ research assistant"}
    >
      <header className="chat-head">
        <div className="chat-title">
          <strong>{inline ? "Summary assistant" : "Research assistant"}</strong>
          <span className="small muted">{conversation?.ticker ? conversation.ticker : "Company research"}</span>
        </div>
        <div className="chat-head-actions">
          <button type="button" className="icon-btn icon-btn-sm" onClick={openHistory} aria-label="Chat history" title="Chat history" aria-expanded={showHistory}>
            <History size={15} />
          </button>
          <button
            type="button"
            className="icon-btn icon-btn-sm"
            onClick={() => {
              setShowHistory(false);
              load(null);
            }}
            aria-label="New chat"
            title="New chat"
          >
            <Plus size={15} />
          </button>
          <button type="button" className="icon-btn icon-btn-sm" onClick={() => setOpen(false)} aria-label="Minimize" title="Minimize">
            <Minus size={15} />
          </button>
          <button
            type="button"
            className="icon-btn icon-btn-sm"
            onClick={() => {
              setOpen(false);
              setShowHistory(false);
              setConversation(null);
              setMessages([]);
              writeKey(storageKey, "");
            }}
            aria-label="Close"
            title="Close (history is kept)"
          >
            <X size={15} />
          </button>
        </div>
      </header>

      {showHistory && (
        <div className="chat-history">
          {history === null ? (
            <p className="small muted">Loading…</p>
          ) : !history.length ? (
            <p className="small muted">No previous chats.</p>
          ) : (
            <ul>
              {history.map((c) => (
                <li key={c.id}>
                  <button
                    type="button"
                    className={c.id === conversation?.id ? "is-active" : ""}
                    onClick={() => {
                      setShowHistory(false);
                      load(c.id);
                    }}
                  >
                    <span>{c.title}</span>
                    <span className="small muted">{String(c.updated_at).slice(0, 10)}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      <div className="chat-messages" ref={listRef} aria-live="polite">
        {messages.map((m) => (
          <div key={m.id} className={`chat-msg chat-${m.role}`}>
            <p>{m.content}</p>
            {m.role === "assistant" && <MessageExtras payload={m.payload || {}} onReport={openReport} onSend={send} navigate={navigate} busy={busy} onDownload={downloadPdf} />}
          </div>
        ))}
        {job && (
          <div className="chat-msg chat-assistant chat-job" role="status">
            <p>
              Research {job.ticker}: <strong>{job.status}</strong>
              {job.agent_statuses ? ` · financial ${job.agent_statuses.financial || "…"}, news ${job.agent_statuses.news || "…"}, strategic ${job.agent_statuses.strategic || "…"}` : ""}
            </p>
            {job.status === "completed" && (
              <button type="button" className="btn btn-sm" onClick={() => openReport({ run_id: job.run_id, ticker: job.ticker })}>
                View report
              </button>
            )}
          </div>
        )}
        {generating && !busy && (
          <div className="chat-msg chat-assistant chat-job" role="status">
            <p>Generating report for {generating}…</p>
          </div>
        )}
        {busy && (
          <div className="chat-msg chat-assistant chat-typing" aria-label="Assistant is responding">
            <span />
            <span />
            <span />
          </div>
        )}
        {error && (
          <p className="field-error small" role="alert">
            {error}
          </p>
        )}
      </div>

      {suggestions.length > 0 && (
        <div className="chat-suggestions">
          {suggestions.map((s) => (
            <button key={s} type="button" onClick={() => send({ text: s })} disabled={busy}>
              {s}
            </button>
          ))}
        </div>
      )}

      <form className="chat-input" onSubmit={submit}>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          maxLength={CHAT_MAX_INPUT}
          placeholder="Ask about a company…"
          aria-label="Message"
          disabled={!conversation}
        />
        <button type="submit" className="btn btn-primary btn-sm" disabled={busy || !input.trim() || !conversation} aria-label="Send">
          <Send size={15} />
        </button>
      </form>
    </section>
  );
  if (!inline) return panel;
  return (
    <div className="chat-inline">
      <button type="button" className="chat-inline-toggle" onClick={() => setOpen(false)} aria-expanded="true">
        <span>
          <MessageCircle size={15} aria-hidden="true" /> Ask about this executive summary
        </span>
        <ChevronDown size={15} aria-hidden="true" />
      </button>
      {panel}
    </div>
  );
}

function MessageExtras({ payload, onReport, onSend, navigate, busy, onDownload }) {
  const actions = payload.actions || [];
  const reports = payload.reports || [];
  const sources = payload.sources || [];
  const notices = payload.notices || [];
  return (
    <>
      {notices.length > 0 && <p className="small muted">{notices.join(" ")}</p>}
      {reports.length > 0 && (
        <div className="chat-chips">
          {reports.map((r) => (
            <button key={r.run_id} type="button" className="btn btn-sm" onClick={() => onReport(r)}>
              {reportLabel(r)}
            </button>
          ))}
          {payload.download && (
            <button type="button" className="btn btn-sm btn-primary" onClick={() => onDownload(payload.download)}>
              Download PDF
            </button>
          )}
        </div>
      )}
      {actions.length > 0 && (
        <div className="chat-chips">
          {actions.map((a, i) =>
            a.type === "open_company" ? (
              <button key={i} type="button" className="btn btn-sm" onClick={() => navigate({ page: "market-company", market: a.market, symbol: a.symbol })}>
                {a.label}
              </button>
            ) : (
              <button
                key={i}
                type="button"
                className={`btn btn-sm ${a.type === "confirm_research" ? "btn-primary" : ""}`}
                disabled={busy}
                onClick={() => onSend({ action: { type: a.type, ticker: a.ticker || undefined, nonce: a.nonce || undefined } })}
              >
                {a.label}
              </button>
            )
          )}
        </div>
      )}
      {sources.length > 0 && (
        <ul className="chat-sources small muted">
          {sources.map((s) => {
            const url = safeExternalUrl(s.url);
            return (
              <li key={s.id}>
                [{s.id}] {url ? (
                  <a href={url} target="_blank" rel="noopener noreferrer nofollow">
                    {s.label}
                  </a>
                ) : (
                  s.label
                )}
                {s.date ? ` · ${String(s.date).slice(0, 10)}` : ""}
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}
