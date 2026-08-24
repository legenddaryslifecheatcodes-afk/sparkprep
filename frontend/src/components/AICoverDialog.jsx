import { useState } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { api, fmtErr } from "@/lib/api";
import { toast } from "sonner";
import { Sparkles, ImageIcon } from "lucide-react";

export default function AICoverDialog({ open, onOpenChange, projectId, onGenerated }) {
  const [prompt, setPrompt] = useState("");
  const [genre, setGenre] = useState("");
  const [slot, setSlot] = useState("front_cover");
  const [busy, setBusy] = useState(false);

  const generate = async () => {
    if (!prompt.trim()) { toast.error("Describe the cover art you want first"); return; }
    setBusy(true);
    try {
      const { data } = await api.post(`/projects/${projectId}/ai-cover`, { prompt, genre: genre || null, slot });
      toast.success("AI cover generated — run Auto-Fix to prep it for print.");
      onGenerated?.(data);
      onOpenChange(false);
    } catch (e) { toast.error(fmtErr(e.response?.data?.detail)); }
    finally { setBusy(false); }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl" data-testid="ai-cover-dialog">
        <DialogHeader>
          <DialogTitle className="font-display font-black text-2xl tracking-tighter flex items-center gap-2">
            <ImageIcon className="w-5 h-5" /> AI Cover Generation
          </DialogTitle>
          <DialogDescription className="text-xs text-neutral-500 font-mono-spec tracking-widest uppercase">
            Generates artwork only — no title text baked in, so it composites cleanly with your title/spine.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3 mt-2">
          <div>
            <Label className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Describe the cover art</Label>
            <Textarea rows={3} value={prompt} onChange={(e) => setPrompt(e.target.value)} className="mt-1" placeholder="A lone lighthouse on a storm-lit cliff, moody teal and orange palette, oil painting style" data-testid="ai-cover-prompt" />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Genre (optional)</Label>
              <Input value={genre} onChange={(e) => setGenre(e.target.value)} className="mt-1" placeholder="Literary Thriller" data-testid="ai-cover-genre" />
            </div>
            <div>
              <Label className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Apply to</Label>
              <Select value={slot} onValueChange={setSlot}>
                <SelectTrigger className="mt-1" data-testid="ai-cover-slot"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="front_cover">Front cover only</SelectItem>
                  <SelectItem value="full_wrap">Full cover wrap</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
        </div>
        <button
          onClick={generate}
          disabled={busy}
          className="mt-4 w-full bg-black text-white py-3 font-mono-spec text-xs tracking-widest uppercase hover:bg-neutral-800 disabled:opacity-50 flex items-center justify-center gap-2 btn-industrial"
          data-testid="ai-cover-generate"
        >
          <Sparkles className="w-4 h-4" /> {busy ? "Generating…" : "Generate Cover Art"}
        </button>
      </DialogContent>
    </Dialog>
  );
}
