/** Stable palette slot (0–7) for a fingerprint, so identical fingerprints always look identical. */
export function fpSlot(fingerprint: string): number {
  let h = 0;
  for (let i = 0; i < Math.min(fingerprint.length, 16); i++) h = (h * 31 + fingerprint.charCodeAt(i)) >>> 0;
  return h % 8;
}
