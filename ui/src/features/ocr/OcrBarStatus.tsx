import { Link } from "react-router";
import { useOcr } from "../../api/hooks";
import { OcrIndicator } from "./OcrIndicator";
import { ocrSummary } from "./model";

/** OCR readiness on every screen, one click from the details. */
export function OcrBarStatus() {
  const { data, isError } = useOcr();
  if (!data || isError) return null;
  const { state, label } = ocrSummary(data);
  return (
    <Link to="/providers" className="shell-status__ocr">
      <OcrIndicator state={state} label={label} />
    </Link>
  );
}
