"use client";

/* ================================================================== */
/* "Ledger" — the 3D robot mascot. Lives INSIDE its stage (the hero's  */
/* right column / a corner of the auth pages) — never fixed to the     */
/* viewport, never covering copy.                                      */
/*                                                                      */
/* Geometry now matches the approved brand reference (ERP/Robot.png):  */
/* white glossy shell, deep-teal accents, dark glass visor, glowing    */
/* cyan face, "Ai" chest badge, tablet in hand (see robot/RobotModel). */
/*                                                                      */
/* Behaviour: boot — he is part of the hero floor from frame one (lying
   flat, no fall-in), rises zombie-style, stretches awake, waves hello —
   then a random activity loop: wave · idle · walk · hop · dance · clap ·
   read · sit — roaming the stage's empty areas. The facial expression
   follows the activity (excited / sleepy / focus).
   Falls back to a calm idle under prefers-reduced-motion. */
/* ================================================================== */

import { Component, Suspense, useEffect, useRef, useState } from "react";
import type { ReactNode, RefObject } from "react";
import * as THREE from "three";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { ContactShadows } from "@react-three/drei";
import { RobotModel, type FaceMode } from "@/components/robot/RobotModel";

type Variant = "hero" | "compact";
type Phase =
  | "boot" | "wave" | "idle" | "walk" | "hop" | "dance" | "clap" | "read" | "sit"
  | "interact" | "spin" | "stretch" | "think" | "march" | "peek" | "jumpingjack"
  | "robotdance" | "moonwalk" | "conductor" | "kungfu" | "bow" | "cartwheel" | "meditate";
type PokeTarget = "invoice" | "journal" | "trial" | "overview" | "ask";

/* Hero hotspots Ledger can walk up to and physically interact with.
   xf = x position as a fraction of the roam bound; reach = [armX, armZ]
   tailored to where the element sits (high card vs low pill). */
const HOTSPOTS: Record<PokeTarget, { xf: number; line: string; reach: [number, number] }> = {
  invoice: {
    xf: -0.92,
    line: "Invoice INV-2025-001 — created and posted!",
    reach: [-0.6, 1.9],
  },
  journal: {
    xf: -0.88,
    line: "Journal entry recorded. Debits equal credits — always.",
    reach: [-0.55, 1.8],
  },
  trial: {
    xf: -0.84,
    line: "Trial balance updated. Everything adds up.",
    reach: [-0.5, 1.7],
  },
  overview: {
    xf: 0.95,
    line: "Fresh numbers, hot off the ledger!",
    reach: [-0.35, 2.15],
  },
  ask: {
    xf: 0.7,
    line: "Ask me anything — I speak fluent accounting.",
    reach: [-0.95, 1.15],
  },
};
const POKEABLE: PokeTarget[] = ["invoice", "journal", "trial", "overview", "ask"];

function pick<T>(arr: T[]): T {
  return arr[Math.floor(Math.random() * arr.length)];
}
function rand(min: number, max: number): number {
  return min + Math.random() * (max - min);
}

/* Facial expression follows the current activity */
function faceForPhase(p: Phase): FaceMode {
  if (p === "dance" || p === "clap" || p === "hop" || p === "wave") return "excited";
  if (p === "sit") return "sleepy";
  if (p === "read" || p === "think") return "focus";
  if (p === "spin" || p === "jumpingjack" || p === "march" || p === "peek" || p === "stretch") return "excited";
  if (p === "robotdance" || p === "moonwalk" || p === "conductor" || p === "kungfu" || p === "cartwheel") return "excited";
  if (p === "meditate") return "focus";
  return "happy";
}

/* On-demand command routines (dispatched via the `ledger-command`
   CustomEvent from the command deck / direct mouse interaction).
   Each maps to its run duration in seconds. */
const COMMAND_DUR: Record<string, number> = {
  robotdance: 3.2,
  moonwalk: 3.6,
  conductor: 3.6,
  kungfu: 3.0,
  bow: 2.8,
  cartwheel: 1.7,
  meditate: 3.8,
};

function RobotActor({ variant, reduced }: { variant: Variant; reduced: boolean }) {
  const root = useRef<THREE.Group>(null);
  const head = useRef<THREE.Group>(null);
  const armL = useRef<THREE.Group>(null);
  const armR = useRef<THREE.Group>(null);
  const legL = useRef<THREE.Group>(null);
  const legR = useRef<THREE.Group>(null);
  const face = useRef<FaceMode>("happy");

  /* --- behaviour state (refs on purpose: 60fps, no re-renders) --- */
  /* Boot sequence on load: Ledger starts lying flat (zombie-rest), stands
     up in place, then activates with a stretch + wave before the ambient
     activity loop takes over. NO falling-from-above entrance — he is a
     native part of the hero; the stage simply opens on him already lying
     there, and he rises. */
  const phase = useRef<Phase>("boot");
  const t = useRef(0);
  const dur = useRef(reduced ? Infinity : 5.0); // boot runs ~5s, then loop
  const dir = useRef<1 | -1>(1);
  useEffect(() => {
    dir.current = Math.random() > 0.5 ? 1 : -1;
  }, []);

  const { viewport } = useThree();
  /* Hero: roam the FULL stage width — he owns the whole right column now.
     The floating cards only render ≥ md; he still walks up to them. */
  const bound =
    variant === "hero"
      ? Math.min(Math.max(0.65, viewport.width / 2 - 1.0), 1.1)
      : Math.max(0.3, viewport.width / 2 - 0.95);

  /* Cards exist only from md up — track so mobile never "pokes" air */
  const [wide, setWide] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia("(min-width: 768px)");
    setWide(mq.matches);
    const on = () => setWide(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);

  /* --- interaction state --- */
  const targetRef = useRef<PokeTarget>("invoice");
  const firedRef = useRef(false);
  const zTarget = useRef(0);

  /* --- command channel: the command deck / direct clicks dispatch
     `ledger-command`; the very next frame performs that routine. --- */
  useEffect(() => {
    const onCommand = (e: Event) => {
      const cmd = (e as CustomEvent<{ command?: string }>).detail?.command;
      if (!cmd || reduced) return;
      const durSec = COMMAND_DUR[cmd];
      if (!durSec) return;
      phase.current = cmd as Phase;
      t.current = 0;
      dur.current = durSec;
      zTarget.current = cmd === "moonwalk" ? 0.05 : 0;
      dir.current = Math.random() > 0.5 ? 1 : -1;
      firedRef.current = true; // routine runs instead of a hotspot poke
    };
    window.addEventListener("ledger-command", onCommand);
    return () => window.removeEventListener("ledger-command", onCommand);
  }, [reduced]);

  useFrame((state, rawDelta) => {
    const g = root.current;
    if (!g) return;
    const d = Math.min(rawDelta, 0.05);
    t.current += d;
    const time = state.clock.elapsedTime;

    /* ---- BOOT SEQUENCE (once per load) ----
       0.0-1.2s lying flat (visitors see him "asleep"), 1.2-3.0s zombie
       stand-up (pitch 90->0, knees bend), 3.0-4.0s activation stretch
       (arms to the sky, up on toes), 4.0-5.0s hello wave. Then the
       normal activity loop takes over. */
    if (phase.current === "boot") {
      if (reduced || t.current >= 5.0) {
        phase.current = "idle";
        t.current = 0;
        dur.current = rand(2.4, 3.2);
      }
    }

    /* random activity loop — bigger repertoire, weighted toward motion */
    if (phase.current !== "boot" && t.current >= dur.current && !reduced) {
      t.current = 0;
      const canPoke = wide && variant === "hero";
      const pool: Phase[] = canPoke
        ? ["walk", "walk", "idle", "wave", "dance", "clap", "read", "sit", "hop",
           "interact", "interact", "spin", "stretch", "think", "march", "peek", "jumpingjack"]
        : ["walk", "walk", "idle", "wave", "dance", "clap", "read", "sit", "hop",
           "spin", "stretch", "think", "march", "peek", "jumpingjack"];
      let next = pick(pool);
      if (next === phase.current) next = pick(pool);
      phase.current = next;
      if (next === "interact") {
        targetRef.current = pick(POKEABLE);
        firedRef.current = false;
        dur.current = 4.6;
        zTarget.current = 0.3; // small step forward — kept shallow so his
        // feet and reaching hand always stay inside the frame (no-crop)
      } else if (next === "walk") {
        dur.current = rand(3.5, 6.5);
        zTarget.current = rand(-0.15, 0.5); // wander the depth of the stage too
      } else if (next === "spin") {
        dur.current = rand(1.6, 2.2);
        zTarget.current = 0.1;
      } else if (next === "jumpingjack" || next === "march") {
        dur.current = rand(2.4, 3.6);
        zTarget.current = 0.12;
      } else if (next === "think" || next === "peek") {
        dur.current = rand(2.2, 3.4);
        zTarget.current = 0.08;
      } else {
        dur.current = rand(2.6, 4.4);
        zTarget.current = 0.08;
      }
    }
    if (reduced) phase.current = "idle";
    const p = phase.current;
    /* Boot face: eyes closed while down, wide awake once he's up */
    face.current =
      p === "boot"
        ? t.current < 2.2 ? "sleepy" : "excited"
        : faceForPhase(p);

    /* ---- BOOT motion: zombie stand-up, activation stretch, hello wave.
       Runs in place on the hero floor — no travel, no falling-in. ---- */
    if (p === "boot") {
      const stand = THREE.MathUtils.clamp((t.current - 1.2) / 1.8, 0, 1);
      const rise = stand * stand * (3 - 2 * stand); // smoothstep ease
      const stretchT = THREE.MathUtils.clamp((t.current - 3.0) / 1.0, 0, 1);
      const waveT = THREE.MathUtils.clamp((t.current - 4.0) / 1.0, 0, 1);

      /* flat on his back -> upright, exactly where he lies */
      g.rotation.x = (1 - rise) * (Math.PI / 2);
      g.rotation.y = 0;
      g.rotation.z = 0;
      g.position.x = THREE.MathUtils.damp(g.position.x, 0, 2, d);
      g.position.z = THREE.MathUtils.damp(g.position.z, 0.1, 2, d);
      const kneeBend = Math.sin(rise * Math.PI) * 0.22;
      g.position.y = THREE.MathUtils.damp(
        g.position.y,
        (1 - rise) * 0.02 + kneeBend * 0.1 + Math.sin(time * 2) * 0.02,
        8,
        d,
      );

      /* knees bend mid-rise, straighten once standing */
      if (legL.current) {
        legL.current.rotation.x = THREE.MathUtils.damp(legL.current.rotation.x, kneeBend * 1.6, 6, d);
        legL.current.rotation.z = THREE.MathUtils.damp(legL.current.rotation.z, 0, 6, d);
      }
      if (legR.current) {
        legR.current.rotation.x = THREE.MathUtils.damp(legR.current.rotation.x, kneeBend * 1.6, 6, d);
        legR.current.rotation.z = THREE.MathUtils.damp(legR.current.rotation.z, 0, 6, d);
      }

      /* arms: resting while down -> sky stretch -> hello wave */
      const stretchAmt = stretchT * (1 - waveT);
      const azLb = -0.1 + (-2.45 + Math.sin(t.current * 5) * 0.08) * stretchAmt;
      const azRb = 0.1
        + (2.45 - Math.sin(t.current * 5) * 0.08) * stretchAmt
        + waveT * (2.25 + Math.sin(t.current * 9) * 0.3);
      const axb = -0.3 * stretchAmt;
      if (armL.current) {
        armL.current.rotation.x = THREE.MathUtils.damp(armL.current.rotation.x, axb, 6, d);
        armL.current.rotation.z = THREE.MathUtils.damp(armL.current.rotation.z, azLb, 6, d);
      }
      if (armR.current) {
        armR.current.rotation.x = THREE.MathUtils.damp(armR.current.rotation.x, axb, 6, d);
        armR.current.rotation.z = THREE.MathUtils.damp(armR.current.rotation.z, azRb, 6, d);
      }

      /* head: eyes toward the sky as he rises, then settles on the visitor */
      if (head.current) {
        head.current.rotation.x = THREE.MathUtils.damp(
          head.current.rotation.x,
          (1 - rise) * 0.5 - state.pointer.y * 0.2 * rise,
          5,
          d,
        );
        head.current.rotation.y = THREE.MathUtils.damp(
          head.current.rotation.y,
          state.pointer.x * 0.5 * rise,
          5,
          d,
        );
        head.current.rotation.z = THREE.MathUtils.damp(head.current.rotation.z, 0, 5, d);
      }
      return; // boot owns this frame; the activity loop resumes afterwards
    }

    /* movement: roam the floor, or walk UP TO a card and interact with it */
    let faceY: number;
    const hs = p === "interact" ? HOTSPOTS[targetRef.current] : null;
    if (hs) {
      /* no-crop: clamp the step-in point so his raised reaching hand stays
         inside the frame on every viewport size */
      const maxX = Math.max(0.15, state.viewport.width / 2 - 1.05);
      const tx = THREE.MathUtils.clamp(hs.xf * bound, -maxX, maxX);
      const dx = tx - g.position.x;
      g.position.x = THREE.MathUtils.damp(g.position.x, tx, 1.9, d);
      /* face where he is walking, then face the visitor once in position */
      faceY =
        t.current < 1.0 && Math.abs(dx) > 0.06
          ? THREE.MathUtils.clamp(dx, -1, 1) * 0.9
          : Math.sin(time * 0.6) * 0.06;
      /* the actual "poke": fire once as his hand lands */
      if (t.current > 1.5 && !firedRef.current) {
        firedRef.current = true;
        window.dispatchEvent(new CustomEvent("ledger-poke", { detail: { target: targetRef.current } }));
      }
    } else if (p === "moonwalk") {
      /* glides backwards across the stage — lean, slide, tiny toe-bounce.
         Hard-clamped inside the no-crop margin: even if a command fires
         while he stands at a hotspot edge, he glides smoothly back inside
         instead of drifting out of the canvas. */
      const lim = Math.max(0.3, bound - 0.25);
      const targetX = THREE.MathUtils.clamp(
        g.position.x - dir.current * 0.35,
        -lim,
        lim,
      );
      g.position.x = THREE.MathUtils.damp(g.position.x, targetX, 2.2, d);
      if (g.position.x >= lim - 0.02) dir.current = -1;
      else if (g.position.x <= -lim + 0.02) dir.current = 1;
      faceY = dir.current * 0.5;
    } else if (p === "walk") {
      g.position.x += dir.current * 0.5 * d;
      if (g.position.x > bound) dir.current = -1;
      else if (g.position.x < -bound) dir.current = 1;
      faceY = dir.current * 0.55;
    } else if (p === "clap" || p === "read" || p === "sit" || p === "spin"
      || p === "march" || p === "jumpingjack" || p === "stretch" || p === "think"
      || p === "robotdance" || p === "conductor" || p === "kungfu" || p === "bow"
      || p === "cartwheel" || p === "meditate") {
      faceY = 0;
    } else {
      faceY = Math.sin(time * 0.6) * 0.12;
    }
    /* no-crop guard: while an arm sweeps out to the side (wave, dance,
       clap, spin, stretch, jumping jacks) drift him back toward the centre
       so his hand can never cross the canvas border */
    if (!hs && (p === "wave" || p === "dance" || p === "clap" || p === "spin"
      || p === "stretch" || p === "jumpingjack" || p === "cartwheel")) {
      g.position.x = THREE.MathUtils.damp(g.position.x, g.position.x * 0.3, 1.5, d);
    }
    g.position.z = THREE.MathUtils.damp(g.position.z, zTarget.current, 2, d);
    g.rotation.y = THREE.MathUtils.damp(g.rotation.y, faceY, 4, d);
    if (p === "spin") g.rotation.y = t.current * 5.2; // a full twirl
    if (p === "bow") {
      /* deep respectful bow — pitch forward and rise back gracefully */
      const bowT = Math.sin(Math.min((t.current / 2.8) * Math.PI, Math.PI)) * 0.5;
      g.rotation.x = THREE.MathUtils.damp(g.rotation.x, bowT, 6, d);
    } else {
      g.rotation.x = THREE.MathUtils.damp(g.rotation.x, 0, 4, d);
    }
    if (p === "cartwheel") g.rotation.z = (t.current / 1.7) * Math.PI * 2; // full side roll
    g.rotation.z =
      p === "dance"
        ? Math.sin(t.current * 4) * 0.1
        : p === "peek"
          ? Math.sin(Math.min(t.current * 1.6, Math.PI)) * 0.3
          : THREE.MathUtils.damp(g.rotation.z, 0, 4, d);

    const bob = p === "walk" ? Math.abs(Math.sin(t.current * 9)) * 0.05 : Math.sin(time * 2) * 0.035;
    const hopY =
      p === "hop"
        ? Math.max(0, Math.sin(t.current * 5.5)) * 0.42
        : p === "jumpingjack"
          ? Math.abs(Math.sin(t.current * 6)) * 0.16
          : p === "march"
            ? Math.abs(Math.sin(t.current * 7)) * 0.08
            : p === "moonwalk"
              ? Math.max(0, Math.sin(t.current * 3.2)) * 0.04
              : p === "cartwheel"
                ? Math.sin(Math.min(t.current / 1.7, 1) * Math.PI) * 0.55
                : 0;
    const sitY = p === "sit" ? -0.3 : 0;
    const toeY = p === "stretch" ? 0.06 : 0;
    const medY = p === "meditate" ? 0.22 + Math.sin(t.current * 1.4) * 0.05 : 0;
    g.position.y = THREE.MathUtils.damp(g.position.y, bob + hopY + sitY + toeY + medY, 10, d);

    /* limbs */
    const dampTo = (o: RefObject<THREE.Group | null>, x: number, z: number) => {
      if (!o.current) return;
      o.current.rotation.x = THREE.MathUtils.damp(o.current.rotation.x, x, 6, d);
      o.current.rotation.z = THREE.MathUtils.damp(o.current.rotation.z, z, 6, d);
    };
    const swing = p === "walk" ? Math.sin(t.current * 9) * 0.55 : 0;
    let axL = 0;
    let azL = 0;
    let axR = 0;
    let azR = 0;
    if (p === "wave") {
      azR = 2.35 + Math.sin(t.current * 10) * 0.3;
    } else if (p === "dance") {
      azL = -2.5 + Math.sin(t.current * 7) * 0.45;
      azR = 2.5 + Math.cos(t.current * 7) * 0.45;
      axL = axR = 0.25;
    } else if (p === "clap") {
      axL = axR = -1.15;
      azL = 0.32 + Math.max(0, Math.sin(t.current * 7)) * 0.5;
      azR = -azL;
    } else if (p === "read") {
      /* tablet arm lifts so the screen faces him; he looks down at it */
      axL = -1.15;
      axR = -0.9;
      azL = -0.25;
      azR = 0.3;
    } else if (p === "sit") {
      axL = -0.45;
      axR = -0.45;
      azL = -0.15;
      azR = 0.15;
    } else if (p === "walk") {
      axL = swing;
      axR = -swing;
    } else if (p === "interact") {
      /* reach out to the element; a small press-bounce as the hand lands */
      const [rx, rz] = HOTSPOTS[targetRef.current].reach;
      const press = t.current > 1.5 ? Math.max(0, Math.sin((t.current - 1.5) * 9)) * 0.3 : 0;
      axR = rx;
      azR = rz + press;
      azL = -0.1 + Math.sin(time * 1.7) * 0.05;
    } else if (p === "spin") {
      /* arms tucked for the twirl */
      azL = -1.5;
      azR = 1.5;
      axL = axR = -0.2;
    } else if (p === "stretch") {
      /* both arms reach for the sky with a slow sway, up on his toes */
      const sway = Math.sin(t.current * 2.4) * 0.18;
      azL = -2.55 + sway;
      azR = 2.55 - sway;
      axL = axR = -0.3;
    } else if (p === "think") {
      /* hand to chin, pondering */
      axR = -2.15;
      azR = 0.4;
      azL = -0.12;
    } else if (p === "march") {
      /*vigorous arm pump opposite the knees */
      axL = Math.sin(t.current * 7) * 0.9;
      axR = -Math.sin(t.current * 7) * 0.9;
    } else if (p === "jumpingjack") {
      /* arms sweep up-and-out together with each beat */
      const up = (Math.sin(t.current * 6) + 1) / 2;
      azL = -(0.25 + up * 2.2);
      azR = 0.25 + up * 2.2;
    } else if (p === "peek") {
      /* hands cupped, leaning to peek around */
      azL = -0.5;
      azR = 0.5;
    } else if (p === "robotdance") {
      /* "the robot" — staccato angles snapping on the half-beat */
      const q = Math.floor(t.current * 2) % 2;
      azL = -(1.15 + q * 0.45);
      azR = 1.15 - (1 - q) * 0.45;
      axL = axR = -0.15 - q * 0.25;
    } else if (p === "conductor") {
      /* conducts the numbers like an orchestra — grand slow sweeps */
      azR = 2.1 + Math.sin(t.current * 2.2) * 0.6;
      axR = -0.55;
      azL = -0.75 + Math.cos(t.current * 2.2) * 0.35;
      axL = -0.3;
    } else if (p === "kungfu") {
      /* three crisp poses: jab → high block → wide guard */
      const pose = Math.floor(t.current / 1.0) % 3;
      if (pose === 0) {
        axR = -1.35;
        azR = 0.55;
        azL = -0.35;
      } else if (pose === 1) {
        azR = 2.35;
        azL = -0.4;
        axL = -0.2;
      } else {
        axL = axR = -0.5;
        azL = -1.75;
        azR = 1.75;
      }
    } else if (p === "bow") {
      /* graceful flourish out as he bends, closing on the rise */
      const rise = Math.sin(Math.min((t.current / 2.8) * Math.PI, Math.PI));
      azL = -1.55 - rise * 0.4;
      azR = 1.55 + rise * 0.4;
      axL = axR = -0.25;
    } else if (p === "cartwheel") {
      /* arms locked out for the full side roll */
      azL = -2.65;
      azR = 2.65;
      axL = axR = -0.1;
    } else if (p === "meditate") {
      /* hands resting toward the knees; he levitates while breathing slow */
      axL = axR = -0.72;
      azL = -0.3;
      azR = 0.3;
    } else {
      azL = -0.08 + Math.sin(time * 1.7) * 0.05;
      azR = 0.08 - Math.sin(time * 1.7) * 0.05;
    }
    dampTo(armL, axL, azL);
    dampTo(armR, axR, azR);

    const legSwing =
      p === "walk"
        ? Math.sin(t.current * 9) * 0.5
        : p === "march"
          ? Math.sin(t.current * 7) * 0.95
          : p === "jumpingjack"
            ? Math.sin(t.current * 6) * 0.35
            : 0;
    const sitLeg = p === "sit" ? -1.45 : 0;
    if (p === "meditate") {
      /* crossed-lotus suggestion while levitating */
      dampTo(legL, 0.85, -0.55);
      dampTo(legR, 0.85, 0.55);
    } else {
      const moonSwing = p === "moonwalk" ? Math.sin(t.current * 3) * 0.25 : 0;
      dampTo(legL, sitLeg + legSwing + moonSwing, 0);
      dampTo(legR, sitLeg - legSwing - moonSwing, 0);
    }

    /* head: look down while reading, ponder upward while thinking, tilt
       into the peek, snap on the robot-dance beat, follow the cursor
       generously otherwise (mouse control) */
    if (head.current) {
      const look = reduced ? 0 : -state.pointer.y * 0.2;
      const down =
        (p === "read" ? 0.42 : 0) + (p === "sit" ? 0.08 : 0) + (p === "think" ? -0.24 : 0);
      const tilt =
        p === "peek"
          ? Math.sin(Math.min(t.current * 1.6, Math.PI)) * 0.3
          : p === "think"
            ? 0.14
            : p === "robotdance"
              ? Math.floor(t.current * 2) % 2
                ? 0.12
                : -0.08
              : 0;
      head.current.rotation.x = THREE.MathUtils.damp(head.current.rotation.x, down + look, 5, d);
      head.current.rotation.y = reduced
        ? 0
        : THREE.MathUtils.damp(head.current.rotation.y, state.pointer.x * 0.5, 5, d);
      head.current.rotation.z = THREE.MathUtils.damp(head.current.rotation.z, tilt, 5, d);
    }
  });

  return (
    <RobotModel
      root={root}
      head={head}
      armL={armL}
      armR={armR}
      legL={legL}
      legR={legR}
      face={face}
      variant={variant}
      reduced={reduced}
    >
    </RobotModel>
  );
}

class RobotErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? null : this.props.children;
  }
}


function CameraRig({ variant }: { variant: Variant }) {
  const { camera } = useThree();
  useEffect(() => {
    /* hero: closer + centred — the canvas now spans the FULL hero row, so
       framing him larger keeps him prominent while his head and boots both
       stay inside the frame at any height. compact pulls back + lifts the
       framing for the auth-page corner. */
    camera.position.set(0, variant === "hero" ? 1.5 : 1.18, variant === "hero" ? 5.8 : 4.75);
    camera.lookAt(0, variant === "hero" ? 1.1 : 1.02, 0);
  }, [camera, variant]);
  return null;
}

/* The stage the robot lives in. Sized by its parent — the hero's right
   column or a corner box on the auth pages. Transparent background:
   he floats directly over the page UI. */
export default function HeroRobotStage({ variant = "hero" }: { variant?: Variant }) {
  const [reduced, setReduced] = useState(false);

  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    setReduced(mq.matches);
    const onChange = (e: MediaQueryListEvent) => setReduced(e.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  return (
    <div className="relative h-full w-full" aria-hidden="true">
      <Canvas
        dpr={[1, 2]}
        performance={{ min: 0.5 }}
        gl={{ antialias: true, alpha: true, powerPreference: "high-performance" }}
        camera={{ position: [0, 1.5, 5.8], fov: 33 }}
        style={{ background: "transparent" }}
      >
        <CameraRig variant={variant} />

        {/* Lighting rig: soft studio key + teal rim = premium toy look */}
        <ambientLight intensity={0.6} />
        <hemisphereLight args={["#e0f2fe", "#ccfbf1", 0.6]} />
        <directionalLight position={[2.5, 4, 3]} intensity={1.5} />
        <directionalLight position={[-3, 2, -2.5]} intensity={1.1} color="#5eead4" />
        <pointLight position={[0, 0.5, 2.4]} intensity={0.4} color="#cffafe" />

        <Suspense fallback={null}>
          <RobotErrorBoundary>
            <RobotActor variant={variant} reduced={reduced} />
          </RobotErrorBoundary>
          {/* Robot only — no podium, no platform elements. A soft contact
              shadow is all that grounds him on the page backdrop. */}
          <ContactShadows
            position={[0, 0.001, 0]}
            opacity={0.16}
            scale={3.0}
            blur={2.8}
            far={1.8}
            color="#1e3a5f"
          />
        </Suspense>
      </Canvas>
    </div>
  );
}
