"use client";

import Image from "next/image";
import { useEffect, useRef, useSyncExternalStore, type RefObject } from "react";
import { createCinemaScene, type CinemaBackground, type CinemaSceneController, type SceneMode } from "./cinema-scene";

const mediaQuery = "(prefers-reduced-motion: reduce)";
function subscribeReducedMotion(callback: () => void) {
  const query = matchMedia(mediaQuery); query.addEventListener("change", callback);
  return () => query.removeEventListener("change", callback);
}
export function useReducedMotion() {
  return useSyncExternalStore(subscribeReducedMotion, () => matchMedia(mediaQuery).matches, () => false);
}
function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new window.Image(); image.onload = () => resolve(image); image.onerror = reject; image.src = src;
  });
}

export function CinemaScene({ host, background, paused, mode, onMode }: {
  host: RefObject<HTMLElement | null>;
  background: CinemaBackground;
  paused: boolean;
  mode: SceneMode | "loading";
  onMode: (mode: SceneMode) => void;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const controller = useRef<CinemaSceneController | null>(null);
  const reducedMotion = useReducedMotion();
  const settings = useRef({ background, paused: paused || reducedMotion });

  useEffect(() => {
    settings.current = { background, paused: paused || reducedMotion };
    controller.current?.setBackground(background); controller.current?.setPaused(settings.current.paused);
  }, [background, paused, reducedMotion]);

  useEffect(() => {
    let cancelled = false;
    Promise.all([loadImage("/cinema/portal.png"), loadImage("/cinema/world.png")]).then(([portal, world]) => {
      if (cancelled || !canvas.current || !host.current) return;
      try {
        controller.current = createCinemaScene({ canvas: canvas.current, host: host.current, images: { portal, world }, onMode });
        controller.current?.setBackground(settings.current.background); controller.current?.setPaused(settings.current.paused);
      } catch { onMode("fallback"); }
    }).catch(() => { if (!cancelled) onMode("fallback"); });
    return () => { cancelled = true; controller.current?.dispose(); controller.current = null; };
  }, [host, onMode]);

  return <>
    <div className="hero-image">
      <Image src={"/cinema/" + background + ".png"} fill sizes="100vw" unoptimized priority style={{ objectFit: "cover", objectPosition: "inherit" }} alt={background === "portal" ? "夜海上的光之门通向明亮的山谷" : "漂浮在云海中的未来城市"} />
    </div>
    <canvas ref={canvas} className="cinema-canvas" role="img" aria-label="光之门三维场景" data-mode={mode} />
  </>;
}
