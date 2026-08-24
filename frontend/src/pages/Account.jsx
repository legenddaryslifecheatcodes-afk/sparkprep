import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import { api, fmtErr } from "@/lib/api";
import { toast } from "sonner";
import Nav from "@/components/Nav";
import { Input } from "@/components/ui/input";
import { Users, Copy, LogOut, Trash2 } from "lucide-react";

function TeamPanel({ tier }) {
  const [status, setStatus] = useState(null);
  const [members, setMembers] = useState(null);
  const [joinCode, setJoinCode] = useState("");
  const [brandName, setBrandName] = useState("");
  const [busy, setBusy] = useState(false);

  const load = async () => {
    try {
      const { data } = await api.get("/team/status");
      setStatus(data);
      setBrandName(data.white_label_brand_name || "");
      if (data.role === "owner") {
        const { data: m } = await api.get("/team/members");
        setMembers(m.members);
      }
    } catch (e) { /* silent -- team status is best-effort */ }
  };

  useEffect(() => { load(); }, []);

  const invite = async () => {
    setBusy(true);
    try {
      const { data } = await api.post("/team/invite");
      navigator.clipboard?.writeText(data.invite_code);
      toast.success(`Invite code copied: ${data.invite_code}`);
      load();
    } catch (e) { toast.error(fmtErr(e.response?.data?.detail)); }
    finally { setBusy(false); }
  };

  const removeMember = async (id) => {
    try {
      await api.delete(`/team/members/${id}`);
      toast.success("Member removed");
      load();
    } catch (e) { toast.error(fmtErr(e.response?.data?.detail)); }
  };

  const join = async () => {
    if (!joinCode.trim()) return;
    setBusy(true);
    try {
      const { data } = await api.post("/team/join", { code: joinCode.trim() });
      toast.success(`Joined ${data.owner_email}'s team`);
      setJoinCode("");
      load();
    } catch (e) { toast.error(fmtErr(e.response?.data?.detail)); }
    finally { setBusy(false); }
  };

  const leave = async () => {
    if (!window.confirm("Leave this team? You'll go back to your own Free plan limits.")) return;
    try {
      await api.post("/team/leave");
      toast.success("Left the team");
      load();
    } catch (e) { toast.error(fmtErr(e.response?.data?.detail)); }
  };

  const saveBranding = async () => {
    setBusy(true);
    try {
      await api.patch("/team/branding", { brand_name: brandName });
      toast.success("Branding saved");
    } catch (e) { toast.error(fmtErr(e.response?.data?.detail)); }
    finally { setBusy(false); }
  };

  if (!status) return null;

  if (status.role === "member") {
    return (
      <div className="bg-white border border-neutral-200 p-6 md:col-span-2" data-testid="team-panel-member">
        <div className="flex items-center gap-2">
          <Users className="w-4 h-4 text-neutral-500" />
          <span className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Team</span>
        </div>
        <p className="text-sm mt-3">You're a member of <span className="font-semibold">{status.owner_email}</span>'s team — your plan, export limits, and upload size all follow their subscription.</p>
        <button onClick={leave} className="mt-4 flex items-center gap-1.5 text-sm text-red-600 hover:underline" data-testid="team-leave-btn">
          <LogOut className="w-3.5 h-3.5" /> Leave team
        </button>
      </div>
    );
  }

  const seatsAvailable = status.seats_total > 1;

  return (
    <div className="bg-white border border-neutral-200 p-6 md:col-span-2" data-testid="team-panel-owner">
      <div className="flex items-center gap-2">
        <Users className="w-4 h-4 text-neutral-500" />
        <span className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Team seats</span>
      </div>
      {!seatsAvailable ? (
        <p className="text-sm mt-3 text-neutral-600">
          Team seats aren't included on your plan. <Link to="/pricing" className="underline">Upgrade to Publisher (3 seats) or Studio (10 seats)</Link> to invite collaborators onto your subscription.
        </p>
      ) : (
        <>
          <p className="text-sm mt-3 text-neutral-600">{status.seats_used} of {status.seats_total} seats used. Invited members share your plan's export/book limits.</p>
          <div className="mt-4 space-y-2">
            {(members || []).map((m) => (
              <div key={m.id} className="flex items-center justify-between border border-neutral-100 px-3 py-2 text-sm" data-testid={`team-member-${m.id}`}>
                <span>{m.name} <span className="text-neutral-400">· {m.email}</span></span>
                <button onClick={() => removeMember(m.id)} className="text-neutral-400 hover:text-red-500" data-testid={`team-remove-${m.id}`}><Trash2 className="w-3.5 h-3.5" /></button>
              </div>
            ))}
            {status.pending_invites?.map((inv) => (
              <div key={inv.code} className="flex items-center justify-between border border-dashed border-neutral-300 px-3 py-2 text-sm text-neutral-500">
                <span className="font-mono-spec">{inv.code}</span>
                <span className="text-[10px] uppercase tracking-widest">Pending</span>
              </div>
            ))}
          </div>
          <button onClick={invite} disabled={busy} className="mt-4 flex items-center gap-1.5 border border-black px-4 py-2 font-mono-spec text-[10px] tracking-widest uppercase hover:bg-black hover:text-white transition-colors disabled:opacity-50" data-testid="team-invite-btn">
            <Copy className="w-3.5 h-3.5" /> Generate invite code
          </button>
        </>
      )}

      {status.role === "owner" && "white_label_brand_name" in status && (
        <div className="mt-6 pt-6 border-t border-neutral-100">
          <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">White-label branding</div>
          {tier !== "studio" ? (
            <p className="text-sm mt-2 text-neutral-600">White-label branding (replaces "SparkPrep" in exported PDFs and audit reports) is a Studio plan feature.</p>
          ) : (
            <div className="flex gap-2 mt-2">
              <Input value={brandName} onChange={(e) => setBrandName(e.target.value)} placeholder="Your imprint or studio name" data-testid="team-brand-input" />
              <button onClick={saveBranding} disabled={busy} className="px-4 border border-black font-mono-spec text-[10px] tracking-widest uppercase hover:bg-black hover:text-white transition-colors disabled:opacity-50" data-testid="team-brand-save">Save</button>
            </div>
          )}
        </div>
      )}

      <div className="mt-6 pt-6 border-t border-neutral-100">
        <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Have an invite code?</div>
        <div className="flex gap-2 mt-2">
          <Input value={joinCode} onChange={(e) => setJoinCode(e.target.value)} placeholder="TEAM-XXXXXXXX" className="font-mono" data-testid="team-join-input" />
          <button onClick={join} disabled={busy} className="px-4 border border-black font-mono-spec text-[10px] tracking-widest uppercase hover:bg-black hover:text-white transition-colors disabled:opacity-50" data-testid="team-join-btn">Join</button>
        </div>
      </div>
    </div>
  );
}

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
          <TeamPanel tier={tier} />
        </div>
      </div>
    </div>
  );
}
