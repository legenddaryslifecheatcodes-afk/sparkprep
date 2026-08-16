/**
 * Atmospheric Legenddary LD monogram watermark for page heros / empty states.
 * - variant="dark"  → uses screen blend + gold glow on the black-bg mark (best on matte-black surfaces)
 * - variant="light" → uses the transparent-bg mark at low opacity (best on cream/paper surfaces)
 * - position: hero-left | hero-right | center | corner-tr
 * - scale: fractional viewport width, capped in pixels
 */
export default function BrandWatermark({
  variant = "dark",
  position = "hero-left",
  scale = 0.6,
  maxPx = 780,
  opacity,
  vignette = true,
}) {
  const isDark = variant === "dark";
  const src = isDark
    ? "/brand/legenddary-mark.png"
    : "/brand/legenddary-mark-transparent.png";

  const layoutClass = {
    "hero-left": "inset-y-0 left-0 right-1/3 justify-center items-center",
    "hero-right": "inset-y-0 left-1/3 right-0 justify-center items-center",
    "center": "inset-0 justify-center items-center",
    "corner-tr": "top-0 right-0 w-1/3 h-1/3 justify-end items-start",
  }[position];

  const finalOpacity =
    opacity != null ? opacity : isDark ? 0.28 : 0.08;

  const imgStyle = {
    width: `min(${scale * 100}vw, ${maxPx}px)`,
    height: "auto",
    transform:
      position === "hero-left"
        ? "translate(8%, 2%)"
        : position === "hero-right"
        ? "translate(-8%, 2%)"
        : position === "corner-tr"
        ? "translate(20%, -10%)"
        : "none",
    mixBlendMode: isDark ? "screen" : "normal",
    opacity: finalOpacity,
    filter: isDark
      ? "drop-shadow(0 0 120px rgba(212,175,55,0.35))"
      : "none",
  };

  const vignetteStyle = isDark
    ? {
        background:
          "radial-gradient(1400px 900px at 32% 50%, rgba(10,10,10,0) 0%, rgba(10,10,10,0.55) 65%, rgba(10,10,10,0.92) 100%)",
      }
    : {
        background:
          "radial-gradient(900px 700px at 30% 45%, rgba(247,247,249,0) 0%, rgba(247,247,249,0.55) 70%, rgba(247,247,249,0.92) 100%)",
      };

  return (
    <>
      <div
        aria-hidden="true"
        className={`pointer-events-none absolute flex overflow-hidden ${layoutClass}`}
      >
        <img
          src={src}
          alt=""
          className="select-none"
          style={imgStyle}
          draggable={false}
          data-testid="brand-watermark"
        />
      </div>
      {vignette && (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-0"
          style={vignetteStyle}
        />
      )}
    </>
  );
}
