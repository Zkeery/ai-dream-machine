import { Suspense } from "react";
import { Workspace } from "@/features/workspace/Workspace";
import { DemoWorkspace } from "@/features/demo/DemoWorkspace";
import { READ_ONLY_DEMO } from "@/lib/demo";

export default function HomePage() {
  return (
    <Suspense
      fallback={
        <main className="flex-1 flex items-center justify-center text-muted">
          加载中…
        </main>
      }
    >
      {READ_ONLY_DEMO ? <DemoWorkspace /> : <Workspace />}
    </Suspense>
  );
}
