import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, fmtErr } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";
import { toast } from "sonner";
import Nav from "@/components/Nav";
import BrandWatermark from "@/components/BrandWatermark";
import { Check, X, HelpCircle, Star, Send } from "lucide-react";

const STATUS_OPTIONS = [
  { key: "worked", label: "Worked", icon: Check, color: "text-emerald-400 border-emerald-500/40 bg-emerald-500/5" },
  { key: "didnt_work", label: "Didn't work", icon: X, color: "text-red-400 border-red-500/40 bg-red-500/5" },
  { key: "not_tested", label: "Not tested", icon: HelpCircle, color: "text-neutral-400 border-neutral-700 bg-neutral-800/40" },
];

export default function BetaFeedback() {
  const nav = useNavigate();
  const { user, refreshUser } = useAuth();
  const [status, setStatus] = useState(null);
  const [checklist, setChecklist] = useState([]);
  const [critical, setCritical] = useState("");
  const [publicReview, setPublicReview] = useState("");
  const [recommend, setRecommend] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    (async () => {
      try {
        const { data } = await api.get("/beta/status");
        setStatus(data);
        setChecklist(
          (data.checklist_template || []).map((f) => ({
            key: f.key,
            label: f.label,
            status: "not_tested",
            notes: "",
          })),
        );
      } catch (e) { toast.error(fmtErr(e.response?.data?.detail)); }
      finally { setLoading(false); }
    })();
  }, []);

  const setItem = (idx, patch) => {
    setChecklist((prev) => prev.map((it, i) => (i === idx ? { ...it, ...patch } : it)));
  };

  const submit = async (e) => {
    e.preventDefault();
    if (!critical.trim()) return toast.error("Critical review is required — even one sentence helps.");
    setBusy(true);
    try {
      await api.post("/beta/feedback", {
        checklist,
        critical_review: critical,
        public_review: publicReview,
        would_recommend: recommend,
      });
      toast.success("Thank you — feedback locked in. Your beta grant is now closed.");
      await refreshUser();
      nav("/dashboard");
    } catch (err) {
      toast.error(fmtErr(err.response?.data?.detail) || "Submission failed");
    } finally {
      setBusy(false);
    }
  };

  if (loading) return <div className="marketing min-h-screen"><Nav dark /><div className="p-10 font-mono-spec text-xs text-neutral-500">Loading…</div></div>;

  if (status?.feedback_submitted) {
    return (
      <div className="marketing min-h-screen">
        <Nav dark />
        <div className="max-w-3xl mx-auto px-6 py-24 text-center">
          <div className="inline-flex items-center gap-2 font-mono-spec text-[10px] tracking-widest uppercase text-emerald-400 border border-emerald-500/40 bg-emerald-500/5 px-3 py-1.5">
            <Check className="w-3 h-3" /> Feedback received
          </div>
          <h1 className="font-display font-black text-5xl tracking-tighter mt-6">Sign-off locked in.</h1>
          <p className="text-neutral-400 mt-4 max-w-lg mx-auto">Thanks for helping us harden Legenddary before the wider launch. Your feedback has been delivered to the founding team.</p>
          <button onClick={() => nav("/dashboard")} className="mt-8 btn-gold px-6 py-3 font-mono-spec text-xs tracking-widest uppercase btn-industrial">
            Back to Dashboard →
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="marketing min-h-screen">
      <Nav dark />
      <div className="relative overflow-hidden">
        <BrandWatermark variant="dark" position="hero-right" scale={0.5} maxPx={620} opacity={0.18} />
        <div className="relative max-w-4xl mx-auto px-6 py-16">
          <span className="inline-flex items-center gap-2 font-mono-spec text-[10px] tracking-[0.24em] uppercase text-[#D4AF37] border border-[#D4AF37]/40 bg-[#D4AF37]/5 px-2.5 py-1">
            <Star className="w-3 h-3" /> Beta Sign-Off · {user?.beta_pass_code || "Beta Access"}
          </span>
          <h1 className="font-display font-black text-5xl md:text-6xl tracking-tighter mt-6 leading-[0.95]" data-testid="feedback-title">
            Tell us <span style={{ background: "linear-gradient(180deg,#E5C158,#D4AF37 55%,#B8933E)", WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent" }}>everything</span>.
          </h1>
          <p className="mt-4 text-neutral-400 max-w-xl leading-relaxed">
            Go feature by feature. Mark what worked, what broke, and what confused you. Then leave a critical review, and — if you want — a public review we can share with future buyers.
          </p>
        </div>
      </div>

      <form onSubmit={submit} className="max-w-4xl mx-auto px-6 pb-24">
        {/* Feature checklist */}
        <div className="marketing-surface p-8" data-testid="checklist-panel">
          <div className="flex items-baseline justify-between flex-wrap gap-2">
            <div>
              <span className="font-mono-spec text-[10px] tracking-widest uppercase text-[#D4AF37]">[ Section 1 · Feature checklist ]</span>
              <h2 className="font-display font-black text-2xl tracking-tight mt-1">What worked. What didn't.</h2>
            </div>
            <span className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">{checklist.filter(x => x.status !== "not_tested").length} / {checklist.length} tested</span>
          </div>

          <div className="mt-6 space-y-4">
            {checklist.map((item, idx) => (
              <div key={item.key} className="border border-neutral-800 p-4" data-testid={`feature-row-${item.key}`}>
                <div className="flex items-start justify-between gap-4 flex-wrap">
                  <div className="flex-1 min-w-[220px]">
                    <div className="font-display font-bold text-base tracking-tight text-white">{item.label}</div>
                    <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500 mt-1">{item.key}</div>
                  </div>
                  <div className="flex gap-1.5">
                    {STATUS_OPTIONS.map((opt) => {
                      const active = item.status === opt.key;
                      const Icon = opt.icon;
                      return (
                        <button
                          key={opt.key}
                          type="button"
                          onClick={() => setItem(idx, { status: opt.key })}
                          className={`px-3 py-2 font-mono-spec text-[10px] tracking-widest uppercase border transition-all flex items-center gap-1.5 btn-industrial ${active ? opt.color : "text-neutral-500 border-neutral-800 hover:border-neutral-700"}`}
                          data-testid={`feature-${item.key}-${opt.key}`}
                        >
                          <Icon className="w-3 h-3" /> {opt.label}
                        </button>
                      );
                    })}
                  </div>
                </div>
                <textarea
                  value={item.notes}
                  onChange={(e) => setItem(idx, { notes: e.target.value })}
                  placeholder="Optional notes — what happened, steps to reproduce, screenshots welcome via review section below"
                  className="mt-3 w-full bg-black/60 border border-neutral-800 focus:border-neutral-600 px-3 py-2 text-sm text-neutral-200 outline-none min-h-[52px] resize-y"
                  data-testid={`feature-${item.key}-notes`}
                />
              </div>
            ))}
          </div>
        </div>

        {/* Critical review */}
        <div className="mt-6 marketing-surface p-8" data-testid="critical-panel">
          <span className="font-mono-spec text-[10px] tracking-widest uppercase text-[#D4AF37]">[ Section 2 · Critical review & suggestions ]</span>
          <h2 className="font-display font-black text-2xl tracking-tight mt-1">Founders read every word.</h2>
          <p className="text-neutral-500 text-sm mt-2">Brutal honesty preferred. Confusing flows, missing features, workflow gaps, aesthetic misses — say it all.</p>
          <textarea
            value={critical}
            onChange={(e) => setCritical(e.target.value)}
            placeholder="What frustrated you? What was missing? What would you change?"
            className="mt-4 w-full bg-black/60 border border-neutral-800 focus:border-[#D4AF37] px-4 py-3 text-sm text-neutral-100 outline-none min-h-[160px] resize-y"
            data-testid="critical-review"
            required
          />
        </div>

        {/* Public review */}
        <div className="mt-6 marketing-surface p-8" data-testid="public-panel">
          <span className="font-mono-spec text-[10px] tracking-widest uppercase text-[#D4AF37]">[ Section 3 · Public review (optional) ]</span>
          <h2 className="font-display font-black text-2xl tracking-tight mt-1">A quote we can share.</h2>
          <p className="text-neutral-500 text-sm mt-2">Only shown publicly if you agree below. Skip if you'd rather stay private.</p>
          <textarea
            value={publicReview}
            onChange={(e) => setPublicReview(e.target.value)}
            placeholder="e.g. Legenddary took my print-ready PDF from 'reject' to 'approved' in ten minutes."
            className="mt-4 w-full bg-black/60 border border-neutral-800 focus:border-[#D4AF37] px-4 py-3 text-sm text-neutral-100 outline-none min-h-[100px] resize-y"
            data-testid="public-review"
          />
          <label className="mt-4 flex items-center gap-2 text-sm text-neutral-300 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={recommend}
              onChange={(e) => setRecommend(e.target.checked)}
              className="w-4 h-4"
              data-testid="would-recommend"
            />
            Yes — I'd recommend Legenddary to another author or publisher.
          </label>
        </div>

        <div className="mt-10 flex items-center justify-between flex-wrap gap-4">
          <span className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Submitting will close your beta grant.</span>
          <button
            type="submit"
            disabled={busy}
            className="btn-gold px-8 py-4 font-mono-spec text-xs tracking-widest uppercase flex items-center gap-2 btn-industrial disabled:opacity-50"
            data-testid="feedback-submit"
          >
            <Send className="w-4 h-4" /> {busy ? "Submitting…" : "Submit Sign-Off"}
          </button>
        </div>
      </form>
    </div>
  );
}
