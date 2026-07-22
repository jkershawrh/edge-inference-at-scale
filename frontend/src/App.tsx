import { Routes, Route, NavLink } from "react-router-dom";
import Dashboard from "./pages/Dashboard";
import Messages from "./pages/Messages";

function App() {
  const navLinkClass = ({ isActive }: { isActive: boolean }) =>
    `flex items-center gap-3 px-4 py-2.5 rounded-lg text-sm font-medium transition-colors ${
      isActive
        ? "bg-blue-600/20 text-blue-400 border border-blue-500/30"
        : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/60"
    }`;

  return (
    <div className="flex h-screen">
      {/* Sidebar */}
      <aside className="w-72 bg-zinc-900/80 border-r border-zinc-800 flex flex-col">
        {/* Header */}
        <div className="p-6 border-b border-zinc-800">
          <div className="flex items-center gap-3 mb-1">
            <div className="w-9 h-9 bg-gradient-to-br from-blue-500 to-cyan-400 rounded-lg flex items-center justify-center">
              <svg
                className="w-5 h-5 text-white"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={2}
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"
                />
              </svg>
            </div>
            <div>
              <h1 className="text-base font-bold text-zinc-100 leading-tight">
                Edge Inference
              </h1>
              <p className="text-[11px] text-zinc-500 font-medium tracking-wide uppercase">
                at Scale
              </p>
            </div>
          </div>
          <p className="text-xs text-zinc-500 mt-3 pl-0.5">
            Summit Connect Demo
          </p>
        </div>

        {/* Navigation */}
        <nav className="flex-1 p-4 space-y-1.5">
          <NavLink to="/" end className={navLinkClass}>
            <svg
              className="w-4 h-4"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M4 5a1 1 0 011-1h14a1 1 0 011 1v2a1 1 0 01-1 1H5a1 1 0 01-1-1V5zM4 13a1 1 0 011-1h6a1 1 0 011 1v6a1 1 0 01-1 1H5a1 1 0 01-1-1v-6zM16 13a1 1 0 011-1h2a1 1 0 011 1v6a1 1 0 01-1 1h-2a1 1 0 01-1-1v-6z"
              />
            </svg>
            Dashboard
          </NavLink>
          <NavLink to="/messages" className={navLinkClass}>
            <svg
              className="w-4 h-4"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z"
              />
            </svg>
            Messages
          </NavLink>
        </nav>

        {/* Footer */}
        <div className="p-4 border-t border-zinc-800 space-y-2">
          <div className="flex items-center gap-2 text-[11px] text-zinc-500">
            <span className="inline-block w-2 h-2 rounded-full bg-red-500" />
            <span>Red Hat</span>
            <span className="text-zinc-700">+</span>
            <span className="inline-block w-2 h-2 rounded-full bg-blue-500" />
            <span>Intel</span>
          </div>
          <p className="text-[10px] text-zinc-600 font-mono leading-relaxed">
            BitNet 1.58-bit | CPU-only Edge AI
          </p>
        </div>
      </aside>

      {/* Main Content */}
      <main className="flex-1 overflow-y-auto bg-zinc-950">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/messages" element={<Messages />} />
        </Routes>
      </main>
    </div>
  );
}

export default App;
