/** Canonical JSON (sorted keys), matching the backend's fingerprint input format. */
export function canonical(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  const entries = Object.entries(value as Record<string, unknown>)
    .filter(([, v]) => v !== undefined)
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${canonical(v)}`).join(",")}}`;
}

/** 32-bit FNV-1a with a seed. Not cryptographic; the mock only needs stable, well-spread ids. */
function fnv(input: string, seed: number): number {
  let h = 0x811c9dc5 ^ seed;
  for (let i = 0; i < input.length; i++) {
    h ^= input.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

/** A 64-hex-character pseudo-sha256 of the canonical form of `value`. */
export function fp(value: unknown): string {
  const text = typeof value === "string" ? value : canonical(value);
  let out = "";
  for (let seed = 0; seed < 8; seed++) out += fnv(text, seed * 0x9e3779b9).toString(16).padStart(8, "0");
  return out;
}

/** Deterministic UUID-shaped id derived from a string. */
export function uuidFrom(text: string): string {
  const h = fp(`uuid:${text}`);
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-4${h.slice(13, 16)}-a${h.slice(17, 20)}-${h.slice(20, 32)}`;
}

/** Small seeded PRNG so fixtures are identical on every load. */
export function rng(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
