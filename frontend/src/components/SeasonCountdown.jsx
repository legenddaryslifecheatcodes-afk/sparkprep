import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Flame } from "lucide-react";

/**
 * Bold hero countdown for the 99-Day Audit Season on /audit.
 * Only rendered during pre_launch phase.
 */
export default function SeasonCountdown() {
  const [season, setSeason] = useState(null);
  const [now, setNow] = useState(new Date());

  useEffect(() => {
    api.get("/season").then(({ data }) => setSeason(data)).catch(() => {});
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  if (!season || season.phase !== "pre_launch") return null;

  const start = new Date(season.start);
  const diff = Math.max(0, start - now);
  const days = Math.floor(diff / (1000 * 60 * 60 * 24));
  const hours = Math.floor((diff / (1000 * 60 * 60)) % 24);
  const minutes = Math.floor((diff / (1000 * 60)) % 60);
  const seconds = Math.floor((diff / 1000) % 60);

  const cells = [
    { label: "Days", value: days },
    { label: "Hrs", value: hours },
    { label: "Min", value: minutes },
    { label: "Sec", value: seconds },
  ];

  return (
    <div className="marketing-surface border-[#FF6A00]/40 bg-gradient-to-br from-[#FF6A00]/[0.08] via-transparent to-[#D4AF37]/[0.06] p-6" data-testid="season-countdown">
      <div className="flex items-center gap-2">
        <Flame className="w-4 h-4 text-[#FF6A00]" />
        <span className="font-mono-spec text-[10px] tracking-widest uppercase text-[#FF6A00]">99-Day Audit Season · Pre-Launch</span>
      </div>
      <h3 className="font-display font-black text-2xl md:text-3xl tracking-tighter mt-3 text-white">
        Launches Sept 23. The last 99 days of 2026 for 99¢ audits.
      </h3>
      <div className="mt-6 grid grid-cols-4 gap-2 md:gap-3" data-testid="countdown-cells">
        {cells.map((c, i) => (
          <div key={c.label} className="bg-black border border-[#FF6A00]/30 p-3 md:p-5 text-center relative overflow-hidden" data-testid={`countdown-${c.label.toLowerCase()}`}>
            <div className="font-display font-black text-3xl md:text-5xl tracking-tighter tabular-nums" style={{ background: "linear-gradient(180deg, #E5C158, #D4AF37 55%, #B8933E)", WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent" }}>
              {String(c.value).padStart(2, "0")}
            </div>
            <div className="font-mono-spec text-[9px] tracking-widest uppercase text-neutral-500 mt-1">{c.label}</div>
          </div>
        ))}
      </div>
      <p className="mt-5 text-sm text-neutral-400 leading-relaxed">
        Every year, the last 99 days spike with author panic, rejection notices and rushed submissions.
        SparkPrep's annual audit season answers with clarity — pinpoint the failure, cite the rule, ship the fix.
      </p>
    </div>
  );
}
