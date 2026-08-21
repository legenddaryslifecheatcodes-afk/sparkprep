import { useEffect, useState } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { api, fmtErr } from "@/lib/api";
import { toast } from "sonner";

export default function NewProjectDialog({ open, onOpenChange, onCreated }) {
  const [specs, setSpecs] = useState(null);
  const [form, setForm] = useState({
    name: "Untitled Book", platform: "kdp", trim_size: "6x9",
    paper_type: "white_50lb", binding: "paperback", page_count: 200, project_type: "cover",
  });
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    api.get("/specs").then(({ data }) => setSpecs(data)).catch(() => {});
  }, [open]);

  const submit = async () => {
    setBusy(true);
    try {
      const { data } = await api.post("/projects", form);
      toast.success("Project created");
      onCreated?.(data);
    } catch (e) { toast.error(fmtErr(e.response?.data?.detail)); }
    finally { setBusy(false); }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg" data-testid="new-project-dialog">
        <DialogHeader>
          <DialogTitle className="font-display font-black text-2xl tracking-tighter">New book project</DialogTitle>
        </DialogHeader>
        <div className="space-y-4 mt-2">
          <div>
            <Label className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Title</Label>
            <Input value={form.name} onChange={e=>setForm({...form, name: e.target.value})} className="mt-1" data-testid="np-name" />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Platform</Label>
              <Select value={form.platform} onValueChange={v => setForm({...form, platform: v})}>
                <SelectTrigger className="mt-1" data-testid="np-platform"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {specs && Object.entries(specs.platforms).map(([k, v]) => (
                    <SelectItem key={k} value={k}>{v.name}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Trim size</Label>
              <Select value={form.trim_size} onValueChange={v => setForm({...form, trim_size: v})}>
                <SelectTrigger className="mt-1" data-testid="np-trim"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {specs && Object.entries(specs.trim_sizes).map(([k, v]) => (
                    <SelectItem key={k} value={k}>{v.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Paper</Label>
              <Select value={form.paper_type} onValueChange={v => setForm({...form, paper_type: v})}>
                <SelectTrigger className="mt-1" data-testid="np-paper"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {specs && Object.entries(specs.paper_types).map(([k, v]) => (
                    <SelectItem key={k} value={k}>{v.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Binding</Label>
              <Select value={form.binding} onValueChange={v => setForm({...form, binding: v})}>
                <SelectTrigger className="mt-1" data-testid="np-binding"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {specs && Object.entries(specs.binding_types).map(([k, v]) => (
                    <SelectItem key={k} value={k}>{v.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Page count</Label>
              <Input type="number" min={24} value={form.page_count} onChange={e=>setForm({...form, page_count: parseInt(e.target.value)||0})} className="mt-1" data-testid="np-pages" />
            </div>
            <div>
              <Label className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">Type</Label>
              <Select value={form.project_type} onValueChange={v => setForm({...form, project_type: v})}>
                <SelectTrigger className="mt-1" data-testid="np-type"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="cover">Cover only</SelectItem>
                  <SelectItem value="interior">Interior only</SelectItem>
                  <SelectItem value="combined">Cover + Interior</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <button onClick={submit} disabled={busy} className="w-full bg-black text-white py-3 font-mono-spec text-xs tracking-widest uppercase hover:bg-neutral-800 disabled:opacity-50 btn-industrial" data-testid="np-submit">
            {busy ? "Creating…" : "Create Project"}
          </button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
