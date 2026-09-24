import { Suspense } from "react";
import { Workspace } from "@/features/workspace/Workspace";

export default function HomePage() {
  return (
    <Suspense
      fallback={
        <main className="flex-1 flex items-center justify-center text-muted">
          加载中…
        </main>
      }
    >
      <Workspace />
    </Suspense>
  );
}
