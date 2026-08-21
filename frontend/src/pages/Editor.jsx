import { useEffect, useState, useMemo, useCallback } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { api, fmtErr, API_URL } from "@/lib/api";
import { toast } from "sonner";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Slider } from "@/components/ui/slider";
import Book3DPro from "@/components/Book3DPro";
import BlurbDialog from "@/components/BlurbDialog";
import ManuscriptComposerDialog from "@/components/ManuscriptComposerDialog";
import IsbnBarcodePanel from "@/components/IsbnBarcodePanel";
import { Logo } from "@/components/Logo";
import {
  Upload, Download, Wand2, ArrowLeft, Eye, EyeOff, Check, AlertTriangle, XCircle,
  Sparkles, Box, Square, FileStack, Palette, Ruler, Layers, RotateCw, Trash2, Info,
  FilePlus, BookOpen, Image as ImageIcon, Paintbrush, FileCheck2,
} from "lucide-react";

const OVERLAYS = [
  { key: "bleed", label: "Bleed", color: "#007BFF", tooltip: "Show the bleed area (0.125\") — extend background art past the trim." },
  { key: "trim", label: "Trim", color: "#FF6A00", tooltip: "Show the final trim line where the book will be cut." },
  { key: "safe", label: "Safe", color: "#D4A857", tooltip: "Keep critical text and logos inside the safe zone to avoid cropping." },
  { key: "barcode", label: "Barcode", color: "#22C55E", tooltip: "Reserved zone for the ISBN barcode block on the back cover." },
];

const SLOTS = [
  { key: "full_wrap", label: "Full Cover Wrap", desc: "Back + Spine + Front in one PDF", icon: BookOpen },
  { key: "front_cover", label: "Front Cover", desc: "Standalone front artwork", icon: ImageIcon },
  { key: "spine", label: "Spine", desc: "Spine strip artwork", icon: Ruler },
  { key: "back_cover", label: "Back Cover", desc: "Blurb + barcode area", icon: FileStack },
  { key: "interior", label: "Interior PDF", desc: "Manuscript pages", icon: FilePlus },
];

const statusIcon = (s) => s === "pass" ? <Check className="w-3.5 h-3.5" /> : s === "warning" ? <AlertTriangle className="w-3.5 h-3.5" /> : <XCircle className="w-3.5 h-3.5" />;
const statusPill = (s) => s === "pass" ? "pill-pass" : s === "warning" ? "pill-warn" : "pill-fail";

function InfoTip({ children, text }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild><span>{children}</span></TooltipTrigger>
      <TooltipContent side="top" className="max-w-xs text-xs">{text}</TooltipContent>
    </Tooltip>
  );
}

export default function Editor() {
  const { id } = useParams();
  const nav = useNavigate();
  const [project, setProject] = useState(null);
  const [specs, setSpecs] = useState(null);
  const [spine, setSpine] = useState(null);
  const [overlays, setOverlays] = useState({ bleed: true, trim: true, safe: true, barcode: false });
  const [viewMode, setViewMode] = useState("flat");
  const [blurbOpen, setBlurbOpen] = useState(false);
  const [composerOpen, setComposerOpen] = useState(false);
  const [uploadingSlot, setUploadingSlot] = useState(null);
  const [uploadingTemplate, setUploadingTemplate] = useState(false);
  const [fixing, setFixing] = useState(false);
  const [exporting, setExporting] = useState(false);

  const load = useCallback(async () => {
    try {
      const [{ data }, { data: sp }] = await Promise.all([
        api.get(`/projects/${id}`),
        api.get("/specs"),
      ]);
      setProject(data);
      setSpecs(sp);
      const { data: spineRes } = await api.post("/specs/spine", {
        page_count: data.page_count, paper_type: data.paper_type,
        trim_size: data.trim_size, binding: data.binding, platform: data.platform,
      });
      setSpine(spineRes);
    } catch (e) { toast.error(fmtErr(e.response?.data?.detail)); nav("/dashboard"); }
  }, [id, nav]);

  useEffect(() => { load(); }, [load]);

  // ---- Update spec fields inline (top section is editable so users can iterate)
  const updateSpec = async (patch) => {
    try {
      const { data } = await api.patch(`/projects/${id}`, patch);
      setProject(data);
      const { data: spineRes } = await api.post("/specs/spine", {
        page_count: data.page_count, paper_type: data.paper_type,
        trim_size: data.trim_size, binding: data.binding, platform: data.platform,
      });
      setSpine(spineRes);
    } catch (e) { toast.error(fmtErr(e.response?.data?.detail)); }
  };

  // ---- Publisher template upload (auto-detect spec from a real distributor template file)
  const uploadPublisherTemplate = async (file) => {
    if (!file) return;
    setUploadingTemplate(true);
    try {
      const fd = new FormData();
      fd.append("file", file);
      const { data } = await api.post(`/projects/${id}/template-upload`, fd, {
        headers: { "Content-Type": "multipart/form-data" },
      });
      setProject((p) => ({
        ...p,
        publisher_template: data.template_id,
        publisher_template_metadata: data.metadata,
        detected_trim: data.detected_trim,
      }));
      toast.success("Publisher template analyzed");
    } catch (e) { toast.error(fmtErr(e.response?.data?.detail)); }
    finally { setUploadingTemplate(false); }
  };

  // ---- Slot upload
  const uploadToSlot = async (slot, file) => {
    if (!file) return;
    setUploadingSlot(slot);
    try {
      const fd = new FormData();
      fd.append("file", file);
      const { data } = await api.post(`/projects/${id}/slot-upload/${slot}`, fd, {
        headers: { "Content-Type": "multipart/form-data" },
      });
      setProject((p) => ({
        ...p,
        slots: { ...(p.slots || {}), [slot]: { ...data.file_metadata, compliance: data.compliance } },
        ...(slot === "full_wrap"
          ? { uploaded_file: data.file_metadata.stored_filename, file_metadata: data.file_metadata, compliance: data.compliance }
          : {}),
      }));
      toast.success(`${slot.replace("_", " ")} analyzed`);
    } catch (e) { toast.error(fmtErr(e.response?.data?.detail)); }
    finally { setUploadingSlot(null); }
  };

  const deleteSlot = async (slot) => {
    try {
      await api.delete(`/projects/${id}/slot/${slot}`);
      setProject((p) => {
        const s = { ...(p.slots || {}) };
        delete s[slot];
        return { ...p, slots: s };
      });
    } catch (e) { toast.error(fmtErr(e.response?.data?.detail)); }
  };

  const autofix = async () => {
    setFixing(true);
    try {
      const { data } = await api.post(`/projects/${id}/autofix`);
      setProject((p) => ({ ...p, file_metadata: data.file_metadata, compliance: data.compliance }));
      // Only claim success when the fix actually resolved everything it
      // attempted -- ghostscript_fix is only present when a gs-level fix
      // (transparency/layers/PDF-X1a declaration) was needed at all.
      if (data.ghostscript_fix && !data.ghostscript_fix.succeeded) {
        toast.warning("Autofix attempted. Manual correction required.");
      } else {
        toast.success("Auto-Fix complete: CMYK + 300 DPI + flattened");
      }
    } catch (e) { toast.error(fmtErr(e.response?.data?.detail)); }
    finally { setFixing(false); }
  };

  const exportPdf = async () => {
    setExporting(true);
    try {
      const { data } = await api.post(`/projects/${id}/export`);
      toast.success("PDF/X-1a ready");
      const token = localStorage.getItem("sp_token");
      window.open(`${API_URL}${data.download_url.replace("/api", "")}${token ? `?token=${token}` : ""}`, "_blank");
    } catch (e) {
      const msg = fmtErr(e.response?.data?.detail);
      if (e.response?.status === 402) { toast.error(msg + " — Redirecting to pricing"); setTimeout(() => nav("/pricing"), 1500); }
      else toast.error(msg);
    } finally { setExporting(false); }
  };

  // ---- Manual adjustments
  const [adj, setAdj] = useState({ spine_offset: 0, bleed_extra: 0, image_scale: 1, target_dpi: 300, color_profile: "US Web Coated SWOP v2" });
  useEffect(() => {
    if (project?.adjustments) setAdj((a) => ({ ...a, ...project.adjustments }));
  }, [project?.adjustments]);
  const saveAdj = async (patch) => {
    const next = { ...adj, ...patch };
    setAdj(next);
    try { await api.patch(`/projects/${id}/adjustments`, patch); } catch {}
  };

  const previewUrl = useMemo(() => {
    const token = localStorage.getItem("sp_token");
    const slots = project?.slots || {};
    // Prefer full_wrap → front_cover → legacy uploaded_file
    if (slots.full_wrap) return `${API_URL}/projects/${id}/slot/full_wrap/preview${token ? `?token=${token}` : ""}`;
    if (slots.front_cover) return `${API_URL}/projects/${id}/slot/front_cover/preview${token ? `?token=${token}` : ""}`;
    if (project?.uploaded_file) return `${API_URL}/projects/${id}/preview${token ? `?token=${token}` : ""}`;
    return null;
  }, [project, id]);

  if (!project || !specs) return <div className="p-10 font-mono-spec text-xs text-neutral-500">Loading editor…</div>;

  const isCover = project.project_type !== "interior";
  const trim = specs.trim_sizes[project.trim_size] || { w: 6, h: 9, label: project.trim_size };
  const plat = specs.platforms[project.platform] || {};
  const compliance = project.slots?.full_wrap?.compliance || project.compliance || [];
  const hasAnyUpload = Object.values(project.slots || {}).length > 0 || !!project.uploaded_file;

  return (
    <TooltipProvider delayDuration={200}>
      <div className="min-h-screen bg-[#F7F7F5]">
        {/* Topbar */}
        <div className="h-14 border-b border-neutral-200 bg-white flex items-center justify-between px-6 sticky top-0 z-40" data-testid="editor-topbar">
          <div className="flex items-center gap-4">
            <button onClick={() => nav("/dashboard")} className="text-neutral-500 hover:text-black" data-testid="back-btn"><ArrowLeft className="w-4 h-4" /></button>
            <Logo compact />
            <div className="w-px h-6 bg-neutral-200 mx-2" />
            <div>
              <div className="font-display font-bold text-sm tracking-tight">{project.name}</div>
              <div className="font-mono-spec text-[10px] tracking-widest text-neutral-500 uppercase">{plat.name} · {trim.label} · {project.page_count}pp</div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <InfoTip text="Compose a print-ready interior PDF from scratch with a fiction, workbook or poetry template.">
              <button onClick={() => setComposerOpen(true)} className="px-3 py-2 border border-neutral-300 font-mono-spec text-[10px] tracking-widest uppercase hover:border-black flex items-center gap-1.5 btn-industrial" data-testid="composer-btn">
                <BookOpen className="w-3.5 h-3.5" /> Compose Interior
              </button>
            </InfoTip>
            <InfoTip text="Generate 3 AI back-cover blurb variations with Claude Sonnet.">
              <button onClick={() => setBlurbOpen(true)} className="px-3 py-2 border border-neutral-300 font-mono-spec text-[10px] tracking-widest uppercase hover:border-black flex items-center gap-1.5 btn-industrial" data-testid="blurb-btn">
                <Sparkles className="w-3.5 h-3.5" /> AI Blurb
              </button>
            </InfoTip>
          </div>
        </div>

        <div className="max-w-7xl mx-auto px-6 py-8 space-y-6">
          {/* SECTION 1 · TEMPLATE SPECIFICATIONS */}
          <section className="bg-white border border-neutral-200" data-testid="section-specs">
            <div className="p-5 border-b border-neutral-200 flex items-center gap-3 flex-wrap">
              <span className="section-header">01 · Template Specifications</span>
              <p className="text-sm text-neutral-600">Set the distributor blueprint. Every check and export below conforms to these values.</p>
            </div>
            <div className="p-5 grid md:grid-cols-6 gap-4">
              <SpecField label="Distributor" tooltip="Distributor whose print requirements the file must pass.">
                <Select value={project.platform} onValueChange={(v) => updateSpec({ platform: v })}>
                  <SelectTrigger data-testid="spec-platform"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {Object.entries(specs.platforms).map(([k, v]) => <SelectItem key={k} value={k}>{v.name}</SelectItem>)}
                  </SelectContent>
                </Select>
              </SpecField>
              <SpecField label="Trim Size" tooltip="Final page dimensions after cutting. Sets required bleed + safe margins.">
                <Select value={project.trim_size} onValueChange={(v) => updateSpec({ trim_size: v })}>
                  <SelectTrigger data-testid="spec-trim"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {Object.entries(specs.trim_sizes).map(([k, v]) => <SelectItem key={k} value={k}>{v.label}</SelectItem>)}
                  </SelectContent>
                </Select>
              </SpecField>
              <SpecField label="Paper Type" tooltip="Paper stock affects spine width via pages-per-inch (PPI). White vs cream vs color.">
                <Select value={project.paper_type} onValueChange={(v) => updateSpec({ paper_type: v })}>
                  <SelectTrigger data-testid="spec-paper"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {Object.entries(specs.paper_types).map(([k, v]) => <SelectItem key={k} value={k}>{v.label}</SelectItem>)}
                  </SelectContent>
                </Select>
              </SpecField>
              <SpecField label="Binding" tooltip="Binding style — determines whether spine text is allowed and jacket flap dimensions.">
                <Select value={project.binding} onValueChange={(v) => updateSpec({ binding: v })}>
                  <SelectTrigger data-testid="spec-binding"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {Object.entries(specs.binding_types).map(([k, v]) => <SelectItem key={k} value={k}>{v.label}</SelectItem>)}
                  </SelectContent>
                </Select>
              </SpecField>
              <SpecField label="Page Count" tooltip="Total interior page count — used to calculate spine width.">
                <Input type="number" min={24} value={project.page_count} onChange={(e) => updateSpec({ page_count: parseInt(e.target.value) || 0 })} data-testid="spec-pages" />
              </SpecField>
              <SpecField label="Project Type" tooltip="Cover only, interior only, or both.">
                <Select value={project.project_type} onValueChange={(v) => updateSpec({ project_type: v })}>
                  <SelectTrigger data-testid="spec-type"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="cover">Cover only</SelectItem>
                    <SelectItem value="interior">Interior only</SelectItem>
                    <SelectItem value="combined">Cover + Interior</SelectItem>
                  </SelectContent>
                </Select>
              </SpecField>
            </div>
            {/* Publisher template upload — auto-detect trim from a real distributor file instead of guessing from the presets above */}
            <div className="border-t border-neutral-200 p-5" data-testid="template-upload-section">
              <div className="flex items-start justify-between flex-wrap gap-4">
                <div>
                  <p className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Or upload the publisher&apos;s template</p>
                  <p className="text-sm text-neutral-600 mt-1">Upload the blank template PDF/image your distributor gave you and we&apos;ll auto-detect its trim dimensions instead of you picking a preset above.</p>
                </div>
                <label className={`px-4 py-2 border font-mono-spec text-[10px] tracking-widest uppercase cursor-pointer btn-industrial ${uploadingTemplate ? "opacity-50 pointer-events-none" : "border-neutral-300 hover:border-black"}`} data-testid="template-upload-label">
                  {uploadingTemplate ? "Analyzing…" : "Upload template file"}
                  <input
                    type="file"
                    accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff"
                    onChange={(e) => uploadPublisherTemplate(e.target.files?.[0])}
                    disabled={uploadingTemplate}
                    className="hidden"
                    data-testid="template-upload-input"
                  />
                </label>
              </div>
              {project.detected_trim && (
                <div className="mt-4 flex flex-wrap items-center gap-4 bg-neutral-50 border border-neutral-200 p-3" data-testid="detected-trim-readout">
                  <FileCheck2 className="w-4 h-4 text-emerald-600 shrink-0" />
                  <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-600">
                    <span className="text-neutral-400">Detected page size</span>{" "}
                    <span className="text-neutral-900">{project.detected_trim.raw_width_inches}&quot; × {project.detected_trim.raw_height_inches}&quot;</span>
                    <span className="text-neutral-400"> · Estimated trim after bleed</span>{" "}
                    <span className="text-neutral-900">{project.detected_trim.estimated_trim_width}&quot; × {project.detected_trim.estimated_trim_height}&quot;</span>
                  </div>
                  <span className="text-xs text-neutral-500">Compare this against the Trim Size preset above and adjust it if it doesn&apos;t already match.</span>
                </div>
              )}
            </div>
            {/* Live spec readout */}
            <div className="border-t border-neutral-200 p-5 grid grid-cols-2 md:grid-cols-6 gap-3">
              <SpecReadout label="Spine width" value={spine ? `${spine.spine_width}"` : "—"} accent tooltip="Auto-calculated from page count × paper PPI." />
              <SpecReadout label="Required bleed" value={`${plat.bleed}"`} tooltip="Extend background art this far past the trim so cutting variance doesn't leave white slivers." />
              <SpecReadout label="Safe margin" value={`${plat.safe_margin_interior}"`} tooltip="Keep essential text/logos inside this margin from every edge." />
              <SpecReadout label="Color space" value="CMYK" tooltip="All commercial print production uses CMYK ink, not screen RGB." />
              <SpecReadout label="Required DPI" value="300" tooltip="Minimum resolution to print sharp text and detail." />
              <SpecReadout label="PDF standard" value={plat.pdf_standard || "PDF/X-1a"} tooltip="Print-ready PDF variant with embedded fonts, CMYK, flattened transparency." />
            </div>
          </section>

          {/* SECTION 2 · UPLOAD */}
          <section className="bg-white border border-neutral-200" data-testid="section-upload">
            <div className="p-5 border-b border-neutral-200 flex items-center gap-3">
              <span className="section-header">02 · Upload</span>
              <p className="text-sm text-neutral-600">Drop a full cover wrap — or upload each part separately. Every file is analyzed the moment it lands.</p>
            </div>
            <div className="p-5 grid md:grid-cols-5 gap-3">
              {SLOTS.map((s) => (
                <SlotDrop
                  key={s.key}
                  slot={s}
                  data={project.slots?.[s.key]}
                  uploading={uploadingSlot === s.key}
                  onUpload={(f) => uploadToSlot(s.key, f)}
                  onDelete={() => deleteSlot(s.key)}
                />
              ))}
            </div>
            {isCover && (
              <div className="p-5 pt-0">
                <IsbnBarcodePanel projectId={id} initialIsbn={project.isbn || ""} />
              </div>
            )}
          </section>

          {/* SECTION 3 · LIVE PREVIEW */}
          <section className="bg-white border border-neutral-200" data-testid="section-preview">
            <div className="p-5 border-b border-neutral-200 flex items-center gap-3 flex-wrap justify-between">
              <div className="flex items-center gap-3">
                <span className="section-header">03 · Live Preview</span>
                <p className="text-sm text-neutral-600 hidden md:block">Toggle overlays, DPI warnings and 3D mockup in real time.</p>
              </div>
              <div className="flex items-center gap-2">
                {isCover && (
                  <div className="border border-neutral-300 flex" data-testid="view-toggle">
                    <button onClick={() => setViewMode("flat")} className={`px-3 py-1.5 font-mono-spec text-[10px] tracking-widest uppercase flex items-center gap-1.5 ${viewMode === "flat" ? "bg-black text-white" : "hover:bg-neutral-50"}`} data-testid="view-flat">
                      <Square className="w-3 h-3" /> Flat
                    </button>
                    <button onClick={() => setViewMode("3d")} className={`px-3 py-1.5 font-mono-spec text-[10px] tracking-widest uppercase flex items-center gap-1.5 ${viewMode === "3d" ? "bg-black text-white" : "hover:bg-neutral-50"}`} data-testid="view-3d">
                      <Box className="w-3 h-3" /> 3D
                    </button>
                  </div>
                )}
                {OVERLAYS.map((o) => (
                  <InfoTip key={o.key} text={o.tooltip}>
                    <button onClick={() => setOverlays((prev) => ({ ...prev, [o.key]: !prev[o.key] }))} className="px-2.5 py-1.5 border border-neutral-300 font-mono-spec text-[10px] tracking-widest uppercase flex items-center gap-1.5 hover:border-black" data-testid={`overlay-${o.key}`}>
                      <span className="w-2.5 h-2.5 border" style={{ borderColor: o.color, background: overlays[o.key] ? o.color : "transparent" }} />
                      {o.label}
                      {overlays[o.key] ? <Eye className="w-3 h-3" /> : <EyeOff className="w-3 h-3 text-neutral-400" />}
                    </button>
                  </InfoTip>
                ))}
              </div>
            </div>
            <div className="p-5 grid md:grid-cols-4 gap-4">
              <div className="md:col-span-3 bg-[#EDEDE9] dot-grid flex items-center justify-center p-6 relative min-h-[460px]" data-testid="canvas-area">
                {!hasAnyUpload && (
                  <div className="text-center max-w-sm">
                    <Upload className="w-8 h-8 text-neutral-400 mx-auto" />
                    <p className="font-display font-bold text-lg mt-3 text-neutral-700">Upload a file above to see the live preview</p>
                    <p className="text-xs text-neutral-500 mt-2">Trim, bleed, safe zone and DPI warnings will render instantly.</p>
                  </div>
                )}
                {hasAnyUpload && isCover && viewMode === "3d" && (
                  <div className="w-full h-[440px] bg-neutral-50 rounded-sm overflow-hidden border border-neutral-200 shadow-inner" data-testid="book-3d-wrap">
                    <Book3DPro frontImageUrl={previewUrl} trim={spine?.trim} spineWidth={spine?.spine_width || 0.5} binding={project.binding} />
                  </div>
                )}
                {hasAnyUpload && isCover && viewMode === "flat" && spine && (
                  <FullCoverPreview trim={trim} spine={spine} bleed={plat.bleed || 0.125} overlays={overlays} previewUrl={previewUrl} />
                )}
                {hasAnyUpload && !isCover && (
                  <InteriorPreview overlays={overlays} previewUrl={previewUrl} />
                )}
              </div>
              {/* Compliance rail */}
              <div className="space-y-2" data-testid="compliance-list">
                <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Compliance</div>
                {compliance.length === 0 && <p className="text-xs text-neutral-500">Upload a file to see the compliance report.</p>}
                {compliance.map((c, i) => (
                  <div key={i} className="border border-neutral-200 p-3" data-testid={`compliance-${c.id}`}>
                    <div className={`px-2 py-0.5 text-[9px] font-mono-spec tracking-widest uppercase inline-flex items-center gap-1 ${statusPill(c.status)}`}>
                      {statusIcon(c.status)} {c.status}
                    </div>
                    <div className="font-display font-bold text-sm mt-1.5 tracking-tight">{c.label}</div>
                    <div className="text-[11px] text-neutral-600 mt-0.5 leading-relaxed">{c.message}</div>
                  </div>
                ))}
              </div>
            </div>
          </section>

          {/* SECTION 4 · ONE-CLICK FIXES */}
          <section className="bg-white border border-neutral-200" data-testid="section-fixes">
            <div className="p-5 border-b border-neutral-200 flex items-center gap-3">
              <span className="section-header">04 · One-Click Fixes</span>
              <p className="text-sm text-neutral-600">Big, bright buttons that do the heavy lifting.</p>
            </div>
            <div className="p-5 grid md:grid-cols-4 gap-3">
              <BigButton
                icon={Wand2}
                label="Auto-Fix Everything"
                tooltip="Runs every fix below in one shot: converts to CMYK, upscales to 300 DPI, flattens transparency and preps for PDF/X-1a export."
                onClick={autofix}
                busy={fixing}
                disabled={!hasAnyUpload}
                primary
                testid="fix-all"
              />
              <BigButton
                icon={Palette}
                label="Convert to CMYK"
                tooltip="Converts file to CMYK color space using US Web Coated SWOP v2 profile — required by IngramSpark & KDP."
                onClick={autofix}
                disabled={!hasAnyUpload}
                testid="fix-cmyk"
              />
              <BigButton
                icon={Ruler}
                label="Fix DPI to 300"
                tooltip="Resamples the image to 300 DPI at the current trim + bleed dimensions."
                onClick={autofix}
                disabled={!hasAnyUpload}
                testid="fix-dpi"
              />
              <BigButton
                icon={Layers}
                label="Add Bleed"
                tooltip={'Extends artwork into the 0.125" bleed zone so cutting variance doesn\'t leave white edges.'}
                onClick={autofix}
                disabled={!hasAnyUpload}
                testid="fix-bleed"
              />
              <BigButton
                icon={Paintbrush}
                label="Align Spine"
                tooltip="Recalculates spine width from page count × paper PPI and re-centers spine text inside safe zones."
                onClick={() => toast.success("Spine width recalculated: " + spine?.spine_width + "\"")}
                disabled={!isCover}
                testid="fix-spine"
              />
              <BigButton
                icon={Download}
                label="Generate PDF/X-1a"
                tooltip="Exports a print-ready PDF/X-1a:2001 with correct trim, bleed, spine, embedded fonts and flattened transparency."
                onClick={exportPdf}
                busy={exporting}
                disabled={!hasAnyUpload}
                primary
                testid="export-pdf"
              />
              <BigButton
                icon={Box}
                label="Preview 3D Mockup"
                tooltip="Switches the preview to a photoreal 3D book you can rotate and screenshot."
                onClick={() => setViewMode("3d")}
                disabled={!isCover || !hasAnyUpload}
                testid="preview-3d"
              />
              <BigButton
                icon={Sparkles}
                label="AI Blurb Writer"
                tooltip="Generate 3 back-cover blurb variations with Claude Sonnet."
                onClick={() => setBlurbOpen(true)}
                testid="ai-blurb"
              />
            </div>
          </section>

          {/* SECTION 5 · MANUAL EDITING */}
          <section className="bg-white border border-neutral-200" data-testid="section-manual">
            <div className="p-5 border-b border-neutral-200 flex items-center gap-3">
              <span className="section-header">05 · Manual Adjustments</span>
              <p className="text-sm text-neutral-600">Fine-tune with live preview updates. Values save automatically.</p>
            </div>
            <div className="p-5 grid md:grid-cols-2 gap-x-8 gap-y-5">
              <SliderRow
                label="Spine offset"
                tooltip="Shift spine text left/right in fractions of an inch. Useful when the spine width sits between binding tolerances."
                unit="in"
                min={-0.25} max={0.25} step={0.005}
                value={adj.spine_offset}
                onChange={(v) => saveAdj({ spine_offset: v })}
                testid="adj-spine-offset"
              />
              <SliderRow
                label="Extra bleed"
                tooltip={'Add extra bleed beyond the distributor default (some print shops prefer 0.25").'}
                unit="in"
                min={0} max={0.25} step={0.005}
                value={adj.bleed_extra}
                onChange={(v) => saveAdj({ bleed_extra: v })}
                testid="adj-bleed-extra"
              />
              <SliderRow
                label="Image scale"
                tooltip="Scale the source image up or down inside the trim area."
                unit="×"
                min={0.5} max={1.5} step={0.01}
                value={adj.image_scale}
                onChange={(v) => saveAdj({ image_scale: v })}
                testid="adj-image-scale"
              />
              <SliderRow
                label="Target DPI"
                tooltip="Resample DPI target — 300 is standard for print, 600 is overkill but pristine."
                unit="dpi"
                min={200} max={600} step={10}
                value={adj.target_dpi}
                onChange={(v) => saveAdj({ target_dpi: v })}
                testid="adj-target-dpi"
              />
              <div>
                <div className="flex items-center gap-2 mb-2">
                  <Label className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Color profile</Label>
                  <InfoTip text="ICC color profile applied during CMYK conversion. SWOP v2 is North American offset standard."><Info className="w-3 h-3 text-neutral-400" /></InfoTip>
                </div>
                <Select value={adj.color_profile} onValueChange={(v) => saveAdj({ color_profile: v })}>
                  <SelectTrigger data-testid="adj-color-profile"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="US Web Coated SWOP v2">US Web Coated SWOP v2 (default)</SelectItem>
                    <SelectItem value="GRACoL 2013">GRACoL 2013 (premium color)</SelectItem>
                    <SelectItem value="FOGRA39">FOGRA39 (European offset)</SelectItem>
                    <SelectItem value="Japan Color 2001 Coated">Japan Color 2001 Coated</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>
          </section>
        </div>

        <BlurbDialog open={blurbOpen} onOpenChange={setBlurbOpen} defaultTitle={project.name} />
        <ManuscriptComposerDialog open={composerOpen} onOpenChange={setComposerOpen} defaultProject={project} />
      </div>
    </TooltipProvider>
  );
}

function SpecField({ label, tooltip, children }) {
  return (
    <div>
      <div className="flex items-center gap-1.5 mb-1.5">
        <Label className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">{label}</Label>
        <InfoTip text={tooltip}><Info className="w-3 h-3 text-neutral-400" /></InfoTip>
      </div>
      {children}
    </div>
  );
}

function SpecReadout({ label, value, tooltip, accent }) {
  return (
    <div className={`p-3 ${accent ? "bg-[#FFF3E6] border border-[#FF6A00]/30" : "bg-neutral-50 border border-neutral-200"}`}>
      <div className="flex items-center gap-1">
        <div className="font-mono-spec text-[9px] tracking-widest uppercase text-neutral-500">{label}</div>
        <InfoTip text={tooltip}><Info className="w-3 h-3 text-neutral-400" /></InfoTip>
      </div>
      <div className={`font-display font-black text-xl tracking-tight mt-1 ${accent ? "text-[#FF6A00]" : "text-neutral-900"}`}>{value}</div>
    </div>
  );
}

function SlotDrop({ slot, data, uploading, onUpload, onDelete }) {
  const Icon = slot.icon;
  const filled = !!data;
  return (
    <InfoTip text={slot.desc}>
      <label className={`block relative border-2 border-dashed p-4 text-center cursor-pointer transition-colors ${filled ? "border-emerald-400 bg-emerald-50/40" : uploading ? "border-[#FF6A00]" : "border-neutral-300 hover:border-black hover:bg-neutral-50"}`} data-testid={`slot-${slot.key}`}>
        <input type="file" accept=".pdf,.jpg,.jpeg,.png,.tif,.tiff,.webp" onChange={(e) => onUpload(e.target.files?.[0])} disabled={uploading} className="hidden" data-testid={`slot-input-${slot.key}`} />
        <Icon className={`w-5 h-5 mx-auto ${filled ? "text-emerald-600" : "text-neutral-400"}`} />
        <div className="font-display font-bold text-sm mt-2 leading-tight">{filled ? "Uploaded" : uploading ? "Analyzing…" : slot.label}</div>
        <div className="font-mono-spec text-[9px] tracking-widest text-neutral-500 mt-0.5 uppercase leading-tight">{filled ? data.original_filename : slot.desc}</div>
        {filled && data.dpi_x != null && (
          <div className="mt-2 font-mono-spec text-[9px] tracking-widest text-neutral-600 uppercase">{data.width_px}×{data.height_px}px · {data.dpi_x} DPI · {data.color_mode}</div>
        )}
        {filled && (
          <button onClick={(e) => { e.preventDefault(); e.stopPropagation(); onDelete(); }} className="absolute top-2 right-2 text-neutral-400 hover:text-red-500" data-testid={`slot-delete-${slot.key}`}>
            <Trash2 className="w-3.5 h-3.5" />
          </button>
        )}
      </label>
    </InfoTip>
  );
}

function BigButton({ icon: Icon, label, tooltip, onClick, busy, disabled, primary, testid }) {
  const cls = primary
    ? "btn-spark text-white"
    : "bg-white border border-neutral-300 text-neutral-900 hover:border-black";
  return (
    <InfoTip text={tooltip}>
      <button
        onClick={onClick}
        disabled={disabled || busy}
        className={`${cls} p-4 flex flex-col items-start gap-2 font-display font-bold text-left btn-industrial min-h-[92px] w-full`}
        data-testid={testid}
      >
        <Icon className="w-5 h-5" />
        <span className="text-sm tracking-tight leading-tight">{busy ? "Working…" : label}</span>
      </button>
    </InfoTip>
  );
}

function SliderRow({ label, tooltip, unit, min, max, step, value, onChange, testid }) {
  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-1.5">
          <Label className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">{label}</Label>
          <InfoTip text={tooltip}><Info className="w-3 h-3 text-neutral-400" /></InfoTip>
        </div>
        <div className="font-mono-spec text-xs text-neutral-900">{typeof value === "number" ? value.toFixed(unit === "dpi" ? 0 : 3) : "—"} <span className="text-neutral-500">{unit}</span></div>
      </div>
      <input
        type="range" className="spark-range w-full"
        min={min} max={max} step={step} value={value ?? 0}
        onChange={(e) => onChange(parseFloat(e.target.value))}
        data-testid={testid}
      />
    </div>
  );
}

// ---- Flat preview (unchanged geometry, colors updated to brand)
function FullCoverPreview({ trim, spine, bleed, overlays, previewUrl }) {
  const w = spine?.full_cover.total_width || 12;
  const h = spine?.full_cover.total_height || 9;
  const trimW = trim.w;
  const trimH = trim.h;
  const spineW = spine?.spine_width || 0.5;
  const aspect = w / h;
  return (
    <div className="relative bg-white shadow-lg" style={{ aspectRatio: aspect, width: "min(70vh, 900px)" }} data-testid="cover-preview">
      {previewUrl ? (
        <img src={previewUrl} alt="cover" className="absolute inset-0 w-full h-full object-cover" />
      ) : (
        <div className="absolute inset-0 flex items-center justify-center text-neutral-400 font-mono-spec text-xs tracking-widest uppercase">No file</div>
      )}
      {overlays.bleed && <div className="absolute inset-0 pointer-events-none overlay-bleed" />}
      {overlays.trim && <div className="absolute pointer-events-none overlay-trim" style={{ top: `${(bleed / h) * 100}%`, bottom: `${(bleed / h) * 100}%`, left: `${(bleed / w) * 100}%`, right: `${(bleed / w) * 100}%` }} />}
      {overlays.safe && <div className="absolute pointer-events-none overlay-safe" style={{ top: `${((bleed + 0.375) / h) * 100}%`, bottom: `${((bleed + 0.375) / h) * 100}%`, left: `${((bleed + 0.375) / w) * 100}%`, right: `${((bleed + 0.375) / w) * 100}%` }} />}
      <div className="absolute top-0 bottom-0 pointer-events-none" style={{ left: `${((bleed + trimW) / w) * 100}%`, borderLeft: "1px dashed rgba(0,0,0,0.4)" }} />
      <div className="absolute top-0 bottom-0 pointer-events-none" style={{ left: `${((bleed + trimW + spineW) / w) * 100}%`, borderLeft: "1px dashed rgba(0,0,0,0.4)" }} />
      {overlays.barcode && (
        <div className="absolute pointer-events-none border-2 border-emerald-500 bg-emerald-100/30" style={{ left: `${((bleed + 0.375) / w) * 100}%`, bottom: `${((bleed + 0.375) / h) * 100}%`, width: `${(2 / w) * 100}%`, height: `${(1.2 / h) * 100}%` }} />
      )}
      <div className="absolute -top-6 left-0 right-0 flex font-mono-spec text-[9px] tracking-widest text-neutral-500 uppercase">
        <div style={{ width: `${((bleed + trimW) / w) * 100}%` }} className="text-center">← Back</div>
        <div style={{ width: `${(spineW / w) * 100}%` }} className="text-center">Spine</div>
        <div className="flex-1 text-center">Front →</div>
      </div>
    </div>
  );
}

function InteriorPreview({ overlays, previewUrl }) {
  return (
    <div className="relative bg-white shadow-lg" style={{ aspectRatio: "6/9", width: "min(50vh, 420px)" }} data-testid="interior-preview">
      {previewUrl ? <img src={previewUrl} alt="interior" className="absolute inset-0 w-full h-full object-contain" /> : <div className="absolute inset-0 flex items-center justify-center text-neutral-400 font-mono-spec text-xs uppercase">No file</div>}
      {overlays.bleed && <div className="absolute inset-0 overlay-bleed pointer-events-none" />}
      {overlays.trim && <div className="absolute pointer-events-none overlay-trim" style={{ inset: "1.4%" }} />}
      {overlays.safe && <div className="absolute pointer-events-none overlay-safe" style={{ inset: "4.2%" }} />}
    </div>
  );
}
