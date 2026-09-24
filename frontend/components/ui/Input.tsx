"use client";

import type { InputHTMLAttributes } from "react";

export function Input({
  className = "",
  ...props
}: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      className={`w-full px-3 py-2 rounded-md bg-surface-2 border border-border text-foreground text-sm placeholder:text-muted focus:outline-none focus:border-primary ${className}`}
      {...props}
    />
  );
}
