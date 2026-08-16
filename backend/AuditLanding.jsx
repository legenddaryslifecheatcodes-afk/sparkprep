import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, fmtErr } from "@/lib/api";
import { toast } from "sonner";
import Nav from "@/components/Nav";
import SeasonBanner from "@/components/SeasonBanner";
import SeasonCountdown from "@/components/SeasonCountdown";
import BrandWatermark from "@/components/BrandWatermark";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Label } from "@/components/ui/label";
import { Upload, ShieldAlert, Zap, FileSearch, FileCheck2 } from "lucide-react";

const PROVENANCE_LABEL = { extracted: "Extracted", calculated: "Calculated", geometry: "From geometry", unresolved: "Unresolved" };

export default function AuditLanding() {
  const [specs, setSpecs] = useState(null);
  const [platform, setPlatform] = useState("kdp");
  const [trim, setTrim] = useState("6x9");
  const [uploading, setUploading] = useState(false);
  const [auditId, setAuditId] = useState(null);
  const [uploadingTemplate, setUploadingTemplate] = useState(false);
  const [detectedSpec, setDetectedSpec] = useState(null);
  const nav = useNavigate();

  useEffect(() => { api.get("/specs").then(({ data }) => setSpecs(data)); }, []);

  // Both the optional template upload and the final cover/interior upload
  // need to reference the same audit record, so create it lazily on
  // whichever action happens first instead of requiring a separate step.
  const ensureAuditId = async () => {
    if (auditId) return auditId;
    const { data } = await api.post("/audit/start", { platform, trim_size: trim });
    setAuditId(data.audit_id);
    return data.audit_id;
  };

  const onTemplateFile = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploadingTemplate(true);
    try {
      const id = await ensureAuditId();
      const fd = new FormData();
      fd.append("file", file);
      const { data } = await api.post(`/audit/${id}/template-upload`, fd, { headers: { "Content-Type": "multipart/form-data" } });
      setDetectedSpec(data.detected_spec);
      toast.success("Template read — using your publisher's real specs instead of the preset");
    } catch (err) {
      toast.error(fmtErr(err.response?.data?.detail) || "Could not read that template");
    } finally { setUploadingTemplate(false); }
  };

  const onFile = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    try {
      const id = await ensureAuditId();
      const fd = new FormData();
      fd.append("file", file);
      await api.post(`/audit/${id}/upload`, fd, { headers: { "Content-Type": "multipart/form-data" } });
      nav(`/audit/${id}/preview`);
    } catch (err) {
      toast.error(fmtErr(err.response?.data?.detail) || "Upload failed");
    } finally { setUploading(false); }
  };

  return (
    <div className="marketing min-h-screen noise-overlay">
      <SeasonBanner />
      <Nav dark />
      <section className="relative overflow-hidden">
        <BrandWatermark variant="dark" position="hero-left" scale={0.58} maxPx={720} opacity={0.24} />
        <div className="relative max-w-6xl mx-auto px-6 pt-20 pb-24 grid md:grid-cols-12 gap-8 items-start">
          <div className="md:col-span-7">
            <span className="font-mono-spec text-xs tracking-widest uppercase text-neutral-400 border border-neutral-700 px-3 py-1.5 inline-flex items-center gap-2" data-testid="audit-tag">
              <ShieldAlert className="w-3 h-3" /> [ 99¢ Failure Audit — No Signup ]
            </span>
            <h1 className="font-display font-black text-5xl md:text-7xl tracking-tighter mt-6 leading-[0.92]" data-testid="audit-title">
              Know exactly<br />why you'll get<br /><span className="text-neutral-500">rejected.</span>
            </h1>
            <p className="mt-6 text-lg text-neutral-400 max-w-xl leading-relaxed">
              IngramSpark, KDP, B&N — they'll reject your file with a two-line notice like <span className="font-mono-spec text-sm text-white">"resolution insufficient"</span> and no details. Drop your cover or interior here and we'll pinpoint every likely failure, quote the exact publisher rule you're breaking, and give you step-by-step fix instructions. Ninety-nine cents. No account.
            </p>
            <div className="mt-8 flex flex-wrap gap-3 text-xs font-mono-spec tracking-widest text-neutral-500 uppercase">
              <span className="border border-neutral-700 px-2 py-1">✓ Pinpoint every risk</span>
              <span className="border border-neutral-700 px-2 py-1">✓ Publisher rule cited</span>
              <span className="border border-neutral-700 px-2 py-1">✓ Fix steps + tools</span>
              <span className="border border-neutral-700 px-2 py-1">✓ Est. minutes to fix</span>
            </div>
          </div>

          <div className="md:col-span-5 space-y-4">
            <SeasonCountdown />
            <div className="marketing-surface p-6" data-testid="audit-form">
              <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500 mb-4">[ Step 01 · Template · Optional: upload your publisher's template ]</div>
              <p className="text-xs text-neutral-500 mb-3">Have the blank template PDF your distributor gave you (trim/bleed/spine guide)? Upload it first and we'll read the real, current specs straight out of it — no need to guess a preset below.</p>
              <label className={`block border-2 border-dashed p-5 text-center cursor-pointer hover:bg-white/5 transition-colors ${uploadingTemplate ? "border-white" : "border-neutral-800"}`} data-testid="audit-template-drop">
                <input type="file" accept=".pdf" onChange={onTemplateFile} disabled={uploadingTemplate} className="hidden" data-testid="audit-template-input" />
                <FileCheck2 className="w-6 h-6 mx-auto text-neutral-500" />
                <div className="font-display font-bold text-sm mt-2 text-white">{uploadingTemplate ? "Reading template…" : "Choose publisher template PDF"}</div>
              </label>
              {detectedSpec && !detectedSpec.error && (
                <div className="mt-3 grid grid-cols-2 gap-2" data-testid="audit-detected-spec">
                  {[
                    ["Trim width", detectedSpec.trim_width],
                    ["Trim height", detectedSpec.trim_height],
                    ["Spine width", detectedSpec.spine_width],
                    ["Bleed", detectedSpec.bleed],
                  ].map(([label, dim]) => dim?.value != null && (
                    <div key={label} className="bg-white/5 border border-neutral-800 p-2">
                      <div className="font-mono-spec text-[8px] tracking-widest uppercase text-neutral-500">{label}</div>
                      <div className="font-display font-bold text-white">{dim.value}in</div>
                      <div className="font-mono-spec text-[8px] tracking-widest uppercase text-emerald-500">{PROVENANCE_LABEL[dim.provenance] || dim.provenance}</div>
                    </div>
                  ))}
                </div>
              )}

              <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500 mt-6 mb-4">[ Step 02 · Set target ]</div>
              {auditId && <p className="text-xs text-neutral-500 mb-3">Locked once you've started uploading — start over to change platform or trim size.</p>}
              <div className="space-y-4">
                <div>
                  <Label className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Distributor</Label>
                  <Select value={platform} onValueChange={setPlatform} disabled={!!auditId}>
                    <SelectTrigger className="mt-1 bg-transparent border-neutral-700 text-white" data-testid="audit-platform"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {specs && Object.entries(specs.platforms).map(([k, v]) => (
                        <SelectItem key={k} value={k}>{v.name}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div>
                  <Label className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Trim size {detectedSpec?.trim_width?.value && <span className="text-emerald-500 normal-case">(overridden by your template above)</span>}</Label>
                  <Select value={trim} onValueChange={setTrim} disabled={!!auditId}>
                    <SelectTrigger className="mt-1 bg-transparent border-neutral-700 text-white" data-testid="audit-trim"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {specs && Object.entries(specs.trim_sizes).map(([k, v]) => (
                        <SelectItem key={k} value={k}>{v.label}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </div>

              <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500 mt-6 mb-3">[ Step 03 · Upload · Drop your cover or interior file ]</div>
              <label className={`block border-2 border-dashed p-8 text-center cursor-pointer hover:bg-white/5 transition-colors ${uploading ? "border-white" : "border-neutral-700"}`} data-testid="audit-drop">
                <input type="file" accept=".pdf,.jpg,.jpeg,.png,.tif,.tiff,.webp" onChange={onFile} disabled={uploading} className="hidden" data-testid="audit-file-input" />
                <Upload className="w-8 h-8 mx-auto text-neutral-500" />
                <div className="font-display font-bold text-lg mt-3 text-white">{uploading ? "Analyzing…" : "Choose file"}</div>
                <div className="font-mono-spec text-[10px] tracking-widest text-neutral-500 mt-1 uppercase">PDF · JPG · PNG · TIFF · WebP (up to 50 MB)</div>
              </label>
              <p className="mt-6 font-mono-spec text-[10px] tracking-widest text-neutral-500 uppercase leading-relaxed">
                Preview is free. Unlock the full pinpointed report for <span className="text-white">$0.99</span>. No subscription, no account.
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* Why */}
      <section className="border-t border-neutral-900">
        <div className="max-w-6xl mx-auto px-6 py-20 grid md:grid-cols-3 gap-4">
          {[
            { icon: FileSearch, title: "Every likely failure, pinpointed", body: "We check resolution, color space, transparency, bleed, PDF/X-1a and spine formula — and flag exactly which will cause a rejection." },
            { icon: ShieldAlert, title: "The exact publisher rule you're breaking", body: "Each finding cites the specific IngramSpark / KDP / B&N guideline section — so you know it's real, not guesswork." },
            { icon: Zap, title: "Step-by-step fix instructions", body: "For every issue: what to click, what tools you'll need, and how many minutes it'll take. No detective work." },
          ].map((f, i) => (
            <div key={i} className="marketing-surface p-6" data-testid={`audit-feature-${i}`}>
              <f.icon className="w-5 h-5 text-white" />
              <h3 className="font-display font-bold text-xl mt-4 tracking-tight text-white">{f.title}</h3>
              <p className="text-sm text-neutral-400 mt-2 leading-relaxed">{f.body}</p>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
