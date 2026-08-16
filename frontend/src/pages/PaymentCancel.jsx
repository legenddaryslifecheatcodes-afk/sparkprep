import { Link } from "react-router-dom";
import Nav from "@/components/Nav";
import { XCircle } from "lucide-react";

export default function PaymentCancel() {
  return (
    <div className="marketing min-h-screen">
      <Nav dark />
      <div className="max-w-lg mx-auto px-6 py-24 text-center">
        <XCircle className="w-16 h-16 mx-auto text-neutral-500" />
        <h1 className="font-display font-black text-4xl tracking-tighter mt-6" data-testid="payment-cancel-title">Payment cancelled.</h1>
        <p className="text-neutral-400 mt-4">No charges made. Grab a plan whenever you're ready.</p>
        <Link to="/pricing" className="mt-8 inline-block bg-white text-black px-6 py-3 font-mono-spec text-xs tracking-widest uppercase hover:bg-neutral-200 btn-industrial" data-testid="cancel-pricing">Back to Pricing</Link>
      </div>
    </div>
  );
}
