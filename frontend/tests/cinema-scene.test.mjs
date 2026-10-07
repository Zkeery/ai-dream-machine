import assert from "node:assert/strict";
import test from "node:test";
import { cameraView, photoPoint, perspective, photoLayout } from "../features/cinema/scene-math.ts";

function transform(matrix, point) {
  return Array.from({ length: 4 }, (_, row) => point.reduce((sum, value, column) => sum + matrix[column * 4 + row] * value, 0));
}
function project(point, viewport, eye = [0, 0, 6]) {
  const clip = transform(perspective(viewport.width / viewport.height), transform(cameraView(eye), [...point, 1]));
  return [clip[0] / clip[3], clip[1] / clip[3]];
}
function close(actual, expected) { assert.ok(Math.abs(actual - expected) < 0.00001, actual + " != " + expected); }

test("the portal and photo layers keep their pixel alignment across desktop, tablet and mobile", () => {
  for (const viewport of [
    { width: 1424, height: 654, mobile: false },
    { width: 712, height: 690, mobile: false },
    { width: 356, height: 396, mobile: true },
  ]) {
    const u = 0.683, v = 0.105;
    const cover = Math.max(viewport.width / 1672, viewport.height / 941);
    const expected = [(u - (viewport.mobile ? 0.735 : 0.5)) * 1672 * cover / viewport.width * 2, (0.5 - v) * 941 * cover / viewport.height * 2];
    for (const depth of [-2, -0.10, 0.03]) {
      const projected = project(photoPoint(u, v, depth, viewport), viewport);
      close(projected[0], expected[0]); close(projected[1], expected[1]);
    }
  }
});

test("a changed camera creates different parallax for separate depth layers", () => {
  const viewport = { width: 1424, height: 654, mobile: false };
  const near = photoPoint(0.75, 0.35, 0.03, viewport), far = photoPoint(0.75, 0.35, -2, viewport);
  const neutralNear = project(near, viewport), neutralFar = project(far, viewport);
  close(neutralNear[0], neutralFar[0]);
  const orbitNear = project(near, viewport, [0.38, 0.20, 6]), orbitFar = project(far, viewport, [0.38, 0.20, 6]);
  assert.ok(Math.hypot(orbitNear[0] - orbitFar[0], orbitNear[1] - orbitFar[1]) > 0.001);
});

test("the camera view translates its own origin to zero for multiple viewing angles", () => {
  for (const eye of [[0, 0, 6], [0.38, 0.20, 6], [-0.38, -0.20, 6]]) {
    const point = transform(cameraView(eye), [...eye, 1]);
    close(point[0], 0); close(point[1], 0); close(point[2], 0); close(point[3], 1);
    const origin = transform(cameraView(eye), [0, 0, 0, 1]);
    close(origin[0], 0); close(origin[1], 0); assert.ok(origin[2] < 0);
  }
});

test("invalid or hidden viewport sizes cannot introduce infinite projection values", () => {
  for (const width of [0, -1, NaN, Infinity]) assert.throws(() => photoLayout({ width, height: 600, mobile: false }), RangeError);
  assert.throws(() => photoLayout({ width: 600, height: 0, mobile: false }), RangeError);
  assert.throws(() => perspective(0), RangeError);
});

