import { Link } from "react-router-dom";
import Nav from "@/components/Nav";
import SeasonBanner from "@/components/SeasonBanner";
import { LogoMark, LogoLockup } from "@/components/Logo";
import BrandWatermark from "@/components/BrandWatermark";
import { CheckCircle2, Zap, FileCheck2, Palette, Ruler, Layers, Flame, ArrowRight } from "lucide-react";

export default function Landing() {
  return (
    <div className="marketing min-h-screen noise-overlay">
      <SeasonBanner />
      <Nav dark />

      {/* Hero */}
      <section className="relative overflow-hidden">
        <BrandWatermark variant="dark" position="hero-left" scale={0.62} maxPx={780} />

        <div className="relative max-w-7xl mx-auto px-6 pt-20 pb-28 grid md:grid-cols-12 gap-10 items-center">
          <div className="md:col-span-7">
            <div className="flex items-center gap-3">
              <LogoMark size={32} />
              <span className="font-mono-spec text-[10px] tracking-[0.28em] uppercase text-[#D4AF37]">
                Publish · Automate · Elevate
              </span>
            </div>

            <span className="mt-8 inline-flex items-center gap-2 font-mono-spec text-xs tracking-widest uppercase text-[#D4AF37] border border-[#D4AF37]/50 bg-[#D4AF37]/10 px-3 py-1.5" data-testid="hero-tag">
              CMYK · 300 DPI · PDF/X-1a · Print-Ready in Minutes
            </span>

            <h1 className="font-display font-black text-5xl md:text-7xl lg:text-8xl tracking-tighter mt-8 leading-[0.9]" data-testid="hero-title">
              Ship a book<br />
              without<br />
              <span className="text-[#D4AF37]" style={{ background: "linear-gradient(180deg, #E5C158, #D4AF37 55%, #B8933E)", WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent" }}>prepress</span><br />
              headaches.
            </h1>

            <p className="mt-8 text-lg text-neutral-400 max-w-xl leading-relaxed">
              Drop any cover or interior file into <span className="text-white font-semibold">Legenddary's Smart SparkPrep Engine</span> — it auto-fixes DPI, CMYK, bleed and spine width, then exports a fully compliant PDF/X-1a for IngramSpark, KDP, Barnes & Noble and Lulu.
            </p>

            <div className="mt-10 flex flex-wrap gap-3">
              <Link to="/register" className="btn-gold px-7 py-4 font-mono-spec text-xs tracking-widest uppercase flex items-center gap-2 btn-industrial" data-testid="hero-cta-primary">
                Prep My First Book <ArrowRight className="w-4 h-4" />
              </Link>
              <Link to="/audit" className="btn-spark px-7 py-4 font-mono-spec text-xs tracking-widest uppercase text-white flex items-center gap-2 btn-industrial" data-testid="hero-cta-audit">
                <Flame className="w-3.5 h-3.5" /> Try the 99¢ Audit
              </Link>
              <Link to="/pricing" className="border border-neutral-700 text-white px-7 py-4 font-mono-spec text-xs tracking-widest uppercase hover:border-[#D4AF37] hover:text-[#D4AF37] btn-industrial" data-testid="hero-cta-secondary">
                See Pricing
              </Link>
            </div>

            <div className="mt-10 flex flex-wrap gap-6 text-xs font-mono-spec tracking-widest text-neutral-500 uppercase">
              <span className="flex items-center gap-1.5"><CheckCircle2 className="w-3.5 h-3.5 text-[#D4AF37]" /> IngramSpark</span>
              <span className="flex items-center gap-1.5"><CheckCircle2 className="w-3.5 h-3.5 text-[#D4AF37]" /> Amazon KDP</span>
              <span className="flex items-center gap-1.5"><CheckCircle2 className="w-3.5 h-3.5 text-[#D4AF37]" /> Barnes & Noble</span>
              <span className="flex items-center gap-1.5"><CheckCircle2 className="w-3.5 h-3.5 text-[#D4AF37]" /> Lulu</span>
            </div>
          </div>

          {/* Editor mock */}
          <div className="md:col-span-5">
            <div className="marketing-surface border-[#D4AF37]/20 p-4 relative">
              <div className="flex items-center justify-between text-xs font-mono-spec text-neutral-500 mb-3 uppercase tracking-widest">
                <span>Cover · 6" × 9"</span>
                <span className="text-emerald-400 flex items-center gap-1"><CheckCircle2 className="w-3 h-3" /> PASS</span>
              </div>
              <div className="aspect-[3/4] bg-neutral-950 relative overflow-hidden">
                <div className="absolute inset-2 book-mockup bg-gradient-to-br from-[#3A2A0A] via-[#0A0A0A] to-[#0A0A0A] flex flex-col justify-between p-6 border-l-2 border-[#D4AF37]">
                  <div>
                    <p className="font-mono-spec text-[10px] tracking-widest uppercase text-[#D4AF37]">Novel</p>
                    <h3 className="font-display font-black text-2xl text-white mt-1 tracking-tight">The Long Print</h3>
                  </div>
                  <p className="font-mono-spec text-[10px] text-[#D4AF37]/80 tracking-widest">M. J. HANLON</p>
                </div>
                <div className="absolute inset-4 overlay-bleed pointer-events-none opacity-70" />
                <div className="absolute inset-6 overlay-trim pointer-events-none opacity-70" />
                <div className="absolute inset-8 overlay-safe pointer-events-none opacity-70" />
              </div>
              <div className="grid grid-cols-3 gap-2 mt-3 text-[10px] font-mono-spec tracking-widest">
                <div className="border border-neutral-800 p-2">
                  <div className="text-neutral-500">DPI</div>
                  <div className="text-emerald-400">312</div>
                </div>
                <div className="border border-neutral-800 p-2">
                  <div className="text-neutral-500">COLOR</div>
                  <div className="text-emerald-400">CMYK</div>
                </div>
                <div className="border border-[#D4AF37]/40 bg-[#D4AF37]/5 p-2">
                  <div className="text-neutral-500">SPINE</div>
                  <div className="text-[#D4AF37]">0.451"</div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* Feature Bento */}
      <section className="border-t border-neutral-900">
        <div className="max-w-7xl mx-auto px-6 py-24">
          <span className="font-mono-spec text-xs tracking-widest uppercase text-[#D4AF37]">[ The Prepress Toolkit ]</span>
          <h2 className="font-display font-black text-4xl md:text-5xl tracking-tighter mt-3 max-w-3xl">Every reason your book gets<br />rejected, <span className="text-[#D4AF37]">eliminated</span>.</h2>

          <div className="grid md:grid-cols-12 gap-4 mt-14">
            {[
              { icon: Ruler, title: "Live DPI Reader", desc: "See effective DPI as you size and place images. Red/yellow/green feedback in real time.", span: "md:col-span-5", accent: true },
              { icon: Palette, title: "One-Click CMYK", desc: "Instantly convert RGB uploads into print-safe CMYK — no swatch juggling.", span: "md:col-span-4" },
              { icon: Layers, title: "Auto-Flatten", desc: "Transparency? Layers? All flattened at export.", span: "md:col-span-3" },
              { icon: Zap, title: "Spine Calculator", desc: "Type your page count. Get exact spine width per paper stock.", span: "md:col-span-4" },
              { icon: FileCheck2, title: "PDF/X-1a Export", desc: "Every export ships with embedded fonts, 300 DPI, correct trim + bleed.", span: "md:col-span-5", accent: true },
              { icon: CheckCircle2, title: "4 Platform Compliance", desc: "IngramSpark, KDP, B&N & Lulu templates baked in.", span: "md:col-span-3" },
            ].map((f, i) => (
              <div key={i} className={`marketing-surface p-8 ${f.accent ? "border-[#D4AF37]/30 bg-gradient-to-br from-[#D4AF37]/[0.06] to-transparent" : ""} ${f.span} min-h-[220px] flex flex-col justify-between`} data-testid={`feature-${i}`}>
                <div className={`w-10 h-10 flex items-center justify-center ${f.accent ? "bg-[#D4AF37] text-black" : "border border-neutral-700 text-white"}`}>
                  <f.icon className="w-5 h-5" />
                </div>
                <div>
                  <h3 className="font-display font-bold text-2xl tracking-tight text-white">{f.title}</h3>
                  <p className="text-neutral-400 text-sm mt-2 leading-relaxed">{f.desc}</p>
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Workflow */}
      <section className="border-t border-neutral-900 bg-neutral-950">
        <div className="max-w-7xl mx-auto px-6 py-24">
          <span className="font-mono-spec text-xs tracking-widest uppercase text-[#D4AF37]">[ Workflow ]</span>
          <h2 className="font-display font-black text-4xl md:text-5xl tracking-tighter mt-3 max-w-3xl">Five steps.<br />Then <span className="text-[#D4AF37]">export</span>.</h2>
          <div className="grid md:grid-cols-5 gap-4 mt-12">
            {["Choose platform", "Set trim + paper", "Enter page count", "Drop your file", "Export PDF/X-1a"].map((step, i) => (
              <div key={i} className={`border ${i === 4 ? "border-[#D4AF37] bg-[#D4AF37]/5" : "border-neutral-800"} p-6`} data-testid={`step-${i}`}>
                <div className={`font-mono-spec text-[10px] tracking-widest ${i === 4 ? "text-[#D4AF37]" : "text-neutral-600"}`}>STEP {String(i + 1).padStart(2, "0")}</div>
                <div className={`font-display font-bold text-xl mt-4 tracking-tight ${i === 4 ? "text-[#D4AF37]" : "text-white"}`}>{step}</div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* CTA */}
      <section className="border-t border-neutral-900 bg-gradient-to-b from-[#0A0A0A] to-[#151007]">
        <div className="max-w-4xl mx-auto px-6 py-24 text-center">
          <LogoLockup className="mx-auto" width={420} />
          <h2 className="font-display font-black text-4xl md:text-6xl tracking-tighter mt-8">
            Stop guessing.<br />
            <span style={{ background: "linear-gradient(180deg, #E5C158, #D4AF37 55%, #B8933E)", WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent" }}>Start printing.</span>
          </h2>
          <p className="text-neutral-400 mt-6 max-w-xl mx-auto">Free tier includes unlimited previews and compliance reports — no credit card. Or run a 99¢ audit right now, no account required.</p>
          <div className="mt-10 flex flex-wrap justify-center gap-3">
            <Link to="/register" className="btn-gold px-8 py-4 font-mono-spec text-xs tracking-widest uppercase btn-industrial" data-testid="footer-cta">
              Get Started — It's Free
            </Link>
            <Link to="/audit" className="btn-spark text-white px-8 py-4 font-mono-spec text-xs tracking-widest uppercase btn-industrial" data-testid="footer-audit">
              Or Run a 99¢ Audit
            </Link>
          </div>
        </div>
      </section>

      <footer className="border-t border-neutral-900 py-8 text-xs text-neutral-600 font-mono-spec tracking-widest uppercase text-center">
        © Legenddary — Publish · Automate · Elevate · We Build. You Legenddary.
      </footer>
    </div>
  );
}
