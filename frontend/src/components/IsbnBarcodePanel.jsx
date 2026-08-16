import { useEffect, useState, useCallback } from "react";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, fmtErr, API_URL } from "@/lib/api";
import { toast } from "sonner";
import { Barcode, Check, X } from "lucide-react";

/**
 * ISBN + EAN-13 barcode preview panel.
 * - Validates ISBN-10/13 in real-time via /api/isbn/validate
 * - Renders live PNG barcode from /api/isbn/barcode.png
 * - Saves to project via PATCH /api/projects/{id}/isbn
 */
export default function IsbnBarcodePanel({ projectId, initialIsbn = "" }) {
  const [isbn, setIsbn] = useState(initialIsbn);
  const [validated, setValidated] = useState(null);
  const [saving, setSaving] = useState(false);

  const validate = useCallback(async (val) => {
    if (!val || val.replace(/\D/g, "").length < 10) { setValidated(null); return; }
    try {
      const { data } = await api.post("/isbn/validate", { isbn: val });
      setValidated(data);
    } catch { setValidated(null); }
  }, []);

  useEffect(() => {
    const t = setTimeout(() => validate(isbn), 400);
    return () => clearTimeout(t);
  }, [isbn, validate]);

  const save = async () => {
    if (!validated?.valid) return;
    setSaving(true);
    try {
      await api.patch(`/projects/${projectId}/isbn`, { isbn: validated.isbn });
      toast.success("ISBN saved · barcode ready for cover");
    } catch (e) { toast.error(fmtErr(e.response?.data?.detail)); }
    finally { setSaving(false); }
  };

  const barcodeSrc = validated?.valid ? `${API_URL}/isbn/barcode.png?isbn=${validated.isbn}` : null;

  return (
    <div className="bg-white border border-neutral-200 p-4" data-testid="isbn-panel">
      <div className="flex items-center gap-2">
        <Barcode className="w-4 h-4 text-neutral-500" />
        <span className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500">ISBN + Barcode</span>
      </div>
      <Label className="font-mono-spec text-[10px] tracking-widest uppercase text-neutral-500 mt-3 block">Enter ISBN-10 or ISBN-13</Label>
      <div className="flex gap-2 mt-1">
        <Input
          value={isbn}
          onChange={(e) => setIsbn(e.target.value)}
          placeholder="978-3-16-148410-0"
          className="font-mono text-sm"
          data-testid="isbn-input"
        />
        <button
          onClick={save}
          disabled={!validated?.valid || saving}
          className="btn-gold px-4 font-mono-spec text-[10px] tracking-widest uppercase disabled:opacity-40 btn-industrial"
          data-testid="isbn-save"
        >
          {saving ? "…" : "Save"}
        </button>
      </div>
      {validated && (
        <div className={`mt-2 flex items-start gap-1.5 text-xs font-mono-spec ${validated.valid ? "text-emerald-700" : "text-red-700"}`} data-testid="isbn-status">
          {validated.valid ? <Check className="w-3.5 h-3.5" /> : <X className="w-3.5 h-3.5" />}
          {validated.valid ? `Valid · ISBN-13 ${validated.isbn}` : `Invalid · ${validated.error}`}
        </div>
      )}
      {barcodeSrc && (
        <div className="mt-4 bg-white border border-neutral-200 p-3 flex justify-center" data-testid="isbn-barcode-preview">
          <img src={barcodeSrc} alt="EAN-13 barcode" className="max-h-24" />
        </div>
      )}
      <p className="mt-3 text-[11px] text-neutral-500 leading-relaxed">
        Barcode auto-renders into the distributor barcode zone on export. ISBN-10 is converted to ISBN-13 automatically.
      </p>
    </div>
  );
}
