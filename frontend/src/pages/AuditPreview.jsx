import { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { api, fmtErr } from "@/lib/api";
import { toast } from "sonner";
import Nav from "@/components/Nav";
import { Check, AlertTriangle, XCircle, Lock, Sparkles } from "lucide-react";

const sevIcon = (s) => s === "pass" ? <Check className="w-4 h-4" /> : s === "warning" ? <AlertTriangle className="w-4 h-4" /> : <XCircle className="w-4 h-4" />;
const sevClass = (s) => s === "pass" ? "text-emerald-400 border-emerald-400/30 bg-emerald-500/10" : s === "warning" ? "text-amber-400 border-amber-400/30 bg-amber-500/10" : "text-red-400 border-red-400/30 bg-red-500/10";

export default function AuditPreview() {
  const { id } = useParams();
  const [audit, setAudit] = useState(null);
  const [checkingOut, setCheckingOut] = useState(false);
  const nav = useNavigate();

  useEffect(() => {
    api.get(`/audit/${id}`).then(({ data }) => setAudit(data)).catch(() => nav("/audit"));
  }, [id]);

  const unlock = async () => {
    setCheckingOut(true);
    try {
      const { data } = await api.post(`/audit/${id}/checkout`, { origin_url: window.location.origin });
      window.location.href = data.checkout_url;
    } catch (e) {
      toast.error(fmtErr(e.response?.data?.detail) || "Checkout failed");
      setCheckingOut(false);
    }
  };

  if (!audit) return <div className="marketing min-h-screen"><Nav dark /><div className="p-10 font-mono-spec text-xs text-neutral-500">Loading…</div></div>;
  const s = audit.summary || {};
  const riskColor = s.rejection_risk === "high" ? "text-red-400 border-red-400/40" : s.rejection_risk === "medium" ? "text-amber-400 border-amber-400/40" : s.rejection_risk === "low" ? "text-yellow-300 border-yellow-300/40" : "text-emerald-400 border-emerald-400/40";

  return (
    <div className="marketing min-h-screen noise-overlay">
      <Nav dark />
      <div className="max-w-5xl mx-auto px-6 py-12">
        <span className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">[ Preview · Audit {id.slice(0, 8)} ]</span>
        <h1 className="font-display font-black text-4xl md:text-6xl tracking-tighter mt-3" data-testid="preview-title">
          {s.critical_failures > 0 ? "This won't pass." : s.warnings > 0 ? "This might squeak by." : "This looks clean."}
        </h1>
        <p className="text-neutral-400 mt-3">Auditing against <span className="font-mono-spec text-white">{audit.platform_name}</span> · trim <span className="font-mono-spec text-white">{audit.trim_label}</span> · file <span className="font-mono-spec text-white">{audit.file_metadata?.original_filename}</span></p>

        {/* Summary tiles */}
        <div className="grid md:grid-cols-4 gap-3 mt-10">
          <div className={`marketing-surface p-5 border ${riskColor}`} data-testid="summary-risk">
            <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Rejection risk</div>
            <div className="font-display font-black text-3xl mt-2 tracking-tight capitalize">{s.rejection_risk || "—"}</div>
          </div>
          <div className="marketing-surface p-5" data-testid="summary-critical">
            <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Critical failures</div>
            <div className="font-display font-black text-3xl mt-2 tracking-tight text-red-400">{s.critical_failures ?? 0}</div>
          </div>
          <div className="marketing-surface p-5" data-testid="summary-warnings">
            <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Warnings</div>
            <div className="font-display font-black text-3xl mt-2 tracking-tight text-amber-400">{s.warnings ?? 0}</div>
          </div>
          <div className="marketing-surface p-5" data-testid="summary-fix">
            <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Est. time to fix</div>
            <div className="font-display font-black text-3xl mt-2 tracking-tight">{s.estimated_fix_minutes ?? 0}<span className="text-neutral-500 text-lg"> min</span></div>
          </div>
        </div>

        {/* Preview issue list */}
        <div className="mt-10 space-y-2" data-testid="preview-issues">
          {audit.preview?.map((f, i) => (
            <div key={i} className="marketing-surface p-5" data-testid={`preview-issue-${i}`}>
              <div className="flex items-start gap-3">
                <div className={`px-2 py-0.5 border font-mono-spec text-[10px] tracking-widest uppercase inline-flex items-center gap-1 ${sevClass(f.severity)}`}>
                  {sevIcon(f.severity)} {f.severity}
                </div>
                <div className="flex-1">
                  <div className="font-display font-bold text-lg tracking-tight text-white">{f.title}</div>
                  <p className="text-sm text-neutral-400 mt-1 leading-relaxed">{f.why_it_fails}</p>
                </div>
                {f.one_click_fix && (
                  <div className="font-mono-spec text-[9px] tracking-widest uppercase text-emerald-400 whitespace-nowrap">One-click fix available</div>
                )}
              </div>
              {/* Locked details */}
              <div className="mt-4 border-t border-neutral-800 pt-4 flex items-center gap-3 text-neutral-500 font-mono-spec text-[10px] tracking-widest uppercase">
                <Lock className="w-3.5 h-3.5" /> Publisher rule cited · Fix steps · Tools · Pinpoint region — unlock below
              </div>
            </div>
          ))}
          {(!audit.preview || audit.preview.length === 0) && (
            <div className="marketing-surface p-8 text-center">
              <Check className="w-8 h-8 mx-auto text-emerald-400" />
              <p className="font-display font-bold text-xl mt-4 text-white">No issues detected in preview.</p>
              <p className="text-neutral-400 text-sm mt-2">The paid deep audit still runs additional checks — worth confirming for peace of mind.</p>
            </div>
          )}
        </div>

        {/* Unlock CTA */}
        <div className="mt-12 marketing-surface p-8 border-white/30" data-testid="unlock-cta">
          <div className="flex items-start justify-between gap-8 flex-wrap">
            <div className="flex-1 min-w-[260px]">
              <span className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">[ Unlock the full report ]</span>
              <h2 className="font-display font-black text-3xl md:text-4xl tracking-tighter mt-3">Get the exact rule cited,<br />the pinpoint region, and every fix step.</h2>
              <ul className="mt-5 space-y-2 text-sm text-neutral-300">
                <li className="flex items-center gap-2"><Sparkles className="w-4 h-4 text-white" /> Cited publisher rule for every finding</li>
                <li className="flex items-center gap-2"><Sparkles className="w-4 h-4 text-white" /> Exact region / pixels / inches with the problem</li>
                <li className="flex items-center gap-2"><Sparkles className="w-4 h-4 text-white" /> Step-by-step fix instructions</li>
                <li className="flex items-center gap-2"><Sparkles className="w-4 h-4 text-white" /> Tools needed + estimated minutes</li>
              </ul>
            </div>
            <div className="text-right">
              <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">One-time</div>
              <div className="font-display font-black text-6xl tracking-tighter mt-1">$0<span className="text-neutral-500">.99</span></div>
              <button
                onClick={unlock}
                disabled={checkingOut}
                className="mt-5 bg-white text-black px-6 py-3.5 font-mono-spec text-xs tracking-widest uppercase hover:bg-neutral-200 disabled:opacity-50 btn-industrial"
                data-testid="unlock-btn"
              >
                {checkingOut ? "Redirecting…" : "Unlock Full Report"}
              </button>
              <div className="mt-3 font-mono-spec text-[9px] tracking-widest uppercase text-neutral-500">Secure via Stripe · No account</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
