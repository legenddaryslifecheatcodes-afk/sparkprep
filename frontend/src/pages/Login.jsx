import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import { toast } from "sonner";
import Nav from "@/components/Nav";

export default function Login() {
  const { login, fmtErr } = useAuth();
  const [email, setEmail] = useState("");
  const [pw, setPw] = useState("");
  const [busy, setBusy] = useState(false);
  const nav = useNavigate();

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      await login(email, pw);
      toast.success("Signed in");
      nav("/dashboard");
    } catch (err) {
      toast.error(fmtErr(err.response?.data?.detail) || err.message);
    } finally { setBusy(false); }
  };

  return (
    <div className="marketing min-h-screen">
      <Nav dark />
      <div className="max-w-md mx-auto px-6 py-24">
        <span className="font-mono-spec text-xs tracking-widest uppercase text-neutral-500">[ Access ]</span>
        <h1 className="font-display font-black text-4xl md:text-5xl tracking-tighter mt-4">Log in.</h1>
        <form onSubmit={submit} className="mt-10 space-y-5" data-testid="login-form">
          <div>
            <label className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Email</label>
            <input type="email" required value={email} onChange={e=>setEmail(e.target.value)} className="mt-2 w-full bg-transparent border border-neutral-700 px-4 py-3 text-white focus:outline-none focus:border-white transition-colors" data-testid="login-email" />
          </div>
          <div>
            <label className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Password</label>
            <input type="password" required value={pw} onChange={e=>setPw(e.target.value)} className="mt-2 w-full bg-transparent border border-neutral-700 px-4 py-3 text-white focus:outline-none focus:border-white transition-colors" data-testid="login-password" />
          </div>
          <button type="submit" disabled={busy} className="w-full btn-gold py-3.5 font-mono-spec text-xs tracking-widest uppercase disabled:opacity-50 btn-industrial" data-testid="login-submit">
            {busy ? "Signing in…" : "Continue"}
          </button>
        </form>
        <p className="mt-6 text-sm text-neutral-500">No account? <Link to="/register" className="text-white underline" data-testid="link-register">Create one</Link></p>
      </div>
    </div>
  );
}
