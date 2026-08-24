import { useEffect, useState } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { api, fmtErr } from "@/lib/api";
import { toast } from "sonner";
import { Palette } from "lucide-react";

export default function CoverTemplateDialog({ open, onOpenChange, projectId, onApplied }) {
  const [templates, setTemplates] = useState([]);
  const [applying, setApplying] = useState(null);

  useEffect(() => {
    if (!open) return;
    api.get("/cover-templates").then(({ data }) => setTemplates(data.templates)).catch(() => {});
  }, [open]);

  const apply = async (key) => {
    setApplying(key);
    try {
      const { data } = await api.post(`/projects/${projectId}/cover-template`, { template_key: key });
      toast.success("Cover template applied — customize it further or replace with your own art any time.");
      onApplied?.(data);
      onOpenChange(false);
    } catch (e) { toast.error(fmtErr(e.response?.data?.detail)); }
    finally { setApplying(null); }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl" data-testid="cover-template-dialog">
        <DialogHeader>
          <DialogTitle className="font-display font-black text-2xl tracking-tighter flex items-center gap-2">
            <Palette className="w-5 h-5" /> Cover Design Templates
          </DialogTitle>
          <DialogDescription className="text-xs text-neutral-500 font-mono-spec tracking-widest uppercase">
            Typographic starter covers — replaces your full cover wrap. Fully editable after.
          </DialogDescription>
        </DialogHeader>
        <div className="grid grid-cols-2 md:grid-cols-3 gap-3 mt-3">
          {templates.map((t) => (
            <button
              key={t.key}
              onClick={() => apply(t.key)}
              disabled={!!applying}
              className="text-left border border-neutral-200 hover:border-black transition-colors disabled:opacity-50"
              data-testid={`cover-template-${t.key}`}
            >
              <div
                className="h-28 flex items-center justify-center relative"
                style={{ background: t.bg_color }}
              >
                <div className="w-1 h-10" style={{ background: t.accent_color }} />
                {applying === t.key && <div className="absolute inset-0 flex items-center justify-center bg-black/40 text-white text-xs">Applying…</div>}
              </div>
              <div className="p-3">
                <div className="font-display font-bold text-sm">{t.label}</div>
                <div className="text-[10px] text-neutral-500 mt-1 uppercase tracking-wide">{t.genre_tags.join(" · ")}</div>
              </div>
            </button>
          ))}
        </div>
      </DialogContent>
    </Dialog>
  );
}
