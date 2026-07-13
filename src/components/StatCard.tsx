import { type ReactNode } from "react";

interface StatCardProps {
  icon: ReactNode;
  label: string;
  value: number | string;
  variant?: "default" | "warning" | "danger";
}

/**
 * Example stat card. Replace or extend freely — this is just a starter
 * pattern for "icon + label + big number" widgets you see on most ops
 * dashboards. Server-component safe (no hooks).
 */
export function StatCard({
  icon,
  label,
  value,
  variant = "default",
}: StatCardProps) {
  const accent =
    variant === "warning"
      ? "text-yellow-700 dark:text-yellow-400"
      : variant === "danger"
        ? "text-red-700 dark:text-red-400"
        : "text-foreground";

  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="flex items-center gap-2 text-muted-foreground text-xs uppercase tracking-wider">
        {icon}
        <span>{label}</span>
      </div>
      <p className={`mt-2 text-2xl font-semibold tabular-nums ${accent}`}>
        {value}
      </p>
    </div>
  );
}
