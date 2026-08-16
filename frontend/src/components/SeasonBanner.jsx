import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "@/lib/api";
import { Flame, Timer } from "lucide-react";

/**
 * 99-Day Audit Season banner
 * pre_launch → orange countdown chip
 * active     → prominent orange bar with days remaining
 * closed     → hidden
 */
export default function SeasonBanner({ variant = "bar" }) {
  const [season, setSeason] = useState(null);
  useEffect(() => {
    api.get("/season").then(({ data }) => setSeason(data)).catch(() => {});
  }, []);

  if (!season || season.phase === "closed") return null;

  const isActive = season.phase === "active";

  if (variant === "chip") {
    return (
      <Link to="/audit" className="inline-flex items-center gap-2 border border-[#FF6A00] bg-[#FF6A00]/10 text-[#FF6A00] px-3 py-1.5 font-mono-spec text-[10px] tracking-widest uppercase hover:bg-[#FF6A00]/20 transition-colors" data-testid="season-chip">
        <Flame className="w-3 h-3" />
        {isActive
          ? <><span>99-Day Audit Season · </span><span>{season.days_remaining}d left</span></>
          : <><span>99-Day Audit Season · </span><span>Launches in {season.days_until}d</span></>}
      </Link>
    );
  }

  return (
    <div className="bg-[#FF6A00] text-white" data-testid="season-banner">
      <div className="max-w-7xl mx-auto px-6 py-2.5 flex items-center justify-between gap-4 flex-wrap">
        <div className="flex items-center gap-3">
          <Flame className="w-4 h-4 shrink-0" />
          <div className="font-mono-spec text-[10px] md:text-xs tracking-widest uppercase">
            {isActive ? (
              <>
                <span className="font-bold">99-Day Audit Season is live</span>
                <span className="opacity-80"> · The final {season.days_remaining} days of the year — audit any book for 99¢</span>
              </>
            ) : (
              <>
                <span className="font-bold">99-Day Audit Season launches Sept 23</span>
                <span className="opacity-80"> · In {season.days_until} days · The last 99 days of 2026 for 99¢ audits</span>
              </>
            )}
          </div>
        </div>
        <Link to="/audit" className="font-mono-spec text-[10px] tracking-widest uppercase bg-white text-[#FF6A00] px-3 py-1 hover:bg-neutral-100 transition-colors btn-industrial whitespace-nowrap" data-testid="season-cta">
          {isActive ? "Run Audit →" : "Preview Now →"}
        </Link>
      </div>
    </div>
  );
}
