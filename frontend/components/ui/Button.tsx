"use client";

import type { ButtonHTMLAttributes } from "react";

type Variant = "primary" | "secondary";

export function Button({
  variant = "primary",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  const base =
    "inline-flex items-center justify-center gap-2 px-4 py-2 rounded-md text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50";
  const styles =
    variant === "primary"
      ? "bg-primary text-primary-ink hover:bg-primary-hover shadow-sm"
      : "bg-surface-2 text-foreground border border-border hover:bg-border";
  return <button className={`${base} ${styles} ${className}`} {...props} />;
}
