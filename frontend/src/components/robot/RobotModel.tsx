"use client";

/* ================================================================== */
/* RobotModel — the geometry of "Ledger", reconstructed from the       */
/* approved reference (ERP/Robot.png):                                 */
/*   · oversized glossy white egg head + deep-teal crest dome          */
/*   · huge dark-glass rounded visor with glowing cyan ARC eyes        */
/*   · teal ear pods with cyan glow rings (no antenna!)                */
/*   · white egg torso, teal side accents, glowing circular "Ai" badge */
/*   · capsule arms, teal elbow joints, dark-teal articulated hands    */
/*   · left hand carries a tablet (his ledger device)                  */
/*   · white thighs/shins, teal hip+knee joints, boots with teal soles */
/*                                                                     */
/* The rig is a plain refs object owned by the stage: root / head /    */
/* arms / legs are damped there at 60fps; the face mode ref drives the */
/* expression controller here (happy arcs · excited dots · sleepy ·    */
/* focus) with periodic blinking. All motion stays in refs — zero      */
/* React re-renders per frame.                                         */
/* ================================================================== */

import { useMemo, useRef } from "react";
import type { ReactNode, RefObject } from "react";
import * as THREE from "three";
import { useFrame } from "@react-three/fiber";
import { RoundedBox } from "@react-three/drei";
import { useRobotMaterials } from "./materials";

export type FaceMode = "happy" | "excited" | "sleepy" | "focus";

/* ---- Liquid-flow tuning ---------------------------------------------
   Picture coloured liquid poured in at Ledger's boot soles and travelling
   up to his crest. FRONT_HUE is the hue that enters at the soles, SPREAD is
   how much of the colour wheel the body spans (half the wheel → several
   colours visible at once, i.e. a moving gradient rather than one solid
   recolour), and FLOW_SPEED is the sweep rate: one full pass takes ~4.5s,
   fast enough to read as liquid movement and slow enough to stay premium
   instead of strobing. */
const FRONT_HUE = 0.5; // teal-cyan — the brand ramp
const SPREAD = 0.5; // half the colour wheel from soles to crest
const FLOW_SPEED = 0.22; // sweeps per second

/* The stage owns these refs (created with useRef) and passes them down;
   RobotModel attaches them to the joints it renders. */
export type RobotRig = {
  root: RefObject<THREE.Group | null>;
  head: RefObject<THREE.Group | null>;
  armL: RefObject<THREE.Group | null>;
  armR: RefObject<THREE.Group | null>;
  legL: RefObject<THREE.Group | null>;
  legR: RefObject<THREE.Group | null>;
  face: RefObject<FaceMode>;
};

export type RobotVariant = "hero" | "compact";

/* Articulated hand: dark-teal palm + 4 curled fingers + thumb */
function Hand({ side }: { side: 1 | -1 }) {
  const m = useRobotMaterials();
  return (
    <group>
      <mesh material={m.joint} scale={[1, 1.2, 0.65]}>
        <sphereGeometry args={[0.06, 20, 20]} />
      </mesh>
      {[-0.033, -0.011, 0.011, 0.033].map((x, i) => (
        <mesh
          key={i}
          position={[x, -0.105, i % 2 === 0 ? -0.004 : 0.002]}
          rotation={[0.22, 0, x * 1.6]}
          material={m.joint}
        >
          <capsuleGeometry args={[0.015, 0.05, 4, 10]} />
        </mesh>
      ))}
      <mesh position={[side * -0.055, -0.045, 0.012]} rotation={[0.1, 0, side * 0.9]} material={m.joint}>
        <capsuleGeometry args={[0.015, 0.04, 4, 10]} />
      </mesh>
    </group>
  );
}

export function RobotModel({
  root,
  head,
  armL,
  armR,
  legL,
  legR,
  face,
  variant = "hero",
  reduced = false,
  children,
}: RobotRig & {
  variant?: RobotVariant;
  reduced?: boolean;
  children?: ReactNode;
}) {
  const m = useRobotMaterials();

  /* Face expression nodes: two arc-eyes (∩ happy) + two round dots */
  const arcL = useRef<THREE.Mesh>(null);
  const arcR = useRef<THREE.Mesh>(null);
  const dotL = useRef<THREE.Mesh>(null);
  const dotR = useRef<THREE.Mesh>(null);
  /* glow-ring nodes — "breathe" by scale (no material mutation) */
  const podRingL = useRef<THREE.Mesh>(null);
  const podRingR = useRef<THREE.Mesh>(null);
  const badgeRing = useRef<THREE.Mesh>(null);

  /* LIQUID-FLOW state: `liquid` is the travelling front (0→1, soles→crest)
     and `target` is the scratch colour each material eases toward. */
  const liquid = useRef(0);
  const target = useMemo(() => new THREE.Color(), []);

  /* POSITIONAL FLOW VARIANTS. The teal accent material is used from the
     boot soles all the way up to the crest, and `joint` runs from the hips
     to the neck. With one shared instance per material, every teal part
     would be painted the SAME colour in the SAME frame — which is exactly
     what made the previous effect read as colour-cycling instead of liquid
     flowing through him. These clones give each height band its own phase
     while the base palette stays identical. */
  const flow = useMemo(
    () => ({
      tealFoot: m.teal.clone(), // boot soles + knee joints (0.02 → 0.16)
      tealMid: m.teal.clone(), // elbows + torso side accents (0.39 → 0.40)
      tealHead: m.teal.clone(), // ear pods + crest dome (0.90 → 0.97)
      jointHigh: m.joint.clone(), // neck (0.67)
      ringPod: m.ring.clone(), // ear-pod glow rings (0.93)
    }),
    [m],
  );

  /* Glowing "Ai" chest badge text — drawn once on an offscreen canvas
     (offline-safe: no font CDN, no texture downloads). */
  const aiTex = useMemo(() => {
    const c = document.createElement("canvas");
    c.width = 256;
    c.height = 256;
    const ctx = c.getContext("2d");
    if (ctx) {
      ctx.clearRect(0, 0, 256, 256);
      ctx.fillStyle = "#ffffff";
      ctx.font = "700 132px system-ui, 'Segoe UI', sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.shadowColor = "#9be8ff";
      ctx.shadowBlur = 26;
      ctx.fillText("Ai", 128, 136);
    }
    const tex = new THREE.CanvasTexture(c);
    tex.anisotropy = 4;
    tex.colorSpace = THREE.SRGBColorSpace;
    return tex;
  }, []);

  /* Self-contained facial controller + glow breathing + the liquid colour
     flow. All animation goes through refs; the only deliberate material
     mutation is the flow painter below (react-hooks/immutability safe). */
  useFrame((s, d) => {
    const t = s.clock.elapsedTime;

    /* LIQUID FLOW. `liquid` sweeps 0→1 monotonically: imagine coloured
       liquid poured in at his boot soles and travelling up to his crest.
       Every coloured part holds a fixed position `p` along that path
       (soles 0 → crest 1) and wears the fluid that has ARRIVED at it:

           hue(part) = FRONT_HUE + SPREAD * p - liquid

       The whole pattern therefore translates upward: the colour at his
       ankles is at his knees a moment later, then his torso, then his head
       — and the instant the front reaches the next part, that part takes on
       exactly the hue that was travelling towards it. SPREAD spans half the
       wheel across the body, so several colours are visible at once (a
       moving liquid gradient, not a solid recolour of the whole robot), and
       every material EASES toward its target (lerp) so nothing snaps. */
    if (!reduced) {
      liquid.current = (liquid.current + d * FLOW_SPEED) % 1;
      const front = liquid.current;
      /* Ease every material toward its target hue — never snap. `% 1` keeps
         the hue in wheel range (three.js normalises it either way). */
      const paint = (mat: THREE.Color, h: number, s: number, l: number) => {
        target.setHSL(h % 1, s, l);
        mat.lerp(target, 0.09);
      };
      const hueAt = (p: number) => FRONT_HUE + SPREAD * p - front;
      paint(flow.tealFoot.color, hueAt(0.02), 0.62, 0.34); // boot soles
      paint(m.joint.color, hueAt(0.27), 0.58, 0.25); // hips + hands
      paint(flow.tealMid.color, hueAt(0.40), 0.62, 0.34); // knees · elbows · torso accents
      paint(m.screen.emissive, hueAt(0.45), 0.55, 0.26); // tablet glow
      paint(m.teal.color, hueAt(0.50), 0.62, 0.34); // chest badge
      paint(m.ring.emissive, hueAt(0.52), 0.95, 0.62); // badge ring
      paint(flow.jointHigh.color, hueAt(0.67), 0.58, 0.25); // neck
      paint(m.eye.emissive, hueAt(0.84), 0.95, 0.62); // eyes
      paint(flow.tealHead.color, hueAt(0.90), 0.62, 0.34); // ear pods + crest
      paint(flow.ringPod.emissive, hueAt(0.93), 0.95, 0.62); // ear-pod rings
    }
    const breathe = reduced ? 0 : Math.sin(t * 2.3) * 0.05 + 0.05;

    const mode = face.current;
    const blink = mode === "happy" && t % 3.7 < 0.12 ? 0.15 : 1;
    const arcs: Array<[typeof arcL, typeof dotL]> = [
      [arcL, dotL],
      [arcR, dotR],
    ];
    for (const [arc, dot] of arcs) {
      if (arc.current) {
        arc.current.visible = mode !== "excited";
        if (mode === "happy") arc.current.scale.set(1 + breathe, blink + breathe, 1);
        else if (mode === "focus") arc.current.scale.setScalar(0.72);
        else arc.current.scale.set(1, 0.4, 1); // sleepy: half-closed arcs
      }
      if (dot.current) {
        dot.current.visible = mode === "excited";
        const pulse = reduced ? 1 : 1 + Math.sin(t * 6) * 0.12;
        dot.current.scale.setScalar(pulse);
      }
    }

    /* ear-pod + badge glow rings breathe */
    const gs = 1 + breathe;
    if (podRingL.current) podRingL.current.scale.setScalar(gs);
    if (podRingR.current) podRingR.current.scale.setScalar(gs);
    if (badgeRing.current) badgeRing.current.scale.setScalar(gs);
  });

  /* ==== BODY GEOMETRY ==== */
  return (
    <group ref={root} scale={variant === "compact" ? 0.82 : 0.85}>
      {/* ============================ LEGS ============================ */}
      {([-1, 1] as const).map((side) => (
        <group
          key={side}
          ref={side === -1 ? legL : legR}
          position={[side * 0.17, 0.6, 0]}
        >
          <mesh material={m.joint}>
            <sphereGeometry args={[0.075, 20, 20]} />
          </mesh>
          <mesh position={[0, -0.15, 0]} material={m.shell}>
            <capsuleGeometry args={[0.068, 0.14, 6, 18]} />
          </mesh>
          <mesh position={[0, -0.29, 0]} material={flow.tealFoot}>
            <sphereGeometry args={[0.062, 18, 18]} />
          </mesh>
          <mesh position={[0, -0.39, 0]} material={m.shell}>
            <capsuleGeometry args={[0.06, 0.1, 6, 18]} />
          </mesh>
          {/* boot — chunky white shell + teal sole */}
          <mesh position={[0, -0.52, 0.06]} scale={[0.95, 0.6, 1.4]} material={m.shell}>
            <sphereGeometry args={[0.11, 24, 24]} />
          </mesh>
          <mesh position={[0, -0.565, 0.065]} scale={[0.98, 0.32, 1.42]} material={flow.tealFoot}>
            <sphereGeometry args={[0.112, 24, 24]} />
          </mesh>
        </group>
      ))}

      {/* ============================ TORSO =========================== */}
      <mesh position={[0, 0.92, 0]} scale={[1.08, 0.95, 0.82]} material={m.shell}>
        <capsuleGeometry args={[0.27, 0.34, 12, 32]} />
      </mesh>
      {/* teal hip saddle under the torso */}
      <mesh position={[0, 0.54, 0]} scale={[1, 0.5, 0.85]} material={m.joint}>
        <sphereGeometry args={[0.24, 24, 24]} />
      </mesh>
      {/* teal side accents */}
      {([-1, 1] as const).map((side) => (
        <mesh
          key={side}
          position={[side * 0.255, 0.8, 0.03]}
          rotation={[0, 0, side * -0.12]}
          material={flow.tealMid}
        >
          <capsuleGeometry args={[0.075, 0.26, 6, 16]} />
        </mesh>
      ))}

      {/* chest badge: teal disc + glowing ring + "Ai" */}
      <mesh position={[0, 1.0, 0.25]} rotation={[Math.PI / 2, 0, 0]} material={m.teal}>
        <cylinderGeometry args={[0.145, 0.145, 0.035, 32]} />
      </mesh>
      <mesh ref={badgeRing} position={[0, 1.0, 0.268]} rotation={[Math.PI / 2, 0, 0]} material={m.ring}>
        <torusGeometry args={[0.145, 0.012, 10, 40]} />
      </mesh>
      <mesh position={[0, 1.0, 0.271]}>
        <planeGeometry args={[0.19, 0.19]} />
        <meshBasicMaterial map={aiTex} transparent depthWrite={false} />
      </mesh>

      {/* ============================ ARMS ============================ */}
      {([-1, 1] as const).map((side) => (
        <group
          key={side}
          ref={side === -1 ? armL : armR}
          position={[side * 0.37, 1.12, 0]}
        >
          {/* shoulder pod rotates with the arm */}
          <mesh position={[0, -0.02, 0]} material={m.shell}>
            <sphereGeometry args={[0.105, 24, 24]} />
          </mesh>
          <mesh position={[0, -0.18, 0]} material={m.shell}>
            <capsuleGeometry args={[0.055, 0.18, 6, 18]} />
          </mesh>
          <mesh position={[0, -0.33, 0]} material={flow.tealMid}>
            <sphereGeometry args={[0.075, 20, 20]} />
          </mesh>
          <mesh position={[0, -0.44, 0]} material={m.shell}>
            <capsuleGeometry args={[0.05, 0.16, 6, 18]} />
          </mesh>
          <group position={[0, -0.57, 0]}>
            <Hand side={side} />
          </group>

          {/* tablet rides the LEFT hand — his ledger device */}
          {side === -1 && (
            <group position={[0, -0.57, 0.05]} rotation={[-1.15, 0, 0.12]}>
              <RoundedBox
                args={[0.3, 0.02, 0.42]}
                radius={0.012}
                smoothness={4}
                material={m.tablet}
              />
              <mesh position={[0, 0.012, 0]} rotation={[-Math.PI / 2, 0, 0]} material={m.screen}>
                <planeGeometry args={[0.26, 0.37]} />
              </mesh>
            </group>
          )}
        </group>
      ))}

      {/* ============================ HEAD ============================ */}
      <mesh position={[0, 1.34, 0]} material={flow.jointHigh}>
        <cylinderGeometry args={[0.075, 0.09, 0.12, 16]} />
      </mesh>
      <group ref={head} position={[0, 1.68, 0]}>
        {/* white egg shell */}
        <mesh scale={[1.08, 0.94, 0.96]} material={m.shell}>
          <sphereGeometry args={[0.36, 48, 48]} />
        </mesh>
        {/* teal crest dome */}
        <mesh position={[0, 0.3, 0]} scale={[0.44, 0.2, 0.34]} material={flow.tealHead}>
          <sphereGeometry args={[0.36, 32, 24]} />
        </mesh>
        {/* dark glass visor — proud rounded panel */}
        <RoundedBox
          args={[0.56, 0.4, 0.28]}
          radius={0.13}
          smoothness={8}
          position={[0, 0.005, 0.24]}
          material={m.visor}
        />
        {/* arc eyes (∩ ∩) + round excited-eye dots */}
        <mesh ref={arcL} position={[-0.105, 0.02, 0.365]} rotation={[0, 0, 0.1]} material={m.eye}>
          <torusGeometry args={[0.055, 0.016, 10, 24, Math.PI]} />
        </mesh>
        <mesh ref={arcR} position={[0.105, 0.02, 0.365]} rotation={[0, 0, -0.1]} material={m.eye}>
          <torusGeometry args={[0.055, 0.016, 10, 24, Math.PI]} />
        </mesh>
        <mesh ref={dotL} position={[-0.105, 0.02, 0.365]} visible={false} material={m.eye}>
          <sphereGeometry args={[0.045, 16, 16]} />
        </mesh>
        <mesh ref={dotR} position={[0.105, 0.02, 0.365]} visible={false} material={m.eye}>
          <sphereGeometry args={[0.045, 16, 16]} />
        </mesh>
        {/* teal ear pods + cyan glow rings */}
        {([-1, 1] as const).map((side) => (
          <group key={side} position={[side * 0.395, 0, 0]}>
            <mesh rotation={[0, 0, Math.PI / 2]} material={flow.tealHead}>
              <cylinderGeometry args={[0.095, 0.095, 0.05, 24]} />
            </mesh>
            <mesh position={[side * 0.03, 0, 0]} rotation={[0, Math.PI / 2, 0]} material={flow.ringPod} ref={side === -1 ? podRingL : podRingR}>
              <torusGeometry args={[0.07, 0.012, 8, 28]} />
            </mesh>
          </group>
        ))}
      </group>

      {/* speech bubble + anything that must follow the body */}
      {children}
    </group>
  );
}
