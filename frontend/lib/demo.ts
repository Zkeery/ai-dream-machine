export const READ_ONLY_DEMO = process.env.NEXT_PUBLIC_READ_ONLY_DEMO === "1";
export const DEMO_MESSAGE = "演示版只开放示例作品";

export function showDemoNotice() {
  if (typeof window !== "undefined") window.dispatchEvent(new Event("dream-demo-notice"));
}

export function guardDemoWrite(method = "GET") {
  if (READ_ONLY_DEMO && !["GET", "HEAD"].includes(method.toUpperCase())) {
    showDemoNotice();
    throw new Error(DEMO_MESSAGE);
  }
}
