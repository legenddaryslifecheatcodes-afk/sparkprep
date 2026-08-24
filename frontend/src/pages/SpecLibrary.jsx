import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, fmtErr } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";
import { toast } from "sonner";
import Nav from "@/components/Nav";
import SeasonBanner from "@/components/SeasonBanner";
import { Ruler, ArrowRight } from "lucide-react";

const PLATFORM_FILTERS = [
  { key: "all", label: "All Distributors" },
  { key: "kdp", label: "Amazon KDP" },
  { key: "ingramspark", label: "IngramSpark" },
  { key: "barnes_noble", label: "B&N Press" },
  { key: "lulu", label: "Lulu" },
];

const REPRESENTATIVE_PAGE_COUNT = 200;

export default function SpecLibrary() {
  const { user } = useAuth();
  const [specs, setSpecs] = useState(null);
  const [filter, setFilter] = useState("all");
  const [loadingKey, setLoadingKey] = useState(null);
  const [spineCache, setSpineCache] = useState({});
  const nav = useNavigate();

  useEffect(() => {
    api.get("/specs").then(({ data }) => setSpecs(data)).catch(() => {});
  }, []);

  useEffect(() => {
    if (!specs) return;
    // Precompute a representative spine width per platform/trim card using
    // that platform's default (first-listed) paper type -- real numbers
    // from the same /specs/spine endpoint the Editor uses, not guesses.
    const platformKeys = filter === "all" ? Object.keys(specs.platforms) : [filter];
    const defaultPaper = Object.keys(specs.paper_types)[0];
    platformKeys.forEach((plat) => {
      Object.keys(specs.trim_sizes).forEach((trim) => {
        const cacheKey = `${plat}:${trim}`;
        if (spineCache[cacheKey]) return;
        api.post("/specs/spine", {
          page_count: REPRESENTATIVE_PAGE_COUNT, paper_type: defaultPaper,
          trim_size: trim, binding: "paperback", platform: plat,
        }).then(({ data }) => {
          setSpineCache((prev) => ({ ...prev, [cacheKey]: data.spine_width }));
        }).catch(() => {});
      });
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [specs, filter]);

  const loadSpec = async (platformKey, trimKey) => {
    if (!user) { nav("/register"); return; }
    const cacheKey = `${platformKey}:${trimKey}`;
    setLoadingKey(cacheKey);
    try {
      const { data } = await api.post("/projects", {
        name: "Untitled Book",
        platform: platformKey,
        trim_size: trimKey,
        paper_type: Object.keys(specs.paper_types)[0],
        binding: "paperback",
        page_count: REPRESENTATIVE_PAGE_COUNT,
        project_type: "cover",
      });
      toast.success(`New project loaded with ${specs.platforms[platformKey].name} · ${specs.trim_sizes[trimKey].label} spec`);
      nav(`/editor/${data.id}`);
    } catch (e) { toast.error(fmtErr(e.response?.data?.detail)); }
    finally { setLoadingKey(null); }
  };

  if (!specs) {
    return (
      <div className="marketing min-h-screen">
        <Nav dark />
        <div className="max-w-6xl mx-auto px-6 py-24 text-center font-mono-spec text-xs text-neutral-500 uppercase tracking-widest">Loading spec library…</div>
      </div>
    );
  }

  const platformKeys = filter === "all" ? Object.keys(specs.platforms) : [filter];

  return (
    <div className="marketing min-h-screen noise-overlay">
      <SeasonBanner />
      <Nav dark />
      <div className="max-w-6xl mx-auto px-6 pt-16 pb-4">
        <span className="font-mono-spec text-xs tracking-widest uppercase text-neutral-500 border border-neutral-700 px-3 py-1.5 inline-flex items-center gap-2" data-testid="spec-library-tag">
          <Ruler className="w-3 h-3" /> [ Distributor Spec Library ]
        </span>
        <h1 className="font-display font-black text-5xl md:text-6xl tracking-tighter mt-6 text-white">
          Every distributor's real specs.
        </h1>
        <p className="mt-4 text-neutral-400 max-w-2xl leading-relaxed">
          The exact trim, bleed, safe zone, paper stock, binding, spine formula and PDF standard your
          cover and interior must match to pass their review. Pick one and every page in the editor auto-conforms.
        </p>
      </div>

      <div className="max-w-6xl mx-auto px-6 flex flex-wrap gap-2" data-testid="spec-library-filters">
        {PLATFORM_FILTERS.map((f) => (
          <button
            key={f.key}
            onClick={() => setFilter(f.key)}
            className={`px-3 py-2 font-mono-spec text-[10px] tracking-widest uppercase border ${filter === f.key ? "bg-white text-black border-white" : "border-neutral-700 text-neutral-400 hover:border-white hover:text-white"}`}
            data-testid={`spec-filter-${f.key}`}
          >
            {f.label}
          </button>
        ))}
      </div>

      <div className="max-w-6xl mx-auto px-6 py-10 grid md:grid-cols-3 gap-4">
        {platformKeys.flatMap((platKey) => {
          const plat = specs.platforms[platKey];
          return Object.entries(specs.trim_sizes).map(([trimKey, trim]) => {
            const cacheKey = `${platKey}:${trimKey}`;
            const spineWidth = spineCache[cacheKey];
            const busy = loadingKey === cacheKey;
            return (
              <div key={cacheKey} className="marketing-surface p-5 flex flex-col" data-testid={`spec-card-${cacheKey}`}>
                <span className="inline-block bg-[#D4A857] text-black font-mono-spec text-[9px] tracking-widest uppercase px-1.5 py-0.5 w-fit">{plat.name}</span>
                <h3 className="font-display font-black text-xl tracking-tight mt-3 text-white">{trim.label}</h3>
                <p className="text-[10px] text-neutral-500 uppercase tracking-widest mt-1">{trim.category} · Paperback</p>

                <div className="mt-4 border border-neutral-800 h-20 flex items-center justify-center bg-black/30">
                  <div className="border border-dashed border-[#D4A857]/60 relative" style={{ width: `${(trim.w / (trim.w + trim.h)) * 100}px`, height: "48px" }}>
                    <div className="absolute inset-1 border border-[#007BFF]/50" />
                  </div>
                </div>

                <div className="grid grid-cols-2 gap-2 mt-4 text-[10px] font-mono-spec tracking-widest text-neutral-500 uppercase">
                  <div>Bleed <span className="text-white block">{plat.bleed}"</span></div>
                  <div>Safe margin <span className="text-white block">{plat.safe_margin_interior}"</span></div>
                  <div>Spine @{REPRESENTATIVE_PAGE_COUNT}pp <span className="text-white block">{spineWidth ? `${spineWidth}"` : "…"}</span></div>
                  <div>Barcode zone <span className="text-white block">{plat.barcode_zone.w}"×{plat.barcode_zone.h}"</span></div>
                  <div>Page range <span className="text-white block">{plat.min_page_count}–{plat.max_page_count}</span></div>
                  <div>Output <span className="text-white block">{plat.pdf_standard}</span></div>
                </div>

                <button
                  onClick={() => loadSpec(platKey, trimKey)}
                  disabled={busy}
                  className="mt-5 flex items-center justify-between border-t border-neutral-800 pt-3 text-xs text-white hover:text-[#D4A857] transition-colors disabled:opacity-50"
                  data-testid={`spec-load-${cacheKey}`}
                >
                  {busy ? "Loading…" : "Load this spec"} <ArrowRight className="w-3.5 h-3.5" />
                </button>
              </div>
            );
          });
        })}
      </div>
    </div>
  );
}
