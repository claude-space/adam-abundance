import type { Config } from "tailwindcss";

const config: Config = {
  // Class-based dark mode — toggle via adding/removing `dark` to html/body.
  // Matches SHA's own dark mode story so user-facing dashboards
  // automatically follow the platform's theme.
  darkMode: ["class"],
  content: ["./src/**/*.{ts,tsx,mdx}"],
  theme: {
    extend: {
      colors: {
        // CSS-var-driven palette so user can flip dark/light without
        // re-defining every shade. Matches Tailwind's "with CSS vars"
        // recommendation.
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        card: "hsl(var(--card))",
        muted: "hsl(var(--muted))",
        "muted-foreground": "hsl(var(--muted-foreground))",
        border: "hsl(var(--border))",
        primary: "hsl(var(--primary))",
        "primary-foreground": "hsl(var(--primary-foreground))",
      },
      fontFamily: {
        sans: ["var(--font-sans)", "system-ui", "sans-serif"],
        mono: ["var(--font-mono)", "ui-monospace", "monospace"],
      },
    },
  },
  plugins: [],
};

export default config;
