import { useEffect, useState } from "react";
import { useParams, useSearchParams, Link } from "react-router-dom";
import { api, fmtErr } from "@/lib/api";
import Nav from "@/components/Nav";
import { Check, AlertTriangle, XCircle, MapPin, BookOpen, Wrench, Timer, CheckCircle2 } from "lucide-react";

const sevIcon = (s) => s === "pass" ? <Check className="w-4 h-4" /> : s === "warning" ? <AlertTriangle className="w-4 h-4" /> : <XCircle className="w-4 h-4" />;
const sevClass = (s) => s === "pass" ? "text-emerald-400 border-emerald-400/30 bg-emerald-500/10" : s === "warning" ? "text-amber-400 border-amber-400/30 bg-amber-500/10" : "text-red-400 border-red-400/30 bg-red-500/10";

export default function AuditReport() {
  const { id } = useParams();
  const [params] = useSearchParams();
  const sessionId = params.get("session_id");
  const [status, setStatus] = useState(sessionId ? "verifying" : "loading");
  const [audit, setAudit] = useState(null);

  useEffect(() => {
    (async () => {
      if (sessionId) {
        // Poll verify
        for (let i = 0; i < 20; i++) {
          try {
            const { data: v } = await api.get(`/audit/${id}/verify`, { params: { session_id: sessionId } });
            if (v.paid) break;
          } catch {}
          await new Promise((r) => setTimeout(r, 1500));
        }
      }
      try {
        const { data } = await api.get(`/audit/${id}`);
        setAudit(data);
        setStatus(data.paid ? "paid" : "unpaid");
      } catch { setStatus("error"); }
    })();
  }, [id, sessionId]);

  if (status === "loading" || status === "verifying") {
    return (
      <div className="marketing min-h-screen">
        <Nav dark />
        <div className="max-w-lg mx-auto px-6 py-24 text-center">
          <div className="animate-pulse font-mono-spec text-xs tracking-widest text-neutral-400 uppercase">
            {status === "verifying" ? "Verifying payment with Stripe…" : "Loading…"}
          </div>
        </div>
      </div>
    );
  }
  if (status === "unpaid") {
    return (
      <div className="marketing min-h-screen">
        <Nav dark />
        <div className="max-w-lg mx-auto px-6 py-24 text-center">
          <h1 className="font-display font-black text-4xl tracking-tighter">Payment not confirmed</h1>
          <p className="text-neutral-400 mt-4">If you just paid, wait 30 seconds and refresh. Otherwise:</p>
          <Link to={`/audit/${id}/preview`} className="mt-6 inline-block border border-white px-5 py-2.5 font-mono-spec text-xs tracking-widest uppercase hover:bg-white hover:text-black transition-colors">Back to Preview</Link>
        </div>
      </div>
    );
  }

  const findings = audit?.full_report || [];
  const s = audit?.summary || {};

  return (
    <div className="marketing min-h-screen noise-overlay">
      <Nav dark />
      <div className="max-w-5xl mx-auto px-6 py-12">
        <div className="flex items-center gap-2 mb-3">
          <CheckCircle2 className="w-5 h-5 text-emerald-400" />
          <span className="font-mono-spec text-[10px] tracking-widest uppercase text-emerald-400" data-testid="paid-badge">[ Paid · Full Report Unlocked ]</span>
        </div>
        <h1 className="font-display font-black text-4xl md:text-6xl tracking-tighter" data-testid="report-title">Your print failure report.</h1>
        <p className="text-neutral-400 mt-3">
          <span className="font-mono-spec text-white">{audit?.platform_name}</span> · trim <span className="font-mono-spec text-white">{audit?.trim_label}</span> · file <span className="font-mono-spec text-white">{audit?.file_metadata?.original_filename}</span>
        </p>

        {/* Overall banner */}
        <div className="mt-8 marketing-surface p-6">
          <div className="flex items-center justify-between flex-wrap gap-4">
            <div>
              <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Overall</div>
              <div className="font-display font-black text-3xl tracking-tight mt-1">
                {s.rejection_risk === "high" ? "High risk of rejection" : s.rejection_risk === "medium" ? "Medium risk" : s.rejection_risk === "low" ? "Low risk" : "Minimal risk"}
              </div>
            </div>
            <div className="flex gap-6 font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">
              <div><div>Critical</div><div className="text-red-400 text-2xl font-display">{s.critical_failures ?? 0}</div></div>
              <div><div>Warnings</div><div className="text-amber-400 text-2xl font-display">{s.warnings ?? 0}</div></div>
              <div><div>Fix time</div><div className="text-white text-2xl font-display">{s.estimated_fix_minutes ?? 0}m</div></div>
            </div>
          </div>
        </div>

        {/* Detailed findings */}
        <div className="mt-8 space-y-3" data-testid="findings-list">
          {findings.map((f, i) => (
            <div key={i} className="marketing-surface p-6" data-testid={`finding-${i}`}>
              <div className="flex items-start gap-3">
                <div className={`px-2 py-0.5 border font-mono-spec text-[10px] tracking-widest uppercase inline-flex items-center gap-1 ${sevClass(f.severity)}`}>
                  {sevIcon(f.severity)} {f.severity}
                </div>
                <div className="flex-1">
                  <div className="font-display font-bold text-xl tracking-tight text-white">{f.title}</div>
                  <p className="text-sm text-neutral-300 mt-2 leading-relaxed">{f.why_it_fails}</p>
                </div>
              </div>

              {/* Publisher rule */}
              <div className="mt-5 pl-3 border-l-2 border-neutral-700">
                <div className="flex items-center gap-2 font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">
                  <BookOpen className="w-3 h-3" /> Publisher Rule
                </div>
                <p className="text-sm text-neutral-200 mt-1 font-mono-spec">{f.publisher_rule}</p>
              </div>

              {/* Pinpoint */}
              {f.pinpoint && (
                <div className="mt-4 pl-3 border-l-2 border-neutral-700">
                  <div className="flex items-center gap-2 font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">
                    <MapPin className="w-3 h-3" /> Where in your file
                  </div>
                  <div className="text-sm text-neutral-200 mt-1 font-mono-spec">
                    Region: <span className="text-white">{f.pinpoint.region}</span>
                    {f.pinpoint.actual_dpi != null && <> · Actual DPI: <span className="text-red-300">{f.pinpoint.actual_dpi}</span> / Required: <span className="text-white">{f.pinpoint.required_dpi || 300}</span></>}
                    {f.pinpoint.actual_pixels && <> · Actual: <span className="text-red-300">{f.pinpoint.actual_pixels[0]}×{f.pinpoint.actual_pixels[1]}px</span></>}
                    {f.pinpoint.required_pixels && <> · Required: <span className="text-white">{f.pinpoint.required_pixels[0]}×{f.pinpoint.required_pixels[1]}px</span></>}
                    {f.pinpoint.actual_inches && <> · Actual: <span className="text-red-300">{f.pinpoint.actual_inches[0]}×{f.pinpoint.actual_inches[1]}"</span></>}
                    {f.pinpoint.expected_inches && <> · Expected: <span className="text-white">{f.pinpoint.expected_inches[0]}×{f.pinpoint.expected_inches[1]}"</span></>}
                    {f.pinpoint.actual && f.pinpoint.required && <> · <span className="text-red-300">{f.pinpoint.actual}</span> → <span className="text-white">{f.pinpoint.required}</span></>}
                  </div>
                </div>
              )}

              {/* Fix steps */}
              <div className="mt-4 pl-3 border-l-2 border-neutral-700">
                <div className="flex items-center gap-2 font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">
                  <Wrench className="w-3 h-3" /> How to fix it
                </div>
                <ol className="mt-2 space-y-1.5 text-sm text-neutral-200 list-decimal pl-5">
                  {f.fix_steps?.map((step, si) => (<li key={si} className="leading-relaxed">{step}</li>))}
                </ol>
              </div>

              {/* Meta row */}
              <div className="mt-5 flex flex-wrap gap-4 pt-4 border-t border-neutral-800 font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">
                <div className="flex items-center gap-1.5"><Wrench className="w-3 h-3" /> Tools: <span className="text-white">{f.fix_tools?.join(" · ")}</span></div>
                <div className="flex items-center gap-1.5"><Timer className="w-3 h-3" /> Time: <span className="text-white">{f.est_fix_minutes} min</span></div>
                {f.one_click_fix && <div className="text-emerald-400">One-click fix in SparkPrep editor</div>}
              </div>
            </div>
          ))}
          {findings.length === 0 && (
            <div className="marketing-surface p-8 text-center">
              <CheckCircle2 className="w-10 h-10 mx-auto text-emerald-400" />
              <p className="font-display font-bold text-2xl mt-4 text-white">Your file passes every check.</p>
              <p className="text-neutral-400 text-sm mt-2">Ready for submission.</p>
            </div>
          )}
        </div>

        {/* Upsell to full editor */}
        <div className="mt-10 marketing-surface p-6 text-center">
          <span className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">[ Want us to fix it? ]</span>
          <h3 className="font-display font-black text-2xl md:text-3xl tracking-tighter mt-3">Skip the manual work.</h3>
          <p className="text-neutral-400 mt-3 max-w-xl mx-auto text-sm">Start a free SparkPrep account and every "one-click fix" above happens automatically on export — CMYK, 300 DPI, flatten, PDF/X-1a.</p>
          <Link to="/register" className="mt-6 inline-block bg-white text-black px-6 py-3 font-mono-spec text-xs tracking-widest uppercase hover:bg-neutral-200 btn-industrial" data-testid="upsell-signup">
            Start Free Account
          </Link>
        </div>
      </div>
    </div>
  );
}
