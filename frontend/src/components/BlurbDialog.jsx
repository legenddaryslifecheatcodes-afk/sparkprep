import { useState } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { api, fmtErr } from "@/lib/api";
import { toast } from "sonner";
import { Sparkles, Copy, RefreshCw } from "lucide-react";

export default function BlurbDialog({ open, onOpenChange, defaultTitle = "" }) {
  const [form, setForm] = useState({ title: defaultTitle, genre: "", page_count: "", themes: "", audience: "" });
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);

  const generate = async () => {
    if (!form.title.trim()) { toast.error("Add a title first"); return; }
    setBusy(true);
    setResult(null);
    try {
      const { data } = await api.post("/ai/blurb", {
        title: form.title,
        genre: form.genre || null,
        page_count: form.page_count ? parseInt(form.page_count) : null,
        themes: form.themes || null,
        audience: form.audience || null,
      });
      setResult(data);
    } catch (e) { toast.error(fmtErr(e.response?.data?.detail) || "AI failed"); }
    finally { setBusy(false); }
  };

  const copy = (text) => {
    navigator.clipboard.writeText(text);
    toast.success("Copied to clipboard");
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl max-h-[90vh] overflow-y-auto" data-testid="blurb-dialog">
        <DialogHeader>
          <DialogTitle className="font-display font-black text-2xl tracking-tighter flex items-center gap-2">
            <Sparkles className="w-5 h-5" /> AI Blurb Writer
          </DialogTitle>
          <DialogDescription className="text-xs text-neutral-500 font-mono-spec tracking-widest uppercase">Claude Sonnet · 3 tones per generation</DialogDescription>
        </DialogHeader>

        <div className="grid grid-cols-2 gap-3 mt-3">
          <div className="col-span-2">
            <Label className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Book Title *</Label>
            <Input value={form.title} onChange={e=>setForm({...form, title: e.target.value})} className="mt-1" data-testid="blurb-title" placeholder="The Long Print" />
          </div>
          <div>
            <Label className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Genre</Label>
            <Input value={form.genre} onChange={e=>setForm({...form, genre: e.target.value})} className="mt-1" data-testid="blurb-genre" placeholder="Literary Thriller" />
          </div>
          <div>
            <Label className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Page Count</Label>
            <Input type="number" value={form.page_count} onChange={e=>{
              const raw = e.target.value;
              if (raw === "") { setForm({...form, page_count: ""}); return; }
              const value = parseInt(raw, 10);
              setForm({...form, page_count: isNaN(value) ? "" : value.toString()});
            }} className="mt-1" data-testid="blurb-pages" placeholder="320" />
          </div>
          <div className="col-span-2">
            <Label className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Themes / Hooks</Label>
            <Textarea rows={2} value={form.themes} onChange={e=>setForm({...form, themes: e.target.value})} className="mt-1" data-testid="blurb-themes" placeholder="obsession, small-town secrets, a photographer's archive" />
          </div>
          <div className="col-span-2">
            <Label className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Target Audience (optional)</Label>
            <Input value={form.audience} onChange={e=>setForm({...form, audience: e.target.value})} className="mt-1" data-testid="blurb-audience" placeholder="Fans of Tana French & Gillian Flynn" />
          </div>
        </div>

        <button onClick={generate} disabled={busy} className="mt-4 w-full bg-black text-white py-3 font-mono-spec text-xs tracking-widest uppercase hover:bg-neutral-800 disabled:opacity-50 flex items-center justify-center gap-2 btn-industrial" data-testid="blurb-generate">
          {busy ? <><RefreshCw className="w-4 h-4 animate-spin" /> Drafting…</> : <><Sparkles className="w-4 h-4" /> Draft Blurbs</>}
        </button>

        {result && (
          <div className="mt-6 space-y-4" data-testid="blurb-result">
            {result.tagline && (
              <div className="bg-neutral-100 border border-neutral-300 p-4">
                <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Tagline</div>
                <div className="flex items-start justify-between gap-2 mt-1">
                  <p className="font-display font-black text-xl tracking-tight" data-testid="blurb-tagline">"{result.tagline}"</p>
                  <button onClick={() => copy(result.tagline)} className="text-neutral-500 hover:text-black shrink-0" data-testid="copy-tagline"><Copy className="w-4 h-4" /></button>
                </div>
              </div>
            )}
            {result.variations?.map((v, i) => (
              <div key={i} className="bg-white border border-neutral-300 p-4" data-testid={`blurb-variation-${i}`}>
                <div className="flex items-center justify-between">
                  <div className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">
                    Variation {i+1} · {v.tone}
                  </div>
                  <button onClick={() => copy(v.copy)} className="text-neutral-500 hover:text-black" data-testid={`copy-variation-${i}`}><Copy className="w-4 h-4" /></button>
                </div>
                <p className="mt-3 text-sm leading-relaxed text-neutral-800 whitespace-pre-line">{v.copy}</p>
              </div>
            ))}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
