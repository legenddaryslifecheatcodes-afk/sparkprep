import { useRef, useMemo, useState, Suspense } from "react";
import { Canvas, useLoader, useFrame } from "@react-three/fiber";
import { OrbitControls, ContactShadows, Environment } from "@react-three/drei";
import * as THREE from "three";
import { Download, RotateCw } from "lucide-react";

/**
 * Photoreal book mockup with per-binding physical materials.
 * - paperback → matte satin laminate (low sheen)
 * - hardcover_case → gloss laminate (mirror sheen, board overhang)
 * - hardcover_jacket → dust-jacket paper (matte, warm, subtle grain)
 */

// Binding-specific physical material presets
const BINDING_PROFILES = {
  paperback: {
    label: "Matte Laminate",
    cover: { roughness: 0.55, metalness: 0.04, envMapIntensity: 0.4 },
    spine: { roughness: 0.6, metalness: 0.06, tint: "#141013" },
    pageEdge: "#F4EDE0",
    boardOverhang: 0,
    grainStrength: 0,
  },
  hardcover_case: {
    label: "Gloss Case Laminate",
    cover: { roughness: 0.18, metalness: 0.1, envMapIntensity: 1.15 },
    spine: { roughness: 0.25, metalness: 0.15, tint: "#1a1613" },
    pageEdge: "#EFE5CE",
    boardOverhang: 0.012,
    grainStrength: 0,
  },
  hardcover_jacket: {
    label: "Dust Jacket Paper",
    cover: { roughness: 0.82, metalness: 0.02, envMapIntensity: 0.22 },
    spine: { roughness: 0.78, metalness: 0.03, tint: "#241d18" },
    pageEdge: "#F6EFDD",
    boardOverhang: 0.008,
    grainStrength: 0.35,
  },
};

// Procedural page-edge texture (thin horizontal stripes)
function makePageEdgeTexture(baseColor = "#F4EDE0") {
  const c = document.createElement("canvas");
  c.width = 4; c.height = 512;
  const ctx = c.getContext("2d");
  ctx.fillStyle = baseColor;
  ctx.fillRect(0, 0, 4, 512);
  ctx.fillStyle = "rgba(90, 70, 40, 0.35)";
  for (let y = 0; y < 512; y += 2) ctx.fillRect(0, y, 4, 1);
  const grad = ctx.createLinearGradient(0, 0, 4, 0);
  grad.addColorStop(0, "rgba(0,0,0,0.28)");
  grad.addColorStop(0.5, "rgba(0,0,0,0)");
  grad.addColorStop(1, "rgba(0,0,0,0.16)");
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, 4, 512);
  const t = new THREE.CanvasTexture(c);
  t.wrapS = t.wrapT = THREE.RepeatWrapping;
  t.anisotropy = 8;
  return t;
}

// Dark spine/back cover texture with subtle grain
function makeSpineTexture(color = "#1A1512") {
  const c = document.createElement("canvas");
  c.width = 64; c.height = 512;
  const ctx = c.getContext("2d");
  ctx.fillStyle = color;
  ctx.fillRect(0, 0, 64, 512);
  for (let i = 0; i < 300; i++) {
    const x = Math.random() * 64;
    const alpha = Math.random() * 0.06;
    ctx.fillStyle = `rgba(255, 220, 180, ${alpha})`;
    ctx.fillRect(x, 0, 0.5, 512);
  }
  const grad = ctx.createLinearGradient(0, 0, 0, 512);
  grad.addColorStop(0, "rgba(0,0,0,0.35)");
  grad.addColorStop(0.15, "rgba(0,0,0,0)");
  grad.addColorStop(0.85, "rgba(0,0,0,0)");
  grad.addColorStop(1, "rgba(0,0,0,0.35)");
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, 64, 512);
  const t = new THREE.CanvasTexture(c);
  t.anisotropy = 8;
  return t;
}

// Dust-jacket paper grain (overlay for the cover normal-ish look — used as roughness map on jacket)
function makeGrainTexture(strength = 0) {
  if (strength <= 0) return null;
  const c = document.createElement("canvas");
  c.width = 512; c.height = 512;
  const ctx = c.getContext("2d");
  ctx.fillStyle = "#808080";
  ctx.fillRect(0, 0, 512, 512);
  for (let i = 0; i < 15000; i++) {
    const x = Math.random() * 512;
    const y = Math.random() * 512;
    const v = 128 + Math.floor((Math.random() - 0.5) * 120 * strength);
    ctx.fillStyle = `rgb(${v},${v},${v})`;
    ctx.fillRect(x, y, 1, 1);
  }
  const t = new THREE.CanvasTexture(c);
  t.wrapS = t.wrapT = THREE.RepeatWrapping;
  t.anisotropy = 8;
  return t;
}

function BookMesh({ frontImageUrl, w, h, d, autoSpin, binding }) {
  const meshRef = useRef();
  const profile = BINDING_PROFILES[binding] || BINDING_PROFILES.paperback;

  const pageTex = useMemo(() => makePageEdgeTexture(profile.pageEdge), [profile.pageEdge]);
  const spineTex = useMemo(() => makeSpineTexture(profile.spine.tint), [profile.spine.tint]);
  const grainTex = useMemo(() => makeGrainTexture(profile.grainStrength), [profile.grainStrength]);

  const coverTex = useLoader(
    THREE.TextureLoader,
    frontImageUrl || "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='400' height='600'%3E%3Crect width='400' height='600' fill='%23111'/%3E%3Ctext x='200' y='300' text-anchor='middle' fill='%23888' font-family='monospace' font-size='16'%3ENo cover%3C/text%3E%3C/svg%3E"
  );
  coverTex.colorSpace = THREE.SRGBColorSpace;
  coverTex.anisotropy = 8;

  useFrame(({ clock }) => {
    if (autoSpin && meshRef.current) {
      meshRef.current.rotation.y = Math.sin(clock.getElapsedTime() * 0.4) * 0.6 - 0.35;
      meshRef.current.rotation.x = Math.sin(clock.getElapsedTime() * 0.3) * 0.05;
    }
  });

  // Material order for BoxGeometry: [+X, -X, +Y, -Y, +Z, -Z]
  const materials = useMemo(() => {
    const pageMat = new THREE.MeshStandardMaterial({ map: pageTex, roughness: 0.85, metalness: 0.02 });
    const spineMat = new THREE.MeshStandardMaterial({
      map: spineTex, roughness: profile.spine.roughness, metalness: profile.spine.metalness,
    });
    const backMat = new THREE.MeshStandardMaterial({
      map: spineTex, roughness: profile.spine.roughness, metalness: profile.spine.metalness * 0.7,
    });
    const frontMat = new THREE.MeshStandardMaterial({
      map: coverTex,
      roughness: profile.cover.roughness,
      metalness: profile.cover.metalness,
      envMapIntensity: profile.cover.envMapIntensity,
      // dust-jacket grain adds micro roughness variation
      roughnessMap: grainTex || null,
    });
    return [pageMat, spineMat, pageMat, pageMat, frontMat, backMat];
  }, [pageTex, spineTex, coverTex, grainTex, profile]);

  const boardW = w + profile.boardOverhang * 2;
  const boardH = h + profile.boardOverhang * 2;

  return (
    <group ref={meshRef} rotation={[-0.08, -0.55, 0]} castShadow>
      {/* Hardcover board overhang — a thin dark ring around the pages */}
      {profile.boardOverhang > 0 && (
        <mesh position={[0, 0, 0]} castShadow>
          <boxGeometry args={[boardW, boardH, d * 1.02]} />
          <meshStandardMaterial color="#0a0806" roughness={0.5} metalness={0.15} />
        </mesh>
      )}
      {/* Book block */}
      <mesh castShadow receiveShadow material={materials} position={[0, 0, 0]}>
        <boxGeometry args={[w, h, d]} />
      </mesh>
      {/* Bevel highlight along the top edge */}
      <mesh position={[0, h / 2 + 0.001, 0]}>
        <boxGeometry args={[w, 0.005, d]} />
        <meshBasicMaterial color="#ffffff" opacity={0.15} transparent />
      </mesh>
    </group>
  );
}

function Scene({ frontImageUrl, trim, spineWidth, autoSpin, binding }) {
  const scale = 0.28;
  const w = (trim?.w || 6) * scale;
  const h = (trim?.h || 9) * scale;
  const d = Math.max(0.05, (spineWidth || 0.6) * scale);

  return (
    <>
      <color attach="background" args={["#F4F1EC"]} />
      <ambientLight intensity={0.35} />
      <directionalLight
        castShadow
        position={[3, 5, 4]}
        intensity={2.2}
        shadow-mapSize-width={2048}
        shadow-mapSize-height={2048}
        shadow-camera-left={-4}
        shadow-camera-right={4}
        shadow-camera-top={4}
        shadow-camera-bottom={-4}
      />
      <directionalLight position={[-4, 2, -3]} intensity={0.6} color="#FFDCC8" />
      <pointLight position={[-3, -1, 3]} intensity={0.3} />

      <Suspense fallback={null}>
        <Environment preset="studio" background={false} />
        <BookMesh frontImageUrl={frontImageUrl} w={w} h={h} d={d} autoSpin={autoSpin} binding={binding} />
        <ContactShadows
          position={[0, -h / 2 - 0.01, 0]}
          opacity={0.55}
          scale={w * 4}
          blur={2.6}
          far={2}
          resolution={1024}
        />
      </Suspense>

      <OrbitControls
        enablePan={false}
        minDistance={2}
        maxDistance={6}
        maxPolarAngle={Math.PI * 0.65}
        minPolarAngle={Math.PI * 0.25}
        autoRotate={autoSpin}
        autoRotateSpeed={0.6}
        target={[0, 0, 0]}
      />
    </>
  );
}

export default function Book3DPro({ frontImageUrl, trim, spineWidth = 0.5, binding = "paperback" }) {
  const canvasRef = useRef();
  const [autoSpin, setAutoSpin] = useState(false);
  const profile = BINDING_PROFILES[binding] || BINDING_PROFILES.paperback;

  const capture = () => {
    if (!canvasRef.current) return;
    const canvasEl = canvasRef.current.querySelector("canvas");
    if (!canvasEl) return;
    const dataUrl = canvasEl.toDataURL("image/png");
    const a = document.createElement("a");
    a.href = dataUrl;
    a.download = `sparkprep_mockup_${Date.now()}.png`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  };

  return (
    <div ref={canvasRef} className="relative w-full h-full" data-testid="book-3d-pro">
      <Canvas
        shadows
        dpr={[1, 2]}
        gl={{ preserveDrawingBuffer: true, antialias: true, toneMapping: THREE.ACESFilmicToneMapping }}
        camera={{ position: [2.4, 1.0, 3.6], fov: 30 }}
      >
        <Scene frontImageUrl={frontImageUrl} trim={trim} spineWidth={spineWidth} autoSpin={autoSpin} binding={binding} />
      </Canvas>
      <div className="absolute bottom-3 left-3 flex gap-2" data-testid="book-3d-pro-controls">
        <button
          onClick={() => setAutoSpin((s) => !s)}
          className={`px-3 py-1.5 font-mono-spec text-[10px] tracking-widest uppercase flex items-center gap-1.5 border transition-colors btn-industrial ${autoSpin ? "bg-black text-white border-black" : "bg-white border-neutral-300 hover:border-black"}`}
          data-testid="autospin-toggle"
        >
          <RotateCw className="w-3 h-3" /> {autoSpin ? "Stop Spin" : "Auto Spin"}
        </button>
        <button
          onClick={capture}
          className="px-3 py-1.5 bg-white border border-neutral-300 font-mono-spec text-[10px] tracking-widest uppercase flex items-center gap-1.5 hover:border-black transition-colors btn-industrial"
          data-testid="capture-mockup"
        >
          <Download className="w-3 h-3" /> Save PNG
        </button>
      </div>
      <div className="absolute top-3 left-3 bg-white/80 backdrop-blur px-2.5 py-1 font-mono-spec text-[9px] tracking-widest uppercase text-neutral-600 border border-neutral-200" data-testid="book-3d-pro-hint">
        Drag · Scroll · Material: <span className="text-black" data-testid="material-label">{profile.label}</span>
      </div>
    </div>
  );
}
