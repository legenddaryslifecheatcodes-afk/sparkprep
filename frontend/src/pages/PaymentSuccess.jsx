import { useEffect, useState } from "react";
import { useSearchParams, Link } from "react-router-dom";
import { api } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";
import Nav from "@/components/Nav";
import { CheckCircle2 } from "lucide-react";

export default function PaymentSuccess() {
  const [params] = useSearchParams();
  const sessionId = params.get("session_id");
  const [status, setStatus] = useState("polling");
  const { refreshUser } = useAuth();

  useEffect(() => {
    if (!sessionId) { setStatus("error"); return; }
    let cancelled = false;
    let attempts = 0;
    const poll = async () => {
      attempts++;
      try {
        const { data } = await api.get(`/payments/status/${sessionId}`);
        if (data.payment_status === "paid") {
          setStatus("paid");
          refreshUser();
          return;
        }
        if (attempts >= 20) { setStatus("timeout"); return; }
        if (!cancelled) setTimeout(poll, 2000);
      } catch {
        if (!cancelled) setTimeout(poll, 2000);
      }
    };
    poll();
    return () => { cancelled = true; };
  }, [sessionId]);

  return (
    <div className="marketing min-h-screen">
      <Nav dark />
      <div className="max-w-lg mx-auto px-6 py-24 text-center">
        <CheckCircle2 className={`w-16 h-16 mx-auto ${status === "paid" ? "text-emerald-400" : "text-neutral-500"}`} />
        <h1 className="font-display font-black text-4xl tracking-tighter mt-6" data-testid="payment-success-title">
          {status === "paid" ? "You're upgraded." : status === "polling" ? "Confirming payment…" : "Checking status…"}
        </h1>
        <p className="text-neutral-400 mt-4">
          {status === "paid" ? "Head back to your dashboard and export away." : "Give us a few seconds while Stripe confirms."}
        </p>
        {status === "paid" && (
          <Link to="/dashboard" className="mt-8 inline-block bg-white text-black px-6 py-3 font-mono-spec text-xs tracking-widest uppercase hover:bg-neutral-200 btn-industrial" data-testid="success-dashboard">
            Go to Dashboard
          </Link>
        )}
      </div>
    </div>
  );
}
