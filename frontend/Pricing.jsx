import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, fmtErr } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";
import { toast } from "sonner";
import Nav from "@/components/Nav";
import SeasonBanner from "@/components/SeasonBanner";
import BrandWatermark from "@/components/BrandWatermark";
import { Check, Flame, ShieldCheck, Sparkles } from "lucide-react";

const TIERS = [
  {
    key: "free",
    name: "Free",
    tag: "Get started",
    price: 0,
    price_label: "$0",
    books: "Preview & compliance only",
    features: [
      "No exports — upgrade to export",
      "Uploads up to 25 MB",
      "KDP + IngramSpark templates",
      "DPI, CMYK + spine calculator",
    ],
    cta: "Start Free",
  },
  {
    key: "author",
    name: "Author",
    tag: "For self-publishers",
    price: 19.99,
    price_label: "$19.99",
    books: "1 full book / month",
    features: [
      "Cover + spine + back + interior",
      "15 print-ready exports / month",
      "Uploads up to 100 MB",
      "All distributor templates",
      "AI Blurb Writer",
    ],
    cta: "Choose Author",
  },
  {
    key: "creator_pro",
    name: "Creator Pro",
    tag: "For prolific writers",
    price: 39.99,
    price_label: "$39.99",
    books: "3 full books / month",
    features: [
      "Everything in Author",
      "45 exports / month",
      "Uploads up to 250 MB",
      "Priority AI blurb + 3D mockup",
      "Email support",
    ],
    cta: "Choose Creator Pro",
    highlight: true,
  },
  {
    key: "publisher",
    name: "Publisher",
    tag: "For small imprints",
    price: 99,
    price_label: "$99",
    books: "5 full books / month",
    features: [
      "Everything in Creator Pro",
      "100 exports / month",
      "Team seats (up to 3)",
      "Uploads up to 500 MB",
      "Bulk audit + batch export",
      "Priority support",
    ],
    cta: "Choose Publisher",
  },
  {
    key: "studio",
    name: "Studio",
    tag: "For prepress studios",
    price: 249,
    price_label: "$249",
    books: "15 full books / month",
    gold: true,
    features: [
      "Everything in Publisher",
      "300 exports / month",
      "Team seats (up to 10)",
      "Uploads up to 1 GB",
      "Advanced color profiles + white-label",
      "Dedicated account manager",
    ],
    cta: "Choose Studio",
  },
];

export default function Pricing() {
  const { user } = useAuth();
  const [busy, setBusy] = useState(null);
  const nav = useNavigate();

  const checkout = async (tier) => {
    if (tier === "free") { nav(user ? "/dashboard" : "/register"); return; }
    if (!user) { nav("/register"); return; }
    setBusy(tier);
    try {
      const { data } = await api.post("/payments/checkout", { tier, origin_url: window.location.origin });
      window.location.href = data.checkout_url;
    } catch (e) {
      const msg = fmtErr(e.response?.data?.detail);
      if (e.response?.status === 503) toast.info("Stripe is being provisioned — check back shortly.");
      else toast.error(msg);
      setBusy(null);
    }
  };

  return (
    <div className="marketing min-h-screen">
      <SeasonBanner />
      <Nav dark />
      <div className="relative overflow-hidden">
        <BrandWatermark variant="dark" position="hero-right" scale={0.55} maxPx={720} opacity={0.22} />
        <div className="relative max-w-7xl mx-auto px-6 py-16">
          <span className="font-mono-spec text-xs tracking-widest uppercase text-neutral-500">[ Pricing ]</span>
          <h1 className="font-display font-black text-5xl md:text-6xl tracking-tighter mt-3">Pay per title,<br />not per fix.</h1>
          <p className="text-neutral-400 mt-4 max-w-xl">Every plan includes CMYK, DPI, spine calculator, PDF/X-1a export and distributor templates. Cancel any time.</p>
        </div>
      </div>
      <div className="max-w-7xl mx-auto px-6 pb-16">

        {/* 99¢ Audit lead-magnet card */}
        <div className="mt-12 bg-gradient-to-r from-[#FF6A00] to-[#FF8B3D] p-[1px]" data-testid="audit-hero">
          <div className="bg-[#0D0D0D] p-8 grid md:grid-cols-3 gap-6 items-center">
            <div className="md:col-span-2">
              <div className="inline-flex items-center gap-2 bg-[#FF6A00] text-white px-2 py-1 font-mono-spec text-[10px] tracking-widest uppercase">
                <Flame className="w-3 h-3" /> Lead Magnet · No Signup Required
              </div>
              <h2 className="font-display font-black text-4xl md:text-5xl tracking-tighter mt-4">99¢ Print Failure Audit.</h2>
              <p className="text-neutral-400 mt-3 max-w-xl leading-relaxed">
                One-time pay. Anyone can upload a cover or interior and get a pixel-level pinpointed audit — with the exact publisher rule cited, region flagged, and step-by-step fix instructions. Perfect for authors who want a second opinion before submitting to IngramSpark or KDP.
              </p>
              <div className="mt-5 flex flex-wrap gap-2 font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">
                <span className="border border-neutral-700 px-2 py-1">✓ No account</span>
                <span className="border border-neutral-700 px-2 py-1">✓ No subscription</span>
                <span className="border border-neutral-700 px-2 py-1">✓ Publisher rule cited</span>
                <span className="border border-neutral-700 px-2 py-1">✓ Fix steps</span>
              </div>
            </div>
            <div className="text-right">
              <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">One-time</div>
              <div className="font-display font-black text-6xl tracking-tighter">$0<span className="text-neutral-500">.99</span></div>
              <a href="/audit" className="mt-4 inline-block bg-white text-[#FF6A00] px-6 py-3 font-mono-spec text-xs tracking-widest uppercase hover:bg-neutral-100 btn-industrial" data-testid="audit-cta">
                Run An Audit
              </a>
            </div>
          </div>
        </div>

        {/* Subscription ladder */}
        <div className="mt-16">
          <div className="flex items-baseline justify-between flex-wrap gap-3">
            <div>
              <span className="font-mono-spec text-xs tracking-widest uppercase text-neutral-500">[ Subscription Ladder ]</span>
              <h2 className="font-display font-black text-3xl md:text-4xl tracking-tighter mt-2">Ship more books, month after month.</h2>
            </div>
            <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Books = complete cover + spine + back + interior</div>
          </div>
          <div className="grid md:grid-cols-5 gap-3 mt-10">
            {TIERS.map((t) => (
              <div
                key={t.key}
                className={`marketing-surface p-6 relative flex flex-col ${t.highlight ? "border-[#FF6A00]/50 ring-1 ring-[#FF6A00]/30" : ""} ${t.gold ? "border-[#D4A857]/50" : ""}`}
                data-testid={`tier-${t.key}`}
              >
                {t.highlight && (
                  <div className="absolute top-0 right-0 bg-[#FF6A00] text-white font-mono-spec text-[10px] tracking-widest uppercase px-2 py-1" data-testid={`tier-badge-${t.key}`}>Most Popular</div>
                )}
                {t.gold && (
                  <div className="absolute top-0 right-0 bg-[#D4A857] text-black font-mono-spec text-[10px] tracking-widest uppercase px-2 py-1" data-testid={`tier-badge-${t.key}`}>Premium</div>
                )}
                <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">{t.tag}</div>
                <h3 className="font-display font-black text-2xl mt-2 tracking-tight">{t.name}</h3>
                <div className="flex items-baseline gap-1 mt-4">
                  <span className={`font-display font-black text-4xl ${t.highlight ? "text-[#FF6A00]" : t.gold ? "text-[#D4A857]" : ""}`}>{t.price_label}</span>
                  {t.price > 0 && <span className="text-neutral-500 text-xs">/mo</span>}
                </div>
                <div className="mt-3 font-mono-spec text-[10px] tracking-widest uppercase text-neutral-400 min-h-[32px]">{t.books}</div>
                <button
                  onClick={() => checkout(t.key)}
                  disabled={busy === t.key}
                  className={`mt-5 w-full py-2.5 font-mono-spec text-xs tracking-widest uppercase transition-colors btn-industrial ${
                    t.highlight ? "btn-spark" : t.gold ? "bg-[#D4A857] text-black hover:brightness-110" : t.price === 0 ? "border border-neutral-700 hover:border-white" : "bg-white text-black hover:bg-neutral-200"
                  }`}
                  data-testid={`tier-select-${t.key}`}
                >
                  {busy === t.key ? "Redirecting…" : t.cta}
                </button>
                <ul className="mt-6 space-y-2 text-xs flex-1">
                  {t.features.map((f, i) => (
                    <li key={i} className="flex items-start gap-1.5 text-neutral-300 leading-relaxed">
                      <Check className="w-3.5 h-3.5 mt-0.5 shrink-0 text-neutral-500" /> {f}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </div>

        {/* Value comparison strip */}
        <div className="mt-16 marketing-surface p-8">
          <span className="font-mono-spec text-xs tracking-widest uppercase text-neutral-500">[ At a glance ]</span>
          <h3 className="font-display font-black text-2xl mt-2 tracking-tight">Which tier matches your output?</h3>
          <div className="mt-6 grid md:grid-cols-5 gap-3 text-sm">
            {[
              { books: "0", best: "Trying it out" },
              { books: "1 /mo", best: "New author with 1 title" },
              { books: "3 /mo", best: "Author with a series or trilogy" },
              { books: "5 /mo", best: "Small imprint or ghost-writer" },
              { books: "15 /mo", best: "Prepress studio / packager" },
            ].map((row, i) => (
              <div key={i} className="border border-neutral-800 p-3">
                <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Books</div>
                <div className="font-display font-black text-2xl tracking-tight text-white mt-1">{row.books}</div>
                <div className="text-xs text-neutral-400 mt-2">{row.best}</div>
              </div>
            ))}
          </div>
        </div>

        <div className="mt-12 text-center">
          <span className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Upgrade or downgrade any time — new limits apply on the next billing cycle.</span>
        </div>
      </div>
    </div>
  );
}
