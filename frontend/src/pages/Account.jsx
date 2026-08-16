import { Link } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import Nav from "@/components/Nav";

export default function Account() {
  const { user } = useAuth();
  const tier = user?.tier || "free";
  const used = user?.exports_this_month ?? 0;
  const limits = { free: 3, author: 15, creator_pro: 45, publisher: 100, studio: 300 };
  const limit = limits[tier] || 3;
  return (
    <div className="min-h-screen bg-[#F7F7F9]">
      <Nav dark={false} />
      <div className="max-w-3xl mx-auto px-6 py-16">
        <span className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">[ Account ]</span>
        <h1 className="font-display font-black text-5xl tracking-tighter mt-2">Billing & usage.</h1>
        <div className="grid md:grid-cols-2 gap-4 mt-10">
          <div className="bg-white border border-neutral-200 p-6" data-testid="account-plan">
            <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Current Plan</div>
            <div className="font-display font-black text-3xl tracking-tight mt-2 capitalize">{tier}</div>
            <div className="mt-4 font-mono-spec text-[10px] tracking-widest uppercase text-neutral-600">
              {tier === "free" ? "Upgrade for higher export limits and bigger files." : "Active subscription."}
            </div>
            <Link to="/pricing" className="mt-6 inline-block border border-black px-5 py-2 font-mono-spec text-[10px] tracking-widest uppercase hover:bg-black hover:text-white transition-colors btn-industrial" data-testid="account-upgrade">
              {tier === "free" ? "Upgrade" : "Change Plan"}
            </Link>
          </div>
          <div className="bg-white border border-neutral-200 p-6" data-testid="account-usage">
            <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Exports this month</div>
            <div className="font-display font-black text-3xl tracking-tight mt-2">{used} <span className="text-neutral-400 font-normal">/ {limit}</span></div>
            <div className="mt-4 h-2 bg-neutral-100">
              <div className="h-full bg-black" style={{ width: `${Math.min(100, (used/limit)*100)}%` }} />
            </div>
            <div className="mt-4 font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Resets 1st of each month</div>
          </div>
          <div className="bg-white border border-neutral-200 p-6 md:col-span-2">
            <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Profile</div>
            <div className="grid grid-cols-2 mt-4 gap-4 text-sm">
              <div><div className="text-neutral-500 text-xs">Name</div><div className="font-mono-spec">{user?.name}</div></div>
              <div><div className="text-neutral-500 text-xs">Email</div><div className="font-mono-spec">{user?.email}</div></div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
