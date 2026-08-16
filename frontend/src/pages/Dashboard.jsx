import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, fmtErr } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";
import { toast } from "sonner";
import Nav from "@/components/Nav";
import NewProjectDialog from "@/components/NewProjectDialog";
import TemplateReader from "@/components/TemplateReader";
import BrandWatermark from "@/components/BrandWatermark";
import { Plus, FileText, Trash2 } from "lucide-react";

export default function Dashboard() {
  const { user } = useAuth();
  const [projects, setProjects] = useState([]);
  const [loading, setLoading] = useState(true);
  const [open, setOpen] = useState(false);
  const [preset, setPreset] = useState(null);
  const nav = useNavigate();

  const load = async () => {
    setLoading(true);
    try {
      const { data } = await api.get("/projects");
      setProjects(data.projects);
    } catch (e) { toast.error(fmtErr(e.response?.data?.detail)); }
    finally { setLoading(false); }
  };

  useEffect(() => {
    load();
    // Auto-open dialog if arriving from public templates page with a preset
    try {
      const stored = sessionStorage.getItem("sp_preset");
      if (stored) {
        setPreset(JSON.parse(stored));
        setOpen(true);
        sessionStorage.removeItem("sp_preset");
      }
    } catch {}
  }, []);

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
  const bookLimits = { free: 0, author: 1, creator_pro: 3, publisher: 5, studio: 15 };
  const bookLimit = bookLimits[tier] ?? 0;

  return (
    <div className="min-h-screen bg-[#F7F7F9]">
      <Nav dark={false} />
      <div className="max-w-7xl mx-auto px-6 py-12">
        {/* Distributor Spec Templates — Step 01 */}
        <TemplateReader
          onSelect={(t) => { setPreset(t); setOpen(true); }}
        />

        <div className="flex items-start justify-between flex-wrap gap-4 mt-16">
          <div>
            <span className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">[ Step 02 · Workspace ]</span>
            <h1 className="font-display font-black text-5xl tracking-tighter mt-2">Your projects.</h1>
            <p className="text-neutral-600 mt-2">Signed in as <span className="font-mono-spec text-xs">{user?.email}</span></p>
          </div>
          <button onClick={() => { setPreset(null); setOpen(true); }} className="btn-gold px-6 py-3 font-mono-spec text-xs tracking-widest uppercase flex items-center gap-2 btn-industrial" data-testid="new-project-btn">
            <Plus className="w-4 h-4" /> Blank Project
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
                <div key={p.id} className="bg-white border border-neutral-200 p-6 hover:border-black transition-colors group" data-testid={`project-${p.id}`}>
                  <div className="flex items-start justify-between">
                    <div>
                      <span className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">{p.platform} · {p.trim_size}</span>
                      <h3 className="font-display font-bold text-xl tracking-tight mt-2">{p.name}</h3>
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
      <NewProjectDialog open={open} onOpenChange={setOpen} preset={preset} onCreated={(p) => { setOpen(false); setPreset(null); nav(`/editor/${p.id}`); }} />
    </div>
  );
}
