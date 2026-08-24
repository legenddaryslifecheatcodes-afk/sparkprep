import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, fmtErr, API_URL } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";
import { toast } from "sonner";
import Nav from "@/components/Nav";
import NewProjectDialog from "@/components/NewProjectDialog";
import BrandWatermark from "@/components/BrandWatermark";
import { Plus, FileText, Trash2, Layers, ShieldAlert, PackageCheck, Download, ChevronDown } from "lucide-react";

export default function Dashboard() {
  const { user } = useAuth();
  const [projects, setProjects] = useState([]);
  const [loading, setLoading] = useState(true);
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState(() => new Set());
  const [batchBusy, setBatchBusy] = useState(false);
  const [batchResult, setBatchResult] = useState(null);
  const [series, setSeries] = useState([]);
  const [seriesOpen, setSeriesOpen] = useState(null);
  const [seriesFindings, setSeriesFindings] = useState(null);
  const nav = useNavigate();

  const load = async () => {
    setLoading(true);
    try {
      const { data } = await api.get("/projects");
      setProjects(data.projects);
    } catch (e) { toast.error(fmtErr(e.response?.data?.detail)); }
    finally { setLoading(false); }
    api.get("/series").then(({ data }) => setSeries(data.series)).catch(() => {});
  };

  useEffect(() => { load(); }, []);

  const toggleSelect = (id) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const runBatch = async (kind) => {
    if (selected.size < 2) { toast.error("Select at least 2 books first"); return; }
    setBatchBusy(true);
    setBatchResult(null);
    try {
      const { data } = await api.post(`/projects/batch-${kind}`, { project_ids: Array.from(selected) });
      setBatchResult({ kind, data });
      if (kind === "export" && data.download_url) {
        const token = localStorage.getItem("sp_token");
        window.open(`${API_URL}${data.download_url.replace("/api", "")}${token ? `?token=${token}` : ""}`, "_blank");
      }
      toast.success(kind === "export" ? "Batch export ready" : "Batch audit complete");
    } catch (e) { toast.error(fmtErr(e.response?.data?.detail)); }
    finally { setBatchBusy(false); }
  };

  const checkSeries = async (name) => {
    if (seriesOpen === name) { setSeriesOpen(null); return; }
    setSeriesOpen(name);
    setSeriesFindings(null);
    try {
      const { data } = await api.get(`/series/${encodeURIComponent(name)}/consistency`);
      setSeriesFindings(data.findings);
    } catch (e) { toast.error(fmtErr(e.response?.data?.detail)); }
  };

  const remove = async (id) => {
    if (!window.confirm("Delete this project?")) return;
    try {
      await api.delete(`/projects/${id}`);
      setProjects(projects.filter(p => p.id !== id));
      toast.success("Deleted");
    } catch (e) { toast.error(fmtErr(e.response?.data?.detail)); }
  };

  const tier = user?.tier || "free";
  const used = user?.exports_this_month ?? 0;
  const limits = { free: 3, author: 15, creator_pro: 45, publisher: 100, studio: 300 };
  const limit = limits[tier] || 3;
  const booksUsed = user?.books_this_month ?? 0;
  const bookLimits = { free: 0, author: 1, creator_pro: 3, publisher: 7, studio: 30 };
  const bookLimit = bookLimits[tier] ?? 0;
  const batchEnabled = tier === "publisher" || tier === "studio";

  return (
    <div className="min-h-screen bg-[#F7F7F9]">
      <Nav dark={false} />
      <div className="max-w-7xl mx-auto px-6 py-12">
        <div className="flex items-start justify-between flex-wrap gap-4">
          <div>
            <span className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">[ Workspace ]</span>
            <h1 className="font-display font-black text-5xl tracking-tighter mt-2">Your projects.</h1>
            <p className="text-neutral-600 mt-2">Signed in as <span className="font-mono-spec text-xs">{user?.email}</span></p>
          </div>
          <button onClick={() => setOpen(true)} className="btn-gold px-6 py-3 font-mono-spec text-xs tracking-widest uppercase flex items-center gap-2 btn-industrial" data-testid="new-project-btn">
            <Plus className="w-4 h-4" /> New Project
          </button>
        </div>

        {/* Usage */}
        <div className="grid md:grid-cols-5 gap-4 mt-10">
          <div className="bg-white border border-neutral-200 p-6" data-testid="stat-tier">
            <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Plan</div>
            <div className="font-display font-black text-2xl mt-2 tracking-tight capitalize">{tier.replace("_", " ")}</div>
            <Link to="/pricing" className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500 hover:text-black mt-4 inline-block">Upgrade →</Link>
          </div>
          <div className="bg-white border border-neutral-200 p-6" data-testid="stat-books">
            <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Books this month</div>
            <div className="font-display font-black text-2xl mt-2 tracking-tight">{booksUsed} <span className="text-neutral-400 font-normal">/ {bookLimit || "—"}</span></div>
            <div className="mt-3 h-1 bg-neutral-100">
              <div className="h-full bg-[#D4AF37]" style={{ width: `${bookLimit > 0 ? Math.min(100, (booksUsed / bookLimit) * 100) : 0}%` }} />
            </div>
            {bookLimit === 0 && <div className="mt-2 font-mono-spec text-[9px] tracking-widest uppercase text-neutral-500">Upgrade to print books</div>}
          </div>
          <div className="bg-white border border-neutral-200 p-6" data-testid="stat-exports">
            <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Exports this month</div>
            <div className="font-display font-black text-2xl mt-2 tracking-tight">{used} <span className="text-neutral-400 font-normal">/ {limit}</span></div>
            <div className="mt-3 h-1 bg-neutral-100">
              <div className="h-full bg-[#D4AF37]" style={{ width: `${Math.min(100, (used/limit)*100)}%` }} />
            </div>
          </div>
          <div className="bg-white border border-neutral-200 p-6" data-testid="stat-projects">
            <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Projects</div>
            <div className="font-display font-black text-2xl mt-2 tracking-tight">{projects.length}</div>
          </div>
          <div className="bg-white border border-neutral-200 p-6" data-testid="stat-platforms">
            <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Platforms unlocked</div>
            <div className="font-display font-black text-2xl mt-2 tracking-tight">{tier === "free" ? 2 : 4}</div>
          </div>
        </div>

        {/* Series consistency */}
        {series.length > 0 && (
          <div className="mt-10 bg-white border border-neutral-200" data-testid="series-panel">
            <div className="p-4 border-b border-neutral-200 flex items-center gap-2">
              <Layers className="w-4 h-4 text-neutral-500" />
              <span className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Series consistency</span>
            </div>
            <div className="divide-y divide-neutral-100">
              {series.map((s) => (
                <div key={s.name}>
                  <button
                    onClick={() => checkSeries(s.name)}
                    className="w-full flex items-center justify-between px-4 py-3 hover:bg-neutral-50 text-left"
                    data-testid={`series-row-${s.name}`}
                  >
                    <span className="text-sm font-medium">{s.name} <span className="text-neutral-400 font-normal">· {s.book_count} books</span></span>
                    <ChevronDown className={`w-4 h-4 text-neutral-400 transition-transform ${seriesOpen === s.name ? "rotate-180" : ""}`} />
                  </button>
                  {seriesOpen === s.name && (
                    <div className="px-4 pb-4">
                      {seriesFindings === null ? (
                        <p className="text-xs text-neutral-500">Checking…</p>
                      ) : seriesFindings.length === 0 ? (
                        <p className="text-xs text-emerald-700">No consistency issues found across this series.</p>
                      ) : (
                        <div className="space-y-2">
                          {seriesFindings.map((f, i) => (
                            <div key={i} className={`border p-3 text-xs ${f.severity === "fail" ? "border-red-200 bg-red-50" : "border-amber-200 bg-amber-50"}`} data-testid={`series-finding-${f.id}`}>
                              <div className="font-semibold">{f.title}</div>
                              <div className="text-neutral-600 mt-1">{f.why_it_fails}</div>
                              <div className="text-neutral-500 mt-1">Odd one out: {f.outliers.map(o => o.name).join(", ")}</div>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Batch audit + batch export (Publisher/Studio) */}
        {batchEnabled && projects.length >= 2 && (
          <div className="mt-6 bg-white border border-neutral-200 p-4 flex flex-wrap items-center justify-between gap-3" data-testid="batch-toolbar">
            <div className="text-xs text-neutral-600">
              <span className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500 mr-2">Bulk audit + batch export</span>
              {selected.size > 0 ? `${selected.size} book${selected.size === 1 ? "" : "s"} selected` : "Select books below with the checkbox to run these"}
            </div>
            <div className="flex gap-2">
              <button onClick={() => runBatch("audit")} disabled={batchBusy || selected.size < 2} className="px-4 py-2 border border-neutral-300 font-mono-spec text-[10px] tracking-widest uppercase hover:border-black disabled:opacity-40 flex items-center gap-1.5 btn-industrial" data-testid="batch-audit-btn">
                <ShieldAlert className="w-3.5 h-3.5" /> Batch Audit
              </button>
              <button onClick={() => runBatch("export")} disabled={batchBusy || selected.size < 2} className="px-4 py-2 bg-black text-white font-mono-spec text-[10px] tracking-widest uppercase hover:bg-neutral-800 disabled:opacity-40 flex items-center gap-1.5 btn-industrial" data-testid="batch-export-btn">
                <PackageCheck className="w-3.5 h-3.5" /> {batchBusy ? "Working…" : "Batch Export"}
              </button>
            </div>
          </div>
        )}
        {batchResult?.kind === "audit" && (
          <div className="mt-3 bg-white border border-neutral-200 p-4 space-y-2" data-testid="batch-audit-results">
            {batchResult.data.results.map((r) => (
              <div key={r.project_id} className="flex items-center justify-between text-xs border-b border-neutral-100 pb-2 last:border-0">
                <span>{r.name || r.project_id}</span>
                {r.ok ? (
                  <span className={r.critical_failures > 0 ? "text-red-600" : "text-emerald-700"}>
                    {r.critical_failures} critical · {r.warnings} warning{r.warnings === 1 ? "" : "s"}
                  </span>
                ) : (
                  <span className="text-red-600">{r.error}</span>
                )}
              </div>
            ))}
          </div>
        )}
        {batchResult?.kind === "export" && batchResult.data.zip_name && (
          <div className="mt-3 bg-white border border-neutral-200 p-4 flex items-center justify-between text-xs" data-testid="batch-export-results">
            <span>{batchResult.data.results.filter(r => r.ok).length} of {batchResult.data.results.length} books exported</span>
            <a
              href={`${API_URL}${batchResult.data.download_url.replace("/api", "")}?token=${localStorage.getItem("sp_token") || ""}`}
              className="flex items-center gap-1.5 underline hover:no-underline"
            >
              <Download className="w-3.5 h-3.5" /> Download zip again
            </a>
          </div>
        )}

        {/* Projects grid */}
        <div className="mt-12">
          {loading ? (
            <p className="font-mono-spec text-xs text-neutral-500">Loading…</p>
          ) : projects.length === 0 ? (
            <div className="relative overflow-hidden border border-dashed border-neutral-300 p-16 text-center bg-white" data-testid="empty-state">
              <BrandWatermark variant="light" position="center" scale={0.32} maxPx={340} opacity={0.09} vignette={false} />
              <div className="relative">
                <FileText className="w-8 h-8 mx-auto text-neutral-400" />
                <p className="font-display font-bold text-2xl tracking-tight mt-4">No projects yet</p>
                <p className="text-neutral-500 mt-2">Kick off your first print-ready book in under a minute.</p>
                <button onClick={() => setOpen(true)} className="mt-6 bg-black text-white px-6 py-3 font-mono-spec text-xs tracking-widest uppercase hover:bg-neutral-800 btn-industrial" data-testid="empty-new-project">Create Project</button>
              </div>
            </div>
          ) : (
            <div className="grid md:grid-cols-3 gap-4">
              {projects.map(p => (
                <div key={p.id} className={`bg-white border p-6 transition-colors group ${selected.has(p.id) ? "border-black" : "border-neutral-200 hover:border-black"}`} data-testid={`project-${p.id}`}>
                  <div className="flex items-start justify-between">
                    <div className="flex items-start gap-3">
                      {batchEnabled && (
                        <input
                          type="checkbox"
                          checked={selected.has(p.id)}
                          onChange={() => toggleSelect(p.id)}
                          className="mt-1.5"
                          data-testid={`select-${p.id}`}
                        />
                      )}
                      <div>
                        <span className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">{p.platform} · {p.trim_size}{p.series_name ? ` · ${p.series_name}` : ""}</span>
                        <h3 className="font-display font-bold text-xl tracking-tight mt-2">{p.name}</h3>
                      </div>
                    </div>
                    <button onClick={() => remove(p.id)} className="opacity-0 group-hover:opacity-100 transition-opacity text-neutral-400 hover:text-red-500" data-testid={`delete-${p.id}`}>
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </div>
                  <div className="grid grid-cols-2 gap-2 mt-6 text-[10px] font-mono-spec tracking-widest text-neutral-500 uppercase">
                    <div>Pages · {p.page_count}</div>
                    <div>Type · {p.project_type}</div>
                    <div>Paper · {p.paper_type.split("_")[0]}</div>
                    <div>Bind · {p.binding.split("_")[0]}</div>
                  </div>
                  <button onClick={() => nav(`/editor/${p.id}`)} className="mt-6 w-full border border-black py-2 font-mono-spec text-xs tracking-widest uppercase hover:bg-black hover:text-white transition-colors btn-industrial" data-testid={`open-${p.id}`}>
                    Open Editor →
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
      <NewProjectDialog open={open} onOpenChange={setOpen} onCreated={(p) => { setOpen(false); nav(`/editor/${p.id}`); }} />
    </div>
  );
}
