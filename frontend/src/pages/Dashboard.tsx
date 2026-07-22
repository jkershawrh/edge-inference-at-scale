import { useEffect, useState } from "react";
import axios from "axios";

const API_BASE = import.meta.env.VITE_API_URL || "";

interface ServiceStatus {
  name: string;
  status: "healthy" | "unhealthy" | "loading";
  detail?: string;
}

interface Statistics {
  total_messages: number;
  successful: number;
  failed: number;
}

function StatusDot({ status }: { status: string }) {
  const color =
    status === "healthy"
      ? "bg-emerald-400 shadow-emerald-400/50"
      : status === "unhealthy"
        ? "bg-red-400 shadow-red-400/50"
        : "bg-zinc-500 animate-pulse";
  return <span className={`inline-block w-2.5 h-2.5 rounded-full shadow-lg ${color}`} />;
}

function StatusCard({ service }: { service: ServiceStatus }) {
  return (
    <div className="bg-zinc-900/60 border border-zinc-800 rounded-xl p-4 flex items-center justify-between">
      <div>
        <p className="text-sm font-medium text-zinc-300">{service.name}</p>
        {service.detail && (
          <p className="text-xs text-zinc-500 mt-0.5">{service.detail}</p>
        )}
      </div>
      <div className="flex items-center gap-2">
        <span
          className={`text-xs font-medium ${
            service.status === "healthy"
              ? "text-emerald-400"
              : service.status === "unhealthy"
                ? "text-red-400"
                : "text-zinc-500"
          }`}
        >
          {service.status === "loading"
            ? "Checking..."
            : service.status === "healthy"
              ? "Online"
              : "Offline"}
        </span>
        <StatusDot status={service.status} />
      </div>
    </div>
  );
}

function StatCard({
  label,
  value,
  color,
}: {
  label: string;
  value: number;
  color: string;
}) {
  return (
    <div className="bg-zinc-900/60 border border-zinc-800 rounded-xl p-5 text-center">
      <p className={`text-3xl font-bold ${color}`}>{value}</p>
      <p className="text-xs text-zinc-500 mt-1 uppercase tracking-wider font-medium">
        {label}
      </p>
    </div>
  );
}

export default function Dashboard() {
  const [services, setServices] = useState<ServiceStatus[]>([
    { name: "SMS Gateway", status: "loading" },
    { name: "Message Router", status: "loading" },
    { name: "LLM Inference", status: "loading" },
    { name: "RAG Service", status: "loading" },
    { name: "Privacy Filter", status: "loading" },
  ]);
  const [stats, setStats] = useState<Statistics>({
    total_messages: 0,
    successful: 0,
    failed: 0,
  });

  useEffect(() => {
    const healthChecks: { name: string; url: string }[] = [
      { name: "SMS Gateway", url: `${API_BASE}/api/sms/health` },
      { name: "Message Router", url: `${API_BASE}/api/router/health` },
      { name: "LLM Inference", url: `${API_BASE}/api/llm/health` },
      { name: "RAG Service", url: `${API_BASE}/api/rag/health` },
      { name: "Privacy Filter", url: `${API_BASE}/api/privacy/health` },
    ];

    async function checkServices() {
      const results = await Promise.all(
        healthChecks.map(async (svc) => {
          try {
            const res = await axios.get(svc.url, { timeout: 5000 });
            return {
              name: svc.name,
              status: "healthy" as const,
              detail: res.data?.version || res.data?.status || undefined,
            };
          } catch {
            return { name: svc.name, status: "unhealthy" as const };
          }
        })
      );
      setServices(results);
    }

    async function fetchStats() {
      try {
        const res = await axios.get(`${API_BASE}/api/router/statistics`, {
          timeout: 5000,
        });
        setStats({
          total_messages: res.data.total_messages ?? 0,
          successful: res.data.successful ?? 0,
          failed: res.data.failed ?? 0,
        });
      } catch {
        // Stats unavailable
      }
    }

    checkServices();
    fetchStats();

    const interval = setInterval(() => {
      checkServices();
      fetchStats();
    }, 10000);

    return () => clearInterval(interval);
  }, []);

  const healthyCount = services.filter((s) => s.status === "healthy").length;

  return (
    <div className="p-8 max-w-6xl mx-auto space-y-8">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-zinc-100">System Dashboard</h1>
        <p className="text-sm text-zinc-500 mt-1">
          Real-time status of Edge Inference services
        </p>
      </div>

      {/* Tagline Banner */}
      <div className="bg-gradient-to-r from-blue-600/10 via-cyan-600/10 to-blue-600/10 border border-blue-500/20 rounded-xl p-6">
        <p className="text-lg font-semibold text-blue-300">
          AI at the edge. Text in, knowledge out. Even on 2G.
        </p>
        <p className="text-sm text-zinc-400 mt-2 font-mono">
          SMS &rarr; LLM &rarr; SMS &nbsp;|&nbsp; BitNet b1.58-2B-4T &nbsp;|&nbsp; Edge Node
        </p>
      </div>

      {/* Service Status */}
      <section>
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-zinc-200">
            Service Health
          </h2>
          <span className="text-xs text-zinc-500 font-mono">
            {healthyCount}/{services.length} online
          </span>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {services.map((svc) => (
            <StatusCard key={svc.name} service={svc} />
          ))}
        </div>
      </section>

      {/* Model Info + Stats Row */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* BitNet Model Card */}
        <section className="bg-zinc-900/60 border border-zinc-800 rounded-xl p-6">
          <h2 className="text-lg font-semibold text-zinc-200 mb-4">
            BitNet Model
          </h2>
          <div className="space-y-3">
            <div className="flex justify-between text-sm">
              <span className="text-zinc-500">Model</span>
              <span className="text-zinc-200 font-mono text-xs">
                BitNet b1.58-2B-4T
              </span>
            </div>
            <div className="flex justify-between text-sm">
              <span className="text-zinc-500">Memory</span>
              <span className="text-zinc-200 font-mono text-xs">~410 MB</span>
            </div>
            <div className="flex justify-between text-sm">
              <span className="text-zinc-500">Provider</span>
              <span className="text-zinc-200 font-mono text-xs">bitnet</span>
            </div>
            <div className="flex justify-between text-sm">
              <span className="text-zinc-500">Quantization</span>
              <span className="text-zinc-200 font-mono text-xs">
                1.58-bit ternary
              </span>
            </div>
            <div className="flex justify-between text-sm">
              <span className="text-zinc-500">Hardware</span>
              <span className="text-zinc-200 font-mono text-xs">
                CPU-only (no GPU required)
              </span>
            </div>
          </div>
        </section>

        {/* Message Statistics */}
        <section>
          <h2 className="text-lg font-semibold text-zinc-200 mb-4">
            Message Statistics
          </h2>
          <div className="grid grid-cols-3 gap-3">
            <StatCard
              label="Total"
              value={stats.total_messages}
              color="text-zinc-100"
            />
            <StatCard
              label="Successful"
              value={stats.successful}
              color="text-emerald-400"
            />
            <StatCard
              label="Failed"
              value={stats.failed}
              color="text-red-400"
            />
          </div>
        </section>
      </div>

      {/* Architecture */}
      <section className="bg-zinc-900/60 border border-zinc-800 rounded-xl p-6">
        <h2 className="text-lg font-semibold text-zinc-200 mb-4">
          Architecture
        </h2>
        <div className="flex items-center justify-center gap-3 text-sm font-mono flex-wrap">
          <span className="px-3 py-1.5 bg-zinc-800 rounded-lg text-zinc-300 border border-zinc-700">
            SMS In
          </span>
          <span className="text-zinc-600">&rarr;</span>
          <span className="px-3 py-1.5 bg-zinc-800 rounded-lg text-zinc-300 border border-zinc-700">
            Privacy Filter
          </span>
          <span className="text-zinc-600">&rarr;</span>
          <span className="px-3 py-1.5 bg-zinc-800 rounded-lg text-zinc-300 border border-zinc-700">
            RAG + Context
          </span>
          <span className="text-zinc-600">&rarr;</span>
          <span className="px-3 py-1.5 bg-blue-900/40 rounded-lg text-blue-300 border border-blue-700/50">
            BitNet LLM
          </span>
          <span className="text-zinc-600">&rarr;</span>
          <span className="px-3 py-1.5 bg-zinc-800 rounded-lg text-zinc-300 border border-zinc-700">
            SMS Out
          </span>
        </div>
        <p className="text-center text-xs text-zinc-600 mt-4">
          All processing happens on-device at the edge -- no cloud GPU required
        </p>
      </section>
    </div>
  );
}
