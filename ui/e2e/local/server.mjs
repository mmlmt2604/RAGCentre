// Isolated local-account server. The bootstrap password is test data passed only on stdin.
import { spawn, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const username = process.env.RAGCENTRE_E2E_LOCAL_USERNAME ?? "e2e-admin";
const password = process.env.RAGCENTRE_E2E_LOCAL_PASSWORD ?? "local-admin-password-for-e2e";
const initialized = spawnSync("uv", ["run", "ragcentre", "user", "init-admin"], {
  cwd: root,
  env: process.env,
  input: `${username}\n${password}\n${password}\n`,
  encoding: "utf8",
});
if (initialized.status !== 0) {
  process.stderr.write(`${initialized.error?.message ?? ""}\n${initialized.stderr ?? ""}`.replaceAll(password, "[test password]"));
  process.exit(initialized.status ?? 1);
}
const child = spawn("uv", ["run", "ragcentre", "serve", "--port", process.env.RAGCENTRE_E2E_LOCAL_PORT ?? "8712", "--workers", "2"], {
  cwd: root, env: process.env, stdio: "inherit",
});
for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, () => child.kill(signal));
child.on("error", (error) => { console.error(error.message); process.exit(1); });
child.on("exit", (code) => process.exit(code ?? 0));
