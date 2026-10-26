import type { Metadata } from "next";
import { AuthProvider } from "@/features/auth/AuthProvider";
import { TrialProvider } from "@/features/trial/TrialProvider";
import "./globals.css";
import "./cinema.css";
import "./demo-presentation.css";

export const metadata: Metadata = {
  title: "AI造梦机",
  description: "从一句创意到一部完整成片的 AI 短片创作系统",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN" className="h-full antialiased">
      <body className="min-h-full flex flex-col" data-recording-demo={process.env.NEXT_PUBLIC_RECORDING_DEMO === "1" ? "true" : undefined}>
        <AuthProvider><TrialProvider>{children}</TrialProvider></AuthProvider>
      </body>
    </html>
  );
}
