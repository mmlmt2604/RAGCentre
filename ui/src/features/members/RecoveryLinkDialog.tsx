import { useState, type FormEvent } from "react";
import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { Field, TextInput } from "../../components/Field";
import { ErrorState } from "../../components/States";
import { Time } from "../../components/Time";
import type { User } from "../auth/types";
import { useManagementAction } from "./data";
import { OneTimeValue } from "./OneTimeValue";

interface RecoveryResult { token: string; expires_at: string; link: string; path: string }
export function RecoveryLinkDialog({ user, onClose }: { user: User; onClose: () => void }) {
  const [password, setPassword] = useState("");
  const [issued, setIssued] = useState<RecoveryResult>();
  const issue = useManagementAction<string, RecoveryResult>((current_password, { request }) => request(`/users/${user.id}/recovery-token`, { method: "POST", body: { current_password } }));
  const submit = (event: FormEvent) => {
    event.preventDefault();
    issue.mutate(password, { onSuccess: (result) => setIssued(result), onSettled: () => setPassword("") });
  };
  return <Dialog open onClose={onClose} title={`Recover ${user.username}`} width={520}>
    {issued ? <><OneTimeValue title={`Recovery link for ${user.username}`} value={issued.link} onClose={onClose} /><p>Expires <Time iso={issued.expires_at} />. Share it only with the account holder. They will sign in normally after resetting their password.</p></> : <>
      <p>Create a one-time password recovery link for <strong>{user.username}</strong>. Creating it replaces previous recovery links. Their current sessions stay active until they set a new password.</p>
      <form className="auth-fields" onSubmit={submit}>
        <Field label="Your administrator password" hint="Confirm your authority to issue this recovery link.">{(a) => <TextInput {...a} type="password" autoComplete="current-password" required maxLength={128} value={password} onChange={(e) => setPassword(e.target.value)} autoFocus />}</Field>
        {issue.error ? <ErrorState title="Could not issue recovery link" error={issue.error} /> : null}
        <div className="form-actions"><Button type="submit" variant="primary" loading={issue.isPending}>Create recovery link</Button><Button onClick={onClose}>Cancel</Button></div>
      </form>
    </>}
  </Dialog>;
}
