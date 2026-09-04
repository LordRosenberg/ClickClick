/** @type {import('tailwindcss').Config} */
import animate from "tailwindcss-animate";

/**
 * Ops-console dark palette. Components reference these tokens (bg-bg-1,
 * text-neon, border-cyan, etc.) — never the raw hex/HSL values directly.
 */
const tokenHex = (h: string) => h;

export default {
  darkMode: ["class"],
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    container: {
      center: true,
      padding: "2rem",
      screens: { "2xl": "1400px" },
    },
    extend: {
      colors: {
        // Dark ops-console tokens (hex) — primary palette for components.
        "bg-base": tokenHex("#0a0d12"),
        "bg-1": tokenHex("#11151c"),
        "bg-2": tokenHex("#161b25"),
        "bg-3": tokenHex("#1b2230"),
        border: "hsl(var(--border))",
        "border-hi": tokenHex("#2a3344"),
        text: tokenHex("#d8dee9"),
        "text-mute": tokenHex("#7a869a"),
        "text-faint": tokenHex("#4b5563"),
        neon: tokenHex("#5af2c1"),
        cyan: tokenHex("#22d3ee"),
        violet: tokenHex("#a78bfa"),
        amber: tokenHex("#fbbf24"),
        err: tokenHex("#f43f5e"),

        // shadcn HSL contract — keep mapping intact for ui/* primitives.
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        destructive: {
          DEFAULT: "hsl(var(--destructive))",
          foreground: "hsl(var(--destructive-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
        },
        popover: {
          DEFAULT: "hsl(var(--popover))",
          foreground: "hsl(var(--popover-foreground))",
        },
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
      },
      borderRadius: {
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },
      boxShadow: {
        neon: "0 0 12px rgba(90, 242, 193, 0.4)",
        cyan: "0 0 12px rgba(34, 211, 238, 0.4)",
        violet: "0 0 12px rgba(167, 139, 250, 0.4)",
      },
      fontFamily: {
        sans: [
          "Inter",
          "system-ui",
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "sans-serif",
        ],
        mono: [
          "JetBrains Mono",
          "Fira Code",
          "SF Mono",
          "ui-monospace",
          "monospace",
        ],
      },
      keyframes: {
        "pulse-neon": {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.4" },
        },
        "caret-blink": {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0" },
        },
      },
      animation: {
        "pulse-neon": "pulse-neon 1.4s ease-in-out infinite",
        "caret-blink": "caret-blink 1s steps(2) infinite",
      },
    },
  },
  plugins: [animate],
};