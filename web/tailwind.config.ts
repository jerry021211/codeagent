import type { Config } from "tailwindcss";

export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        canvas: "rgb(var(--canvas) / <alpha-value>)",
        surface: "rgb(var(--surface) / <alpha-value>)",
        "surface-muted": "rgb(var(--surface-muted) / <alpha-value>)",
        "surface-strong": "rgb(var(--surface-strong) / <alpha-value>)",
        ink: "rgb(var(--ink) / <alpha-value>)",
        "ink-muted": "rgb(var(--ink-muted) / <alpha-value>)",
        "ink-faint": "rgb(var(--ink-faint) / <alpha-value>)",
        line: "rgb(var(--line) / <alpha-value>)",
        "line-strong": "rgb(var(--line-strong) / <alpha-value>)",
        accent: "rgb(var(--accent) / <alpha-value>)",
        "accent-strong": "rgb(var(--accent-strong) / <alpha-value>)",
        success: "rgb(var(--success) / <alpha-value>)",
        warning: "rgb(var(--warning) / <alpha-value>)",
        danger: "rgb(var(--danger) / <alpha-value>)",
        sidebar: "rgb(var(--sidebar) / <alpha-value>)",
        "sidebar-ink": "rgb(var(--sidebar-ink) / <alpha-value>)",
        "sidebar-muted": "rgb(var(--sidebar-muted) / <alpha-value>)",
        code: "rgb(var(--code) / <alpha-value>)",
        "code-ink": "rgb(var(--code-ink) / <alpha-value>)",
        "user-bubble": "rgb(var(--user-bubble) / <alpha-value>)",
        "user-bubble-ink": "rgb(var(--user-bubble-ink) / <alpha-value>)",
        cockpit: {
          950: "#080b12",
          900: "#0d121d",
          850: "#111827",
          800: "#182131",
        },
      },
      boxShadow: {
        panel: "0 18px 50px rgba(0, 0, 0, 0.16)",
      },
      animation: {
        "soft-pulse": "soft-pulse 2.2s ease-in-out infinite",
      },
      keyframes: {
        "soft-pulse": {
          "0%, 100%": { opacity: "0.55", transform: "scale(0.92)" },
          "50%": { opacity: "1", transform: "scale(1)" },
        },
      },
    },
  },
  plugins: [],
} satisfies Config;
