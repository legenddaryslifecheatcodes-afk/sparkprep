import { useState, useRef, useEffect } from "react";

export default function Book3D({ frontImageUrl, trim, spineWidth = 0.5 }) {
  const [rotY, setRotY] = useState(-25);
  const [rotX, setRotX] = useState(-5);
  const dragRef = useRef(null);
  const dragging = useRef(false);
  const start = useRef({ x: 0, y: 0, rotY: -25, rotX: -5 });

  const onDown = (e) => {
    dragging.current = true;
    const pt = e.touches ? e.touches[0] : e;
    start.current = { x: pt.clientX, y: pt.clientY, rotY, rotX };
  };
  const onMove = (e) => {
    if (!dragging.current) return;
    const pt = e.touches ? e.touches[0] : e;
    const dx = pt.clientX - start.current.x;
    const dy = pt.clientY - start.current.y;
    setRotY(start.current.rotY + dx * 0.4);
    setRotX(Math.max(-30, Math.min(30, start.current.rotX - dy * 0.3)));
  };
  const onUp = () => { dragging.current = false; };

  useEffect(() => {
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    window.addEventListener("touchmove", onMove);
    window.addEventListener("touchend", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      window.removeEventListener("touchmove", onMove);
      window.removeEventListener("touchend", onUp);
    };
    // eslint-disable-next-line
  }, []);

  const w = trim?.w || 6;
  const h = trim?.h || 9;
  const scale = 40; // px per inch — controls display size
  const bookW = w * scale;
  const bookH = h * scale;
  const bookD = (spineWidth || 0.5) * scale;

  return (
    <div
      ref={dragRef}
      onMouseDown={onDown}
      onTouchStart={onDown}
      className="w-full h-full flex items-center justify-center cursor-grab active:cursor-grabbing select-none"
      style={{ perspective: "1400px" }}
      data-testid="book-3d"
    >
      <div
        className="relative"
        style={{
          width: `${bookW}px`,
          height: `${bookH}px`,
          transformStyle: "preserve-3d",
          transform: `rotateX(${rotX}deg) rotateY(${rotY}deg)`,
          transition: dragging.current ? "none" : "transform 0.5s cubic-bezier(.2,.8,.2,1)",
        }}
      >
        {/* Front cover */}
        <div
          className="absolute inset-0 bg-white shadow-2xl border border-neutral-300 overflow-hidden"
          style={{ transform: `translateZ(${bookD / 2}px)` }}
        >
          {frontImageUrl ? (
            <img src={frontImageUrl} alt="cover" className="w-full h-full object-cover" />
          ) : (
            <div className="w-full h-full flex items-center justify-center bg-gradient-to-br from-neutral-800 to-neutral-950 text-neutral-500 font-mono-spec text-[10px] tracking-widest uppercase">No cover</div>
          )}
        </div>
        {/* Back cover */}
        <div
          className="absolute inset-0 bg-neutral-800 border border-neutral-400"
          style={{ transform: `rotateY(180deg) translateZ(${bookD / 2}px)` }}
        />
        {/* Spine */}
        <div
          className="absolute top-0 left-0 h-full bg-gradient-to-r from-neutral-950 via-neutral-800 to-neutral-950 flex items-center justify-center"
          style={{
            width: `${bookD}px`,
            transform: `rotateY(-90deg) translateZ(${bookW / 2}px)`,
            transformOrigin: "left center",
          }}
        >
          <div className="font-display font-bold text-white text-[8px] tracking-widest uppercase" style={{ writingMode: "vertical-rl" }}>SparkPrep</div>
        </div>
        {/* Right edge (opposite spine) */}
        <div
          className="absolute top-0 right-0 h-full bg-neutral-100"
          style={{
            width: `${bookD}px`,
            transform: `rotateY(90deg) translateZ(${bookW / 2 - bookD}px)`,
            transformOrigin: "right center",
          }}
        >
          {/* Page edges */}
          <div className="w-full h-full" style={{ background: "repeating-linear-gradient(0deg, #fff, #fff 1px, #e5e5e5 1px, #e5e5e5 2px)" }} />
        </div>
        {/* Top */}
        <div
          className="absolute top-0 left-0 w-full bg-neutral-100"
          style={{
            height: `${bookD}px`,
            transform: `rotateX(90deg) translateZ(${bookH / 2 - bookD}px)`,
            transformOrigin: "top center",
            background: "repeating-linear-gradient(90deg, #fff, #fff 1px, #e5e5e5 1px, #e5e5e5 2px)",
          }}
        />
        {/* Bottom */}
        <div
          className="absolute bottom-0 left-0 w-full bg-neutral-100"
          style={{
            height: `${bookD}px`,
            transform: `rotateX(-90deg) translateZ(${bookH / 2 - bookD}px)`,
            transformOrigin: "bottom center",
            background: "repeating-linear-gradient(90deg, #fff, #fff 1px, #e5e5e5 1px, #e5e5e5 2px)",
          }}
        />
      </div>
    </div>
  );
}
