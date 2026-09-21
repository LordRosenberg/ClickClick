import { NavLink, Outlet } from "react-router-dom";
import { cn } from "@/lib/utils";

const navItems = [
  { to: "/", label: "任务", end: true },
  { to: "/device", label: "设备", end: false },
  { to: "/skills", label: "技能", end: false },
];

export function RootLayout() {
  return (
    <div className="min-h-screen bg-bg-base text-text">
      <header className="sticky top-0 z-20 border-b border-border bg-bg-base/80 backdrop-blur">
        <div className="mx-auto flex min-h-14 max-w-7xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-2">
          <span className="flex items-center gap-2 font-mono text-sm font-semibold tracking-tight">
            <span className="inline-block h-2 w-2 animate-pulse-neon rounded-full bg-neon shadow-neon" />
            <span className="text-text">CLICK</span>
            <span className="text-cyan">CLICK</span>
            <span className="text-text-mute">/ console</span>
          </span>
          <nav className="flex items-center gap-1">
            {navItems.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  cn(
                    "rounded border px-3 py-1 font-mono text-xs uppercase tracking-wide transition-colors",
                    isActive
                      ? "border-neon/40 bg-neon/10 text-neon shadow-neon"
                      : "border-border bg-transparent text-text-mute hover:border-border-hi hover:bg-bg-2 hover:text-text",
                  )
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-4 py-6">
        <Outlet />
      </main>
    </div>
  );
}
