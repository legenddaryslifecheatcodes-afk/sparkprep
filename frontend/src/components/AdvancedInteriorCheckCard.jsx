import { useEffect, useState } from "react";
import { api, fmtErr } from "@/lib/api";
import { toast } from "sonner";
import { ShieldCheck, FileSearch, Lock } from "lucide-react";

// Mirrors backend ADVANCED_INTERIOR_PRICE_CENTS in server.py -- keep in sync.
const PRICE_BY_TIER = {
  free: 49.99,
  author: 39.99,
  creator_pro: 34.99,
  publisher: 29.99,
  studio: 29.99,
};
const MAX_PAGES = 300;

export default function AdvancedInteriorCheckCard({ project, user, projectId }) {
  const [status, setStatus] = useState(null); // null = loading, {paid, findings}
  const [purchasing, setPurchasing] = useState(false);

  const tier = user?.tier || "free";
  const price = PRICE_BY_TIER[tier] ?? PRICE_BY_TIER.free;
  const hasSubscriberDiscount = tier !== "free";
  const overPageLimit = (project?.page_count || 0) > MAX_PAGES;

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const sessionId = params.get("interior_check_session_id");
    if (sessionId) {
      api.get(`/projects/${projectId}/interior-check/verify`, { params: { session_id: sessionId } })
        .then(({ data }) => {
          setStatus(data);
          if (data.paid) toast.success("Advanced Interior Check unlocked");
        })
        .catch(() => setStatus({ paid: false }))
        .finally(() => {
          params.delete("interior_check_session_id");
          const clean = `${window.location.pathname}${params.toString() ? `?${params}` : ""}`;
          window.history.replaceState({}, "", clean);
        });
      return;
    }
    api.get(`/projects/${projectId}/interior-check/status`)
      .then(({ data }) => setStatus(data))
      .catch(() => setStatus({ paid: false }));
  }, [projectId]);

  const purchase = async () => {
    setPurchasing(true);
    try {
      const { data } = await api.post(`/projects/${projectId}/interior-check/checkout`, {
        origin_url: window.location.origin,
      });
      window.location.href = data.checkout_url;
    } catch (e) {
      const msg = fmtErr(e.response?.data?.detail);
      if (e.response?.status === 503) toast.info("Payments aren't configured yet — check back shortly.");
      else toast.error(msg);
      setPurchasing(false);
    }
  };

  return (
    <section
      className="border-2 border-dashed border-[#D4A857] bg-[#FFFBF0]"
      data-testid="advanced-interior-check"
    >
      <div className="p-5 border-b-2 border-dashed border-[#D4A857]/60 flex items-start justify-between flex-wrap gap-3">
        <div className="flex items-start gap-3">
          <FileSearch className="w-5 h-5 text-[#B8933E] shrink-0 mt-0.5" />
          <div>
            <div className="flex items-center gap-2 flex-wrap">
              <h3 className="font-display font-black text-lg tracking-tight">Advanced Interior Check</h3>
              <span className="font-mono-spec text-[9px] tracking-widest uppercase bg-[#D4A857] text-black px-1.5 py-0.5">
                Add-on — not part of your plan
              </span>
            </div>
            <p className="text-sm text-neutral-600 mt-1 max-w-2xl">
              Full structural check of every page (up to {MAX_PAGES}), not just the standard one-page check
              included with your account. <strong>One-time purchase for this book only</strong> — it does not
              renew, and it is not a subscription upgrade.
            </p>
          </div>
        </div>
      </div>

      <div className="p-5">
        {status === null ? (
          <div className="font-mono-spec text-xs text-neutral-500">Checking status…</div>
        ) : status.paid ? (
          <div data-testid="interior-check-results">
            <div className="flex items-center gap-2 text-sm font-semibold text-[#8A6D24]">
              <ShieldCheck className="w-4 h-4" /> Unlocked — full {MAX_PAGES}-page check complete
            </div>
            {status.findings?.length ? (
              <ul className="mt-3 space-y-2">
                {status.findings.map((f, i) => (
                  <li key={i} className="text-sm border border-[#D4A857]/40 bg-white px-3 py-2">
                    <span className="font-mono-spec text-[9px] tracking-widest uppercase mr-2 text-neutral-500">
                      {f.severity}
                    </span>
                    {f.title}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-neutral-600 mt-2">No structural issues found across the full interior.</p>
            )}
          </div>
        ) : (
          <div className="flex items-end justify-between flex-wrap gap-4">
            <div>
              <div className="flex items-baseline gap-2">
                <span className="font-display font-black text-3xl tracking-tight">${price.toFixed(2)}</span>
                <span className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">one-time · this book</span>
              </div>
              {hasSubscriberDiscount && (
                <p className="text-xs text-[#8A6D24] mt-1 font-semibold">Your subscriber discount applies</p>
              )}
              <p className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500 mt-2">
                Advanced Interior Check (up to {MAX_PAGES} pages)
              </p>
            </div>
            {overPageLimit ? (
              <div className="text-sm text-red-700 font-medium flex items-center gap-1.5">
                <Lock className="w-4 h-4" /> This book has {project.page_count} pages — over the {MAX_PAGES}-page limit
              </div>
            ) : (
              <button
                onClick={purchase}
                disabled={purchasing}
                className="px-6 py-3 bg-[#D4A857] text-black font-mono-spec text-xs tracking-widest uppercase hover:brightness-105 btn-industrial disabled:opacity-50"
                data-testid="interior-check-purchase-btn"
              >
                {purchasing ? "Redirecting…" : `Unlock Advanced Check — $${price.toFixed(2)}`}
              </button>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
