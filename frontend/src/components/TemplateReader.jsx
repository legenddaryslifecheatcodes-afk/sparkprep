import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { ShieldCheck, Ruler, FileWarning } from "lucide-react";

const PLATFORM_META = {
  kdp: { label: "Amazon KDP", color: "#D4AF37", textColor: "#0A0A0A", tagline: "Kindle Direct Publishing" },
  ingramspark: { label: "IngramSpark", color: "#B8933E", textColor: "#FFFFFF", tagline: "Global distribution" },
  barnes_noble: { label: "Barnes & Noble Press", color: "#8A6D24", textColor: "#FFFFFF", tagline: "B&N retail" },
  lulu: { label: "Lulu", color: "#E5C158", textColor: "#0A0A0A", tagline: "Print-on-demand" },
};

// Distributor spec templates — each entry is a compliance blueprint (trim + paper + binding + page count)
// exactly matching that publisher's requirements. NOT visual design templates.
const SPEC_TEMPLATES = [
  { platform: "kdp", trim: "6x9", paper: "cream_50lb", binding: "paperback", pages: 300, spec_id: "KDP-6X9-PB" },
  { platform: "kdp", trim: "5.5x8.5", paper: "cream_50lb", binding: "paperback", pages: 240, spec_id: "KDP-5.5X8.5-PB" },
  { platform: "kdp", trim: "8.5x11", paper: "white_50lb", binding: "paperback", pages: 80, spec_id: "KDP-LTR-PB" },
  { platform: "kdp", trim: "7x10", paper: "color_60lb_premium", binding: "paperback", pages: 150, spec_id: "KDP-7X10-COLOR" },
  { platform: "ingramspark", trim: "6x9", paper: "white_50lb", binding: "paperback", pages: 280, spec_id: "IS-6X9-PB" },
  { platform: "ingramspark", trim: "5.25x8", paper: "cream_50lb", binding: "hardcover_case", pages: 320, spec_id: "IS-5.25X8-HC" },
  { platform: "ingramspark", trim: "6.14x9.21", paper: "white_60lb", binding: "hardcover_jacket", pages: 400, spec_id: "IS-6.14X9.21-JKT" },
  { platform: "barnes_noble", trim: "5x8", paper: "cream_50lb", binding: "paperback", pages: 200, spec_id: "BN-5X8-PB" },
  { platform: "lulu", trim: "6x9", paper: "cream_50lb", binding: "paperback", pages: 260, spec_id: "LULU-6X9-PB" },
];

export default function TemplateReader({ onSelect }) {
  const [specs, setSpecs] = useState(null);
  const [activePlatform, setActivePlatform] = useState("all");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.get("/specs")
      .then(({ data }) => setSpecs(data))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const list = SPEC_TEMPLATES.filter((t) => activePlatform === "all" || t.platform === activePlatform);

  if (loading) return <div className="font-mono-spec text-xs text-neutral-500">Loading distributor specs…</div>;

  return (
    <section className="bg-white border border-neutral-200" data-testid="template-reader">
      <div className="p-6 border-b border-neutral-200">
        <div className="flex items-start justify-between flex-wrap gap-4">
          <div className="max-w-2xl">
            <div className="inline-flex items-center gap-2 px-2 py-1 bg-neutral-900 text-white font-mono-spec text-[10px] tracking-widest uppercase">
              <ShieldCheck className="w-3 h-3" /> Distributor Spec Library
            </div>
            <h2 className="font-display font-black text-3xl md:text-4xl tracking-tighter mt-3">Start from a publisher's blueprint.</h2>
            <p className="text-sm text-neutral-600 mt-3 leading-relaxed">
              These are not cover design templates. Each one is a <span className="font-mono-spec text-xs">technical spec sheet</span> pulled directly from the distributor — the exact trim, bleed, safe zone, paper stock, binding, spine formula and PDF standard your cover and interior must match to pass their review. Pick one and every page in the editor auto-conforms.
            </p>
          </div>
          <div className="bg-amber-50 border border-amber-200 p-3 max-w-sm">
            <div className="flex items-start gap-2">
              <FileWarning className="w-4 h-4 text-amber-700 shrink-0 mt-0.5" />
              <div className="font-mono-spec text-[10px] tracking-widest uppercase text-amber-800 leading-relaxed">
                Cover design templates (title layouts, art) are a separate feature — coming soon. These are strictly the compliance blueprints.
              </div>
            </div>
          </div>
        </div>

        <div className="flex flex-wrap gap-1 mt-6" data-testid="platform-filter">
          {[
            { k: "all", label: "All Distributors" },
            { k: "kdp", label: "Amazon KDP" },
            { k: "ingramspark", label: "IngramSpark" },
            { k: "barnes_noble", label: "B&N Press" },
            { k: "lulu", label: "Lulu" },
          ].map((f) => (
            <button
              key={f.k}
              onClick={() => setActivePlatform(f.k)}
              className={`px-3 py-1.5 font-mono-spec text-[10px] tracking-widest uppercase border transition-colors btn-industrial ${
                activePlatform === f.k
                  ? "bg-black text-white border-black"
                  : "bg-white text-neutral-600 border-neutral-300 hover:border-black"
              }`}
              data-testid={`platform-filter-${f.k}`}
            >
              {f.label}
            </button>
          ))}
        </div>
      </div>

      <div className="grid md:grid-cols-3 lg:grid-cols-3 gap-0 border-b border-neutral-200">
        {list.map((t, i) => {
          const trim = specs?.trim_sizes[t.trim];
          const paper = specs?.paper_types[t.paper];
          const plat = specs?.platforms[t.platform];
          const meta = PLATFORM_META[t.platform];
          const spineWidth = paper?.ppi ? (t.pages / paper.ppi).toFixed(3) : "—";
          return (
            <button
              key={i}
              onClick={() => onSelect?.(t)}
              className="text-left p-6 hover:bg-neutral-50 transition-colors group border-neutral-200 border-r border-b flex flex-col justify-between min-h-[300px]"
              data-testid={`template-card-${i}`}
            >
              <div>
                {/* Platform badge */}
                <div className="flex items-center gap-2">
                  <span className="font-mono-spec text-[9px] tracking-widest uppercase px-1.5 py-0.5" style={{ background: meta?.color, color: meta?.textColor || "#0A0A0A" }}>
                    {meta?.label}
                  </span>
                  <span className="font-mono-spec text-[9px] tracking-widest uppercase text-neutral-400">{t.spec_id}</span>
                </div>

                {/* Spec headline */}
                <h3 className="font-display font-black text-2xl tracking-tighter mt-3 leading-tight">
                  {trim?.label?.replace(/["]/g, '"') || t.trim}
                </h3>
                <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500 mt-1">
                  {t.binding.replace("_", " ")} · {paper?.label?.split("(")[0]?.trim()}
                </div>

                {/* Blueprint diagram */}
                <div className="mt-4 flex items-center gap-3">
                  <div className="relative" style={{ aspectRatio: `${trim?.w || 6}/${trim?.h || 9}`, width: 50 }}>
                    <div className="absolute inset-0 border-2 border-neutral-300" />
                    <div className="absolute" style={{ inset: "8%", border: "1px dashed #EC008C" }} title="Trim" />
                    <div className="absolute" style={{ inset: "18%", border: "1px dotted #FFF200", background: "rgba(255,242,0,0.05)" }} title="Safe zone" />
                  </div>
                  <div className="font-mono-spec text-[10px] leading-relaxed text-neutral-600">
                    <div><span className="text-neutral-400">BLEED</span> {plat?.bleed || 0.125}"</div>
                    <div><span className="text-neutral-400">SAFE</span> {plat?.safe_margin_interior || 0.375}"</div>
                  </div>
                </div>
              </div>

              {/* Compliance specs */}
              <div className="mt-5 pt-4 border-t border-neutral-100 space-y-1 font-mono-spec text-[10px] tracking-widest uppercase">
                <div className="flex justify-between text-neutral-600"><span>Spine formula</span><span className="text-neutral-900">{spineWidth}"</span></div>
                <div className="flex justify-between text-neutral-600"><span>Paper PPI</span><span className="text-neutral-900">{paper?.ppi}</span></div>
                <div className="flex justify-between text-neutral-600"><span>Barcode zone</span><span className="text-neutral-900">{plat?.barcode_zone?.w}×{plat?.barcode_zone?.h}"</span></div>
                <div className="flex justify-between text-neutral-600"><span>Output</span><span className="text-neutral-900">{plat?.pdf_standard}</span></div>
              </div>

              <div className="mt-4 pt-3 border-t border-neutral-100 font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500 group-hover:text-black transition-colors flex items-center justify-between">
                <span>Load this spec</span>
                <span aria-hidden>→</span>
              </div>
            </button>
          );
        })}
      </div>
    </section>
  );
}
