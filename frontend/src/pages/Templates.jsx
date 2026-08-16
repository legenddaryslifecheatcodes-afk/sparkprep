import { useNavigate } from "react-router-dom";
import Nav from "@/components/Nav";
import TemplateReader from "@/components/TemplateReader";
import { useAuth } from "@/context/AuthContext";

export default function Templates() {
  const nav = useNavigate();
  const { user } = useAuth();

  const handleSelect = (t) => {
    sessionStorage.setItem("sp_preset", JSON.stringify(t));
    if (user) nav("/dashboard");
    else nav("/register");
  };

  return (
    <div className="min-h-screen bg-[#F7F7F9]">
      <Nav dark={false} />
      <div className="max-w-7xl mx-auto px-6 py-12">
        <span className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">[ Distributor Spec Library ]</span>
        <h1 className="font-display font-black text-5xl md:text-6xl tracking-tighter mt-2">Publisher blueprints,<br />not design templates.</h1>
        <p className="text-neutral-600 mt-4 max-w-2xl leading-relaxed">
          Every card below is a certified spec sheet from Amazon KDP, IngramSpark, Barnes & Noble Press or Lulu — the exact trim, bleed, spine width, safe zone and PDF standard your book must match to be accepted for distribution.
        </p>
        <div className="mt-10">
          <TemplateReader onSelect={handleSelect} />
        </div>
      </div>
    </div>
  );
}
