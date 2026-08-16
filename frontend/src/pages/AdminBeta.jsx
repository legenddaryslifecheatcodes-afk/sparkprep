import { useEffect, useState } from "react";
import { api, fmtErr } from "@/lib/api";
import { toast } from "sonner";
import Nav from "@/components/Nav";
import { Copy, Plus, ShieldOff, Star, Users, KeyRound, ChevronRight, ChevronDown } from "lucide-react";

const STATUS_COLOR = {
  unredeemed: "text-emerald-400 border-emerald-500/30 bg-emerald-500/5",
  active: "text-[#D4AF37] border-[#D4AF37]/40 bg-[#D4AF37]/5",
  consumed: "text-neutral-400 border-neutral-700 bg-neutral-800/40",
  revoked: "text-red-400 border-red-500/40 bg-red-500/5",
};

export default function AdminBeta() {
  const [passes, setPasses] = useState([]);
  const [feedback, setFeedback] = useState([]);
  const [loading, setLoading] = useState(true);
  const [genCount, setGenCount] = useState(10);
  const [genNote, setGenNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [openIdx, setOpenIdx] = useState(null);
  const origin = typeof window !== "undefined" ? window.location.origin : "";

  const load = async () => {
    try {
      const [p, f] = await Promise.all([
        api.get("/admin/beta/passes"),
        api.get("/admin/beta/feedback"),
      ]);
      setPasses(p.data.passes);
      setFeedback(f.data.feedback);
    } catch (e) { toast.error(fmtErr(e.response?.data?.detail)); }
    finally { setLoading(false); }
  };

  useEffect(() => { load(); }, []);

  const generate = async () => {
    setBusy(true);
    try {
      const { data } = await api.post("/admin/beta/generate", { count: Number(genCount), note: genNote });
      toast.success(`Generated ${data.generated} codes`);
      setGenNote("");
      load();
    } catch (e) { toast.error(fmtErr(e.response?.data?.detail)); }
    finally { setBusy(false); }
  };

  const revoke = async (code) => {
    if (!window.confirm(`Revoke ${code}? This cannot be undone.`)) return;
    try {
      await api.post(`/admin/beta/revoke/${code}`);
      toast.success("Revoked");
      load();
    } catch (e) { toast.error(fmtErr(e.response?.data?.detail)); }
  };

  const copy = async (text) => {
    try { await navigator.clipboard.writeText(text); toast.success("Copied"); }
    catch { toast.error("Copy failed"); }
  };

  const stats = {
    total: passes.length,
    unredeemed: passes.filter(p => p.status === "unredeemed").length,
    active: passes.filter(p => p.status === "active").length,
    consumed: passes.filter(p => p.status === "consumed").length,
    revoked: passes.filter(p => p.status === "revoked").length,
    feedback: feedback.length,
  };

  return (
    <div className="marketing min-h-screen">
      <Nav dark />
      <div className="max-w-7xl mx-auto px-6 py-12">
        <span className="font-mono-spec text-[10px] tracking-widest uppercase text-[#D4AF37]">[ Admin · Beta program ]</span>
        <h1 className="font-display font-black text-4xl md:text-5xl tracking-tighter mt-2" data-testid="admin-title">Beta command center.</h1>

        {/* Stat strip */}
        <div className="mt-8 grid md:grid-cols-6 gap-3">
          {[
            { label: "Total passes", val: stats.total, icon: KeyRound },
            { label: "Unredeemed", val: stats.unredeemed, icon: null, accent: "text-emerald-400" },
            { label: "Active testers", val: stats.active, icon: null, accent: "text-[#D4AF37]" },
            { label: "Consumed", val: stats.consumed, icon: null },
            { label: "Revoked", val: stats.revoked, icon: null, accent: "text-red-400" },
            { label: "Feedback rec'd", val: stats.feedback, icon: Star, accent: "text-white" },
          ].map((s, i) => (
            <div key={i} className="marketing-surface p-4" data-testid={`stat-${s.label.replace(/\s/g, '-').toLowerCase()}`}>
              <div className="font-mono-spec text-[9px] tracking-widest uppercase text-neutral-500">{s.label}</div>
              <div className={`font-display font-black text-3xl tracking-tighter mt-2 ${s.accent || ""}`}>{s.val}</div>
            </div>
          ))}
        </div>

        {/* Generate */}
        <div className="mt-10 marketing-surface p-6 border-[#D4AF37]/30" data-testid="generate-panel">
          <div className="flex items-baseline justify-between flex-wrap gap-2">
            <div>
              <span className="font-mono-spec text-[10px] tracking-widest uppercase text-[#D4AF37]">[ Generate more passes ]</span>
              <h2 className="font-display font-black text-2xl tracking-tight mt-1">Mint new beta codes.</h2>
            </div>
          </div>
          <div className="mt-4 grid md:grid-cols-[120px_1fr_auto] gap-3 items-end">
            <div>
              <span className="font-mono-spec text-[9px] tracking-widest uppercase text-neutral-500">Count</span>
              <input
                type="number" min="1" max="200" value={genCount}
                onChange={(e) => setGenCount(e.target.value)}
                className="mt-1 w-full bg-black border border-neutral-800 focus:border-[#D4AF37] px-3 py-2 text-sm text-white outline-none"
                data-testid="gen-count"
              />
            </div>
            <div>
              <span className="font-mono-spec text-[9px] tracking-widest uppercase text-neutral-500">Note (optional)</span>
              <input
                value={genNote} onChange={(e) => setGenNote(e.target.value)}
                placeholder="e.g. Facebook group cohort, Twitter DMs, Reddit r/selfpublish"
                className="mt-1 w-full bg-black border border-neutral-800 focus:border-[#D4AF37] px-3 py-2 text-sm text-white outline-none"
                data-testid="gen-note"
              />
            </div>
            <button onClick={generate} disabled={busy} className="btn-gold px-5 py-2.5 font-mono-spec text-xs tracking-widest uppercase flex items-center gap-2 btn-industrial disabled:opacity-50" data-testid="gen-submit">
              <Plus className="w-3.5 h-3.5" /> {busy ? "Minting…" : "Generate"}
            </button>
          </div>
        </div>

        {/* Passes table */}
        <div className="mt-10">
          <span className="font-mono-spec text-[10px] tracking-widest uppercase text-[#D4AF37]">[ All beta passes ]</span>
          <h2 className="font-display font-black text-2xl tracking-tight mt-1">Passes · {passes.length}</h2>
          {loading ? <p className="mt-4 font-mono-spec text-xs text-neutral-500">Loading…</p> : (
            <div className="mt-4 overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="font-mono-spec text-[9px] tracking-widest uppercase text-neutral-500 border-b border-neutral-800">
                    <th className="text-left py-2 pr-3">Code</th>
                    <th className="text-left py-2 pr-3">Status</th>
                    <th className="text-left py-2 pr-3">Redeemed by</th>
                    <th className="text-left py-2 pr-3">Note</th>
                    <th className="text-right py-2 pl-3">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {passes.map((p) => (
                    <tr key={p.id} className="border-b border-neutral-900/60 hover:bg-neutral-900/30" data-testid={`pass-row-${p.code}`}>
                      <td className="py-3 pr-3 font-mono-spec text-xs tracking-widest">{p.code}</td>
                      <td className="py-3 pr-3">
                        <span className={`px-2 py-1 font-mono-spec text-[9px] tracking-widest uppercase border ${STATUS_COLOR[p.status] || ""}`}>{p.status}</span>
                      </td>
                      <td className="py-3 pr-3 text-neutral-400 text-xs">{p.redeemed_by_email || "—"}</td>
                      <td className="py-3 pr-3 text-neutral-500 text-xs truncate max-w-[220px]">{p.note || "—"}</td>
                      <td className="py-3 pl-3 text-right whitespace-nowrap">
                        <button onClick={() => copy(`${origin}/beta/redeem?code=${p.code}`)} className="inline-flex items-center gap-1 px-2 py-1 font-mono-spec text-[9px] tracking-widest uppercase text-neutral-300 hover:text-white border border-neutral-800 hover:border-neutral-600" data-testid={`copy-link-${p.code}`}>
                          <Copy className="w-3 h-3" /> Link
                        </button>
                        <button onClick={() => copy(p.code)} className="ml-2 inline-flex items-center gap-1 px-2 py-1 font-mono-spec text-[9px] tracking-widest uppercase text-neutral-300 hover:text-white border border-neutral-800 hover:border-neutral-600" data-testid={`copy-code-${p.code}`}>
                          <Copy className="w-3 h-3" /> Code
                        </button>
                        {p.status !== "revoked" && p.status !== "consumed" && (
                          <button onClick={() => revoke(p.code)} className="ml-2 inline-flex items-center gap-1 px-2 py-1 font-mono-spec text-[9px] tracking-widest uppercase text-red-400 border border-red-500/30 hover:bg-red-500/10" data-testid={`revoke-${p.code}`}>
                            <ShieldOff className="w-3 h-3" /> Revoke
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Feedback list */}
        <div className="mt-14">
          <span className="font-mono-spec text-[10px] tracking-widest uppercase text-[#D4AF37]">[ Tester sign-offs ]</span>
          <h2 className="font-display font-black text-2xl tracking-tight mt-1">Feedback · {feedback.length}</h2>
          {feedback.length === 0 ? (
            <p className="mt-4 font-mono-spec text-xs text-neutral-500">No submissions yet.</p>
          ) : (
            <div className="mt-4 space-y-3">
              {feedback.map((fb, i) => {
                const open = openIdx === i;
                const worked = (fb.checklist || []).filter(c => c.status === "worked").length;
                const broke = (fb.checklist || []).filter(c => c.status === "didnt_work").length;
                return (
                  <div key={fb.id} className="marketing-surface" data-testid={`feedback-${fb.id}`}>
                    <button onClick={() => setOpenIdx(open ? null : i)} className="w-full flex items-center justify-between gap-4 p-5 text-left">
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-3 flex-wrap">
                          <span className="font-mono-spec text-xs tracking-widest text-white">{fb.user_email}</span>
                          {fb.pass_code && <span className="font-mono-spec text-[9px] tracking-widest uppercase text-neutral-500">{fb.pass_code}</span>}
                          {fb.would_recommend && <span className="font-mono-spec text-[9px] tracking-widest uppercase text-emerald-400 border border-emerald-500/40 px-1.5 py-0.5">recommends</span>}
                        </div>
                        <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500 mt-2 flex gap-4 flex-wrap">
                          <span className="text-emerald-400">{worked} worked</span>
                          <span className="text-red-400">{broke} broke</span>
                          <span>{new Date(fb.submitted_at).toLocaleString()}</span>
                        </div>
                      </div>
                      {open ? <ChevronDown className="w-4 h-4 text-neutral-500" /> : <ChevronRight className="w-4 h-4 text-neutral-500" />}
                    </button>
                    {open && (
                      <div className="border-t border-neutral-900 p-5 space-y-4">
                        <div>
                          <span className="font-mono-spec text-[9px] tracking-widest uppercase text-neutral-500">Critical review</span>
                          <p className="mt-1 text-sm text-neutral-200 whitespace-pre-wrap">{fb.critical_review || "—"}</p>
                        </div>
                        {fb.public_review && (
                          <div>
                            <span className="font-mono-spec text-[9px] tracking-widest uppercase text-[#D4AF37]">Public review</span>
                            <p className="mt-1 text-sm text-neutral-200 whitespace-pre-wrap">{fb.public_review}</p>
                          </div>
                        )}
                        <div>
                          <span className="font-mono-spec text-[9px] tracking-widest uppercase text-neutral-500">Feature-by-feature</span>
                          <div className="mt-2 space-y-1.5">
                            {(fb.checklist || []).map((c, ci) => (
                              <div key={ci} className="flex items-start gap-3 text-xs">
                                <span className={`font-mono-spec text-[9px] tracking-widest uppercase w-24 shrink-0 ${
                                  c.status === "worked" ? "text-emerald-400" :
                                  c.status === "didnt_work" ? "text-red-400" : "text-neutral-500"
                                }`}>{c.status.replace("_"," ")}</span>
                                <div className="flex-1">
                                  <div className="text-neutral-300">{c.label}</div>
                                  {c.notes && <div className="text-neutral-500 mt-0.5 whitespace-pre-wrap">{c.notes}</div>}
                                </div>
                              </div>
                            ))}
                          </div>
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
