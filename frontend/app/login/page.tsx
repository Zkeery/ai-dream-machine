"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Clapperboard } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { useAuth } from "@/features/auth/AuthProvider";

export default function LoginPage() {
  const { login } = useAuth();
  const router = useRouter();
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const c = code.trim();
    if (!c) {
      setError("请输入邀请码");
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      await login(c);
      router.replace("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "登录失败");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="flex-1 flex items-center justify-center p-6">
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-sm bg-surface border border-border rounded-2xl shadow-lg p-8 space-y-5"
      >
        <div className="text-center">
          <div className="mx-auto w-12 h-12 rounded-xl bg-gradient-to-br from-primary to-accent flex items-center justify-center mb-3">
            <Clapperboard className="text-white" size={24} />
          </div>
          <h1 className="text-xl font-bold text-foreground">AI造梦机</h1>
          <p className="text-sm text-muted mt-1">输入邀请码登录</p>
        </div>

        <div>
          <label htmlFor="invite" className="block text-sm text-muted mb-1.5">
            邀请码
          </label>
          <Input
            id="invite"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder="8 位邀请码"
            autoFocus
          />
        </div>

        {error && <p className="text-sm text-danger">{error}</p>}

        <Button type="submit" disabled={submitting} className="w-full py-2.5">
          {submitting ? "登录中…" : "登录"}
        </Button>
      </form>
    </main>
  );
}
