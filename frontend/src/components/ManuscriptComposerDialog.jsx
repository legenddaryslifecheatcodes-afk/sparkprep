import { useEffect, useState } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { api, fmtErr, API_URL } from "@/lib/api";
import { toast } from "sonner";
import { BookOpen, Download, Eye, Sparkles } from "lucide-react";

const SAMPLE_SOURCE = `# Chapter One: The Long Print

Emma had never trusted the machine. It hummed at midnight, printed itself dry by dawn, and left the smell of toner
in the folds of every doorway. She stood in front of it now, her coat still buttoned, watching a page slide out
as if the room had exhaled.

Her father used to say the shop had two hearts. One belonged to the press.

***

Three streets over, in an office that still smelled of fresh paint, a different machine was about to fail her.

# Chapter Two: The Rejection

The letter came on a Tuesday. It was two lines long.

"Your file did not meet our print specifications. Resubmission required."`;

export default function ManuscriptComposerDialog({ open, onOpenChange, defaultProject }) {
  const [templates, setTemplates] = useState([]);
  const [form, setForm] = useState({
    template: "fiction_novel",
    title: defaultProject?.name || "Untitled Manuscript",
    author: "",
    source_text: SAMPLE_SOURCE,
    trim_size: defaultProject?.trim_size || "6x9",
    platform: defaultProject?.platform || "kdp",
  });
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    api.get("/manuscript/templates").then(({ data }) => setTemplates(data.templates)).catch(() => {});
  }, [open]);

  useEffect(() => {
    if (defaultProject) {
      setForm((f) => ({
        ...f,
        title: defaultProject.name || f.title,
        trim_size: defaultProject.trim_size || f.trim_size,
        platform: defaultProject.platform || f.platform,
      }));
    }
  }, [defaultProject]);

  const compose = async () => {
    setBusy(true);
    setResult(null);
    try {
      const { data } = await api.post("/manuscript/compose", form);
      setResult(data);
      toast.success(`Manuscript composed · ${data.page_count} pages · ${data.template_label}`);
    } catch (e) { toast.error(fmtErr(e.response?.data?.detail)); }
    finally { setBusy(false); }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-4xl max-h-[92vh] overflow-y-auto" data-testid="composer-dialog">
        <DialogHeader>
          <DialogTitle className="font-display font-black text-3xl tracking-tighter flex items-center gap-2">
            <BookOpen className="w-6 h-6" /> Compose Interior
          </DialogTitle>
          <DialogDescription className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">
            Pick a template, paste your manuscript, get a print-ready PDF/X-1a interior.
          </DialogDescription>
        </DialogHeader>

        <div className="grid md:grid-cols-3 gap-3 mt-3">
          {templates.map((t) => (
            <button
              key={t.key}
              onClick={() => setForm({ ...form, template: t.key })}
              className={`text-left p-4 border ${form.template === t.key ? "border-[#D4AF37] bg-[#D4AF37]/5" : "border-neutral-200 hover:border-black"} transition-colors`}
              data-testid={`composer-template-${t.key}`}
            >
              <div className="font-display font-bold text-lg tracking-tight">{t.label}</div>
              <div className="text-xs text-neutral-600 mt-2 leading-relaxed">{t.description}</div>
            </button>
          ))}
        </div>

        <div className="grid grid-cols-2 gap-3 mt-5">
          <div>
            <Label className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Title</Label>
            <Input value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} className="mt-1" data-testid="composer-title" />
          </div>
          <div>
            <Label className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Author</Label>
            <Input value={form.author} onChange={(e) => setForm({ ...form, author: e.target.value })} className="mt-1" data-testid="composer-author" />
          </div>
        </div>

        <div className="mt-3">
          <div className="flex items-center justify-between mb-1">
            <Label className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">
              Manuscript source · Use <span className="font-bold text-black">"# Chapter Title"</span> for chapter breaks, <span className="font-bold text-black">"***"</span> on its own line for a scene/section break
            </Label>
            <button onClick={() => setForm({ ...form, source_text: SAMPLE_SOURCE })} className="font-mono-spec text-[10px] tracking-widest uppercase text-[#D4AF37] hover:underline" data-testid="composer-sample">Load sample</button>
          </div>
          <Textarea rows={12} value={form.source_text} onChange={(e) => setForm({ ...form, source_text: e.target.value })} className="font-mono text-xs" data-testid="composer-text" />
        </div>

        <button
          onClick={compose}
          disabled={busy}
          className="mt-4 w-full btn-gold py-3.5 font-mono-spec text-xs tracking-widest uppercase flex items-center justify-center gap-2 btn-industrial disabled:opacity-50"
          data-testid="composer-compose"
        >
          <Sparkles className="w-4 h-4" /> {busy ? "Composing…" : "Compose Interior PDF"}
        </button>

        {result && (
          <div className="mt-6 border border-[#D4AF37] bg-[#D4AF37]/5 p-4" data-testid="composer-result">
            <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-600">Composed</div>
            <div className="font-display font-black text-2xl tracking-tighter mt-1">{result.page_count} pages · {result.template_label}</div>
            <div className="text-xs text-neutral-600 mt-1">Trim {result.trim?.[0]}"×{result.trim?.[1]}" · {result.platform.toUpperCase()} margins</div>
            <div className="mt-4 flex gap-2">
              <a
                href={`${API_URL}${result.preview_url.replace("/api", "")}?token=${localStorage.getItem("sp_token") || ""}`}
                target="_blank" rel="noreferrer"
                className="flex items-center gap-1.5 border border-black px-4 py-2 font-mono-spec text-[10px] tracking-widest uppercase hover:bg-black hover:text-white transition-colors btn-industrial"
                data-testid="composer-preview-link"
              >
                <Eye className="w-3 h-3" /> Preview PDF
              </a>
              <a
                href={`${API_URL}${result.preview_url.replace("/api", "")}?token=${localStorage.getItem("sp_token") || ""}`}
                download={`${form.title.replace(/[^a-z0-9]+/gi, "_")}_interior.pdf`}
                className="flex items-center gap-1.5 btn-gold px-4 py-2 font-mono-spec text-[10px] tracking-widest uppercase btn-industrial"
                data-testid="composer-download"
              >
                <Download className="w-3 h-3" /> Download
              </a>
            </div>
            <p className="mt-3 text-xs text-neutral-600 leading-relaxed">
              Upload this file to your project's <span className="font-mono text-black">Interior PDF</span> slot and export as PDF/X-1a for full distributor compliance.
            </p>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
