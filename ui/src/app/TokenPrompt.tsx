import { useQueryClient } from "@tanstack/react-query";
import { KeyRound } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { onUnauthorized, setToken } from "../api/client";
import { Button } from "../components/Button";
import { Dialog } from "../components/Dialog";
import { Field, TextInput } from "../components/Field";

/** Asks for the server's access token whenever a request returns 401. */
export function TokenPrompt() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");
  const [rejected, setRejected] = useState(false);
  const justSubmitted = useRef(false);

  useEffect(
    () =>
      onUnauthorized(() => {
        setRejected(justSubmitted.current);
        justSubmitted.current = false;
        setOpen(true);
      }),
    [],
  );

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!value.trim()) return;
    setToken(value.trim());
    justSubmitted.current = true;
    setOpen(false);
    setRejected(false);
    void qc.resetQueries();
  };

  return (
    <Dialog open={open} onClose={() => setOpen(false)} title="Access token required">
      <form onSubmit={submit} className="token-form">
        <p>
          This RAGCentre server requires an access token (<code>RAGCENTRE_API_TOKEN</code>). It is kept for this browser tab only.
        </p>
        <Field label="Token" error={rejected ? "That token was not accepted. Check it and try again." : null}>
          {(a) => <TextInput {...a} type="password" mono autoFocus autoComplete="off" value={value} onChange={(e) => setValue(e.target.value)} />}
        </Field>
        <div className="token-form__actions">
          <Button variant="primary" type="submit" icon={KeyRound} disabled={!value.trim()}>
            Use token
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
