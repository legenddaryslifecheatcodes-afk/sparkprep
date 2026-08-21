import { Link } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import { Logo } from "@/components/Logo";

export default function Nav({ dark = true }) {
  const { user, logout } = useAuth();
  const base = dark
    ? "border-b border-neutral-900 bg-[#0D0D0D]/90 backdrop-blur-xl"
    : "border-b border-neutral-200 bg-[#F7F7F5]/90 backdrop-blur-xl";
  const linkCls = dark ? "text-neutral-400 hover:text-white transition-colors" : "text-neutral-600 hover:text-neutral-900 transition-colors";
  return (
    <nav className={`sticky top-0 z-50 ${base}`} data-testid="top-nav">
      <div className="max-w-7xl mx-auto flex items-center justify-between px-6 py-4">
        <Link to="/" data-testid="nav-logo">
          <Logo dark={dark} />
        </Link>
        <div className="flex items-center gap-5 text-sm">
          {user?.beta_active && (
            <Link to="/beta/feedback" className="font-mono-spec text-[10px] tracking-widest uppercase text-black bg-[#D4AF37] px-2.5 py-1.5 hover:brightness-110 btn-industrial" data-testid="nav-beta-badge">
              BETA · Sign Off
            </Link>
          )}
          {user?.is_admin && (
            <Link to="/admin/beta" className={`${linkCls} font-mono-spec text-[10px] tracking-widest uppercase border ${dark ? "border-neutral-700" : "border-neutral-300"} px-2 py-1`} data-testid="nav-admin">
              Admin
            </Link>
          )}
          <Link to="/audit" className="font-mono-spec text-[10px] tracking-widest uppercase text-white bg-[#FF6A00] px-2.5 py-1.5 hover:brightness-110 btn-industrial" data-testid="nav-audit">
            99¢ Audit
          </Link>
          <Link to="/pricing" className={linkCls} data-testid="nav-pricing">Pricing</Link>
          {user ? (
            <>
              <Link to="/dashboard" className={linkCls} data-testid="nav-dashboard">Dashboard</Link>
              <Link to="/account" className={linkCls} data-testid="nav-account">Account</Link>
              <button onClick={logout} className={`${linkCls} font-mono-spec text-xs tracking-widest uppercase`} data-testid="nav-logout">Logout</button>
            </>
          ) : (
            <>
              <Link to="/login" className={linkCls} data-testid="nav-login">Log In</Link>
              <Link to="/register" className="px-4 py-2 btn-gold font-mono-spec text-xs tracking-widest uppercase btn-industrial" data-testid="nav-signup">
                Start Free
              </Link>
            </>
          )}
        </div>
      </div>
    </nav>
  );
}
