import { useEffect, useState } from "react";
import { useNavigate, useSearchParams, Link } from "react-router-dom";
import { api, fmtErr } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";
import { toast } from "sonner";
import Nav from "@/components/Nav";
import BrandWatermark from "@/components/BrandWatermark";
import { LogoLockup } from "@/components/Logo";
import { KeyRound, ShieldCheck, Sparkles } from "lucide-react";

export default function BetaRedeem() {
  const nav = useNavigate();
  const [params] = useSearchParams();
  const { setUser } = useAuth();
  const [code, setCode] = useState((params.get("code") || "").toUpperCase());
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const c = params.get("code");
    if (c) setCode(c.toUpperCase());
  }, [params]);

  const submit = async (e) => {
    e.preventDefault();
    if (!code.trim()) return toast.error("Enter your beta code");
    if (!email.trim()) return toast.error("Enter your email");
    if (password.length < 6) return toast.error("Password must be at least 6 characters");
    setBusy(true);
    try {
      const { data } = await api.post("/beta/redeem", {
        code: code.trim().toUpperCase(),
        email: email.trim(),
        password,
        name: name.trim() || undefined,
      });
      setUser(data.user);
      toast.success("Beta pass redeemed. Welcome to the vault.");
      nav("/dashboard");
    } catch (err) {
      toast.error(fmtErr(err.response?.data?.detail) || "Redemption failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="marketing min-h-screen">
      <Nav dark />
      <div className="relative overflow-hidden">
        <BrandWatermark variant="dark" position="hero-right" scale={0.55} maxPx={680} opacity={0.22} />
        <div className="relative max-w-5xl mx-auto px-6 py-20 grid md:grid-cols-2 gap-14 items-center">
          <div>
            <span className="inline-flex items-center gap-2 font-mono-spec text-[10px] tracking-[0.24em] uppercase text-[#D4AF37] border border-[#D4AF37]/40 bg-[#D4AF37]/5 px-2.5 py-1">
              <Sparkles className="w-3 h-3" /> Private Beta · Invitation Only
            </span>
            <h1 className="font-display font-black text-5xl md:text-6xl tracking-tighter mt-6 leading-[0.95]" data-testid="beta-title">
              Break it. Bend it.<br /><span style={{ background: "linear-gradient(180deg,#E5C158,#D4AF37 55%,#B8933E)", WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent" }}>Then tell us.</span>
            </h1>
            <p className="mt-6 text-neutral-400 leading-relaxed max-w-md">
              Your beta pass unlocks one full book project with every premium feature enabled — unlimited exports on that project, full 1GB uploads, AI blurb, 3D mockup, all distributor templates. Ship one book. Sign off. We reward every tester who submits real feedback.
            </p>
            <ul className="mt-8 space-y-3 text-sm text-neutral-300">
              <li className="flex items-center gap-3"><ShieldCheck className="w-4 h-4 text-[#D4AF37]" /> Single-use code — cannot be shared or transferred</li>
              <li className="flex items-center gap-3"><ShieldCheck className="w-4 h-4 text-[#D4AF37]" /> All premium features unlocked for one full project</li>
              <li className="flex items-center gap-3"><ShieldCheck className="w-4 h-4 text-[#D4AF37]" /> Ends when you submit your sign-off feedback</li>
            </ul>
          </div>

          <form onSubmit={submit} className="marketing-surface p-8 border-[#D4AF37]/30" data-testid="beta-redeem-form">
            <div className="flex items-center gap-2 font-mono-spec text-[10px] tracking-widest uppercase text-[#D4AF37]">
              <KeyRound className="w-3.5 h-3.5" /> Redeem your pass
            </div>
            <h2 className="font-display font-black text-3xl tracking-tighter mt-2">Claim your access.</h2>

            <label className="block mt-6">
              <span className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Beta code</span>
              <input
                value={code}
                onChange={(e) => setCode(e.target.value.toUpperCase())}
                placeholder="LGND-XXXXXXXX"
                className="mt-2 w-full bg-black border border-neutral-800 focus:border-[#D4AF37] px-4 py-3 font-mono-spec text-sm tracking-widest text-white outline-none"
                data-testid="beta-code-input"
                autoComplete="off"
              />
            </label>

            <label className="block mt-4">
              <span className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Your name (optional)</span>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. Chris"
                className="mt-2 w-full bg-black border border-neutral-800 focus:border-[#D4AF37] px-4 py-3 text-sm text-white outline-none"
                data-testid="beta-name-input"
              />
            </label>

            <label className="block mt-4">
              <span className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Email</span>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
                className="mt-2 w-full bg-black border border-neutral-800 focus:border-[#D4AF37] px-4 py-3 text-sm text-white outline-none"
                data-testid="beta-email-input"
                autoComplete="email"
              />
            </label>

            <label className="block mt-4">
              <span className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Choose a password</span>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="At least 6 characters"
                className="mt-2 w-full bg-black border border-neutral-800 focus:border-[#D4AF37] px-4 py-3 text-sm text-white outline-none"
                data-testid="beta-password-input"
                autoComplete="new-password"
              />
            </label>

            <button
              type="submit"
              disabled={busy}
              className="mt-8 w-full btn-gold py-4 font-mono-spec text-xs tracking-widest uppercase btn-industrial disabled:opacity-50"
              data-testid="beta-redeem-submit"
            >
              {busy ? "Redeeming…" : "Redeem & Enter Beta"}
            </button>

            <p className="mt-4 text-[10px] font-mono-spec tracking-widest text-neutral-500 uppercase text-center">
              Already redeemed? <Link to="/login" className="text-[#D4AF37]">Log in →</Link>
            </p>
          </form>
        </div>
      </div>
    </div>
  );
}
