import { useState, useEffect, useRef, useCallback } from "react";
import axios from "axios";

const API_BASE = import.meta.env.VITE_API_URL || "";

interface Message {
  id?: string;
  sender: string;
  receiver: string;
  content: string;
  timestamp?: string;
  direction?: "inbound" | "outbound";
  model?: string;
  latency_ms?: number;
  provider?: string;
}

export default function Messages() {
  const [phoneNumber, setPhoneNumber] = useState("+1234567890");
  const [message, setMessage] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, []);

  const fetchHistory = useCallback(async () => {
    try {
      const res = await axios.get(`${API_BASE}/api/sms/history`, {
        timeout: 5000,
      });
      const data = Array.isArray(res.data) ? res.data : res.data.messages || [];
      setMessages(data);
    } catch {
      // History endpoint unavailable
    }
  }, []);

  useEffect(() => {
    fetchHistory();
    pollRef.current = setInterval(fetchHistory, 2000);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [fetchHistory]);

  useEffect(() => {
    scrollToBottom();
  }, [messages, scrollToBottom]);

  const handleSend = async () => {
    if (!message.trim() || sending) return;

    setError(null);
    setSending(true);

    try {
      await axios.post(`${API_BASE}/api/sms/receive`, {
        sender: phoneNumber,
        receiver: "+1000000000",
        content: message,
      });
      setMessage("");
      // Immediate fetch to show the sent message
      await fetchHistory();
    } catch (err) {
      const errorMsg =
        axios.isAxiosError(err) && err.response?.data?.detail
          ? err.response.data.detail
          : "Failed to send message. Is the backend running?";
      setError(typeof errorMsg === "string" ? errorMsg : "Failed to send message.");
    } finally {
      setSending(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const isSent = (msg: Message) =>
    msg.direction === "inbound" || msg.sender === phoneNumber;

  const formatTime = (ts?: string) => {
    if (!ts) return "";
    try {
      return new Date(ts).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch {
      return "";
    }
  };

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="px-8 py-5 border-b border-zinc-800 bg-zinc-900/40">
        <h1 className="text-xl font-bold text-zinc-100">SMS Simulator</h1>
        <p className="text-sm text-zinc-500 mt-0.5">
          Send a text message and get an AI-powered response via BitNet
        </p>
      </div>

      {/* Messages Area */}
      <div className="flex-1 overflow-y-auto px-8 py-6 space-y-4">
        {messages.length === 0 && (
          <div className="flex flex-col items-center justify-center h-full text-center">
            <div className="w-16 h-16 rounded-full bg-zinc-800/60 flex items-center justify-center mb-4">
              <svg
                className="w-8 h-8 text-zinc-600"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={1.5}
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z"
                />
              </svg>
            </div>
            <p className="text-zinc-500 text-sm">No messages yet</p>
            <p className="text-zinc-600 text-xs mt-1">
              Send a message below to start the conversation
            </p>
          </div>
        )}

        {messages.map((msg, idx) => {
          const sent = isSent(msg);
          return (
            <div
              key={msg.id || idx}
              className={`flex ${sent ? "justify-end" : "justify-start"}`}
            >
              <div
                className={`max-w-md rounded-2xl px-4 py-3 ${
                  sent
                    ? "bg-blue-600 text-white rounded-br-md"
                    : "bg-zinc-800 text-zinc-200 rounded-bl-md border border-zinc-700/50"
                }`}
              >
                <p className="text-sm whitespace-pre-wrap break-words">
                  {msg.content}
                </p>
                <div
                  className={`flex items-center gap-2 mt-1.5 text-[10px] ${
                    sent ? "text-blue-200/70 justify-end" : "text-zinc-500"
                  }`}
                >
                  {formatTime(msg.timestamp) && (
                    <span>{formatTime(msg.timestamp)}</span>
                  )}
                  {!sent && msg.model && (
                    <span className="font-mono">{msg.model}</span>
                  )}
                  {!sent && msg.latency_ms != null && (
                    <span className="font-mono">{msg.latency_ms}ms</span>
                  )}
                  {!sent && msg.provider && (
                    <span className="font-mono">{msg.provider}</span>
                  )}
                </div>
              </div>
            </div>
          );
        })}
        <div ref={messagesEndRef} />
      </div>

      {/* Error */}
      {error && (
        <div className="mx-8 mb-2 px-4 py-2 bg-red-900/30 border border-red-800/50 rounded-lg text-red-300 text-sm">
          {error}
        </div>
      )}

      {/* Input Area */}
      <div className="px-8 py-4 border-t border-zinc-800 bg-zinc-900/40">
        <div className="flex items-center gap-3 mb-3">
          <label className="text-xs text-zinc-500 font-medium whitespace-nowrap">
            From:
          </label>
          <input
            type="text"
            value={phoneNumber}
            onChange={(e) => setPhoneNumber(e.target.value)}
            className="bg-zinc-800/60 border border-zinc-700 rounded-lg px-3 py-1.5 text-sm text-zinc-200 font-mono w-44 focus:outline-none focus:border-blue-500/50 focus:ring-1 focus:ring-blue-500/20"
          />
          <span className="text-xs text-zinc-600 font-mono">
            &rarr; +1000000000
          </span>
        </div>
        <div className="flex gap-3">
          <div className="flex-1 relative">
            <textarea
              value={message}
              onChange={(e) =>
                setMessage(e.target.value.slice(0, 160))
              }
              onKeyDown={handleKeyDown}
              placeholder="Type your SMS message..."
              maxLength={160}
              rows={2}
              className="w-full bg-zinc-800/60 border border-zinc-700 rounded-xl px-4 py-3 text-sm text-zinc-200 placeholder-zinc-600 resize-none focus:outline-none focus:border-blue-500/50 focus:ring-1 focus:ring-blue-500/20"
            />
            <span
              className={`absolute bottom-2 right-3 text-[10px] font-mono ${
                message.length >= 150 ? "text-amber-400" : "text-zinc-600"
              }`}
            >
              {message.length}/160
            </span>
          </div>
          <button
            onClick={handleSend}
            disabled={!message.trim() || sending}
            className="px-6 py-3 bg-blue-600 hover:bg-blue-500 disabled:bg-zinc-700 disabled:text-zinc-500 text-white text-sm font-medium rounded-xl transition-colors self-end"
          >
            {sending ? (
              <span className="flex items-center gap-2">
                <svg
                  className="animate-spin w-4 h-4"
                  fill="none"
                  viewBox="0 0 24 24"
                >
                  <circle
                    className="opacity-25"
                    cx="12"
                    cy="12"
                    r="10"
                    stroke="currentColor"
                    strokeWidth="4"
                  />
                  <path
                    className="opacity-75"
                    fill="currentColor"
                    d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
                  />
                </svg>
                Sending
              </span>
            ) : (
              "Send SMS"
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
