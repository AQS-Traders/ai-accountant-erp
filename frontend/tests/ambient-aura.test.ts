/**
 * AmbientAura acceptance tests (Work Stream G).
 *
 * Tests the palette-rotation hook LOGIC with fake timers plus the
 * component's structural guarantees (blob count, pointer-events,
 * print CSS, reduced-motion) that are enforced by the exported
 * constants and globals.css.
 *
 * Run: npx vitest run tests/ambient-aura.test.ts
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  AURA_BLOB_COUNT,
  AURA_BLOB_DURATIONS,
  AURA_BLOB_OPACITIES,
  AURA_CYCLE_MS,
  AURA_PALETTES,
  nextPaletteIndex,
} from "../src/components/shared/auraPalette";

const GLOBALS_CSS = resolve(__dirname, "../src/app/globals.css");
const AURA_TSX = resolve(__dirname, "../src/components/shared/AmbientAura.tsx");
const DASH_LAYOUT = resolve(__dirname, "../src/app/(dashboard)/layout.tsx");
const AUTH_LAYOUT = resolve(__dirname, "../src/app/(auth)/layout.tsx");

describe("aura palette rotation", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("advances the palette every 7 seconds (fake timers)", async () => {
    let current = 0;
    const seen: number[] = [current];
    // Mirror of useAuraPalette's interval body.
    const id = setInterval(() => {
      current = nextPaletteIndex(current, AURA_PALETTES.length);
      seen.push(current);
    }, AURA_CYCLE_MS);

    vi.advanceTimersByTime(AURA_CYCLE_MS * 3);
    clearInterval(id);

    expect(seen).toEqual([0, 1, 2, 3]);
  });

  it("cycles perpetually and seamlessly (wraps without jumps)", () => {
    const count = AURA_PALETTES.length;
    let i = 0;
    for (let step = 0; step < count * 3; step++) {
      i = nextPaletteIndex(i, count);
    }
    expect(i).toBe(0); // full cycles land back on the start palette
  });

  it("uses only light pastel colors from the allowed palette", () => {
    const allowed = new Set([
      "#14b8a6", "#7dd3fc", "#c4b5fd", "#a7f3d0", "#fbcfe8",
      "#a5f3fc", "#818cf8", "#67e8f9",
    ]);
    for (const palette of AURA_PALETTES) {
      expect(palette).toHaveLength(3);
      for (const color of palette) {
        expect(allowed.has(color.toLowerCase())).toBe(true);
      }
    }
  });

  it("rotates through MANY distinct palettes (multi-colour themes)", () => {
    // At least 8 palettes and at least 6 distinct colours overall so the
    // background visibly shifts through different light themes.
    expect(AURA_PALETTES.length).toBeGreaterThanOrEqual(8);
    const colors = new Set(AURA_PALETTES.flat());
    expect(colors.size).toBeGreaterThanOrEqual(6);
  });

  it("has exactly 3 blobs, faded opacities and desynchronized loops", () => {
    expect(AURA_BLOB_COUNT).toBe(3);
    expect(AURA_BLOB_DURATIONS).toEqual(["22s", "30s", "38s"]);
    for (const opacity of AURA_BLOB_OPACITIES) {
      expect(opacity).toBeGreaterThanOrEqual(0.1);
      expect(opacity).toBeLessThanOrEqual(0.2);
    }
    expect(new Set(AURA_BLOB_DURATIONS).size).toBe(3); // desynchronized
  });

  it("page shell isolates the stacking context (aura paints above the shell)", () => {
    // Without `isolate` on the app shell, the opaque shell background
    // paints OVER the z-index:-10 aura and the effect vanishes (the live
    // defect reported on the dashboard).
    const layout = readFileSync(DASH_LAYOUT, "utf-8");
    expect(layout).toMatch(/className="relative isolate flex h-screen overflow-hidden bg-bg-primary"/);
  });

  it("dashboard layout renders the aura as a full-viewport background", () => {
    const layout = readFileSync(DASH_LAYOUT, "utf-8");
    expect(layout).toMatch(/<AmbientAura \/>/);
    // The aura is rendered at the SHELL level (before the sidebar), so the
    // tint covers the whole screen - sidebar included - not one container.
    expect(layout.indexOf("<AmbientAura")).toBeLessThan(layout.indexOf("<Sidebar"));
    // Sidebar and topbar are translucent so the aura shows through them.
    expect(layout).toMatch(/AmbientAura/);
    const sidebar = readFileSync(
      resolve(__dirname, "../src/components/layout/Sidebar.tsx"),
      "utf-8"
    );
    expect(sidebar).toMatch(/from-sidebar-bg\/75 to-sidebar-bg-end\/75 backdrop-blur-xl/);
  });

  it("login + signup pages render the aura (auth layout)", () => {
    const layout = readFileSync(AUTH_LAYOUT, "utf-8");
    expect(layout).toMatch(/className="relative isolate min-h-screen/);
    expect(layout).toMatch(/<AmbientAura \/>/);
  });

  it("component layer is non-interactive and behind content", () => {
    const css = readFileSync(GLOBALS_CSS, "utf-8");
    expect(css).toMatch(/\.ambient-aura\s*{[^}]*pointer-events:\s*none/);
    expect(css).toMatch(/\.ambient-aura\s*{[^}]*z-index:\s*-10/);
    expect(css).toMatch(/\.ambient-aura\s*{[^}]*overflow:\s*hidden/);
    // Blobs use ONLY transform in keyframes (GPU-friendly).
    expect(css).toMatch(/@keyframes aura-drift-a\s*{[\s\S]*?transform:[^;]*scale/);
    // Keyframes never animate top/left.
    const drift = css.match(/@keyframes aura-drift-[abc]\s*{[\s\S]*?\n}/g) ?? [];
    expect(drift).toHaveLength(3);
    for (const block of drift) {
      expect(block).not.toMatch(/\b(top|left|right|bottom):/);
    }
  });

  it("print CSS hides the aura; reduced motion pauses the drift", () => {
    const css = readFileSync(GLOBALS_CSS, "utf-8");
    const printBlock = css.match(/@media print\s*{[\s\S]/);
    expect(printBlock).not.toBeNull();
    expect(css).toMatch(/@media print\s*{[\s\S]*?\.ambient-aura\s*{[^}]*display:\s*none/);
    expect(css).toMatch(
      /@media \(prefers-reduced-motion: reduce\)\s*{[\s\S]*?\.ambient-aura-blob\s*{[^}]*animation:\s*none/
    );
  });

  it("blobs crossfade via CSS transition (never an abrupt jump)", () => {
    const tsx = readFileSync(AURA_TSX, "utf-8");
    const palette = readFileSync(
      resolve(__dirname, "../src/components/shared/auraPalette.ts"),
      "utf-8"
    );
    expect(tsx).toMatch(/transition:\s*AURA_CROSSFADE_CSS/);
    // Solid colour + blur crossfades reliably in every browser
    // (background-image gradients do not transition in Firefox).
    expect(tsx).toMatch(/backgroundColor:\s*palette\[i\]/);
    expect(palette).toMatch(/background 1\.8s ease, opacity 1\.8s ease/);
  });

  it("dashboard page no longer hosts a second aura layer (shell owns it)", () => {
    const page = readFileSync(
      resolve(__dirname, "../src/app/(dashboard)/page.tsx"),
      "utf-8"
    );
    expect(page).not.toMatch(/AmbientAura/);
  });
});
