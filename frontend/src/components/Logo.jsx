/**
 * Legenddary brand lockup — anchored to the official gold+silver "LD" monogram.
 * All rendering uses the master PNG at /brand/legenddary-mark.png (+ /brand/legenddary-logo.png for the full lockup).
 */

export function LogoMark({ size = 32, className = "" }) {
  return (
    <img
      src="/brand/legenddary-mark.png"
      alt="Legenddary"
      width={size}
      height={size}
      className={`inline-block object-contain shrink-0 ${className}`}
      style={{ width: size, height: size }}
      draggable={false}
      data-testid="legenddary-mark"
    />
  );
}

export function Logo({ compact = false, dark = false }) {
  const brandText = dark ? "#F5E6B0" : "#0A0A0A";
  const subText = dark ? "#A9A28E" : "#736C55";
  return (
    <div className="flex items-center gap-3" data-testid="brand-logo">
      <LogoMark size={38} />
      <div className="flex flex-col leading-none">
        <span
          className="font-display font-black text-[19px] tracking-[0.06em]"
          style={{
            color: brandText,
            background: dark
              ? "linear-gradient(180deg, #F5E6B0 0%, #D4AF37 55%, #A47A1E 100%)"
              : "none",
            WebkitBackgroundClip: dark ? "text" : "border-box",
            WebkitTextFillColor: dark ? "transparent" : brandText,
          }}
        >
          LEGENDDARY
        </span>
        {!compact && (
          <span
            className="font-mono-spec text-[9px] tracking-[0.32em] uppercase mt-1"
            style={{ color: subText }}
          >
            SparkPrep · Print Engine
          </span>
        )}
      </div>
    </div>
  );
}

/** SparkPrep's own main logo (distinct from the Legenddary parent-brand mark above) --
 * shown top-center in the nav alongside the existing Legenddary lockup, not in place of it. */
export function SparkPrepLogo({ height = 40, className = "" }) {
  return (
    <img
      src="/brand/sparkprep-logo.png"
      alt="SparkPrep — Print Right. Every Time."
      className={`block object-contain ${className}`}
      style={{ height, width: "auto" }}
      draggable={false}
      data-testid="sparkprep-main-logo"
    />
  );
}

/** Full brand lockup image (mark + "LEGENDDARY" + tagline). Use for hero/marketing/emails. */
export function LogoLockup({ className = "", width }) {
  return (
    <img
      src="/brand/legenddary-logo.png"
      alt="Legenddary — Publish · Automate · Elevate"
      className={`block object-contain ${className}`}
      style={width ? { width, height: "auto" } : undefined}
      draggable={false}
      data-testid="legenddary-lockup"
    />
  );
}
