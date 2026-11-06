// Times typed by a person: "90", "1:30", "1:02:03", "2.5".

/** Seconds, null for an empty field, NaN for something that is not a time. */
export function parseClock(text: string): number | null {
  const s = text.trim();
  if (!s) return null;
  const parts = s.split(':');
  if (parts.length > 3 || parts.some((p) => !/^\d+(\.\d+)?$/.test(p.trim()))) return NaN;
  // Only the last part may be a fraction or run past 59: "1:90" and "1.5:00" are mistakes.
  const n = parts.map((p) => Number(p));
  if (n.slice(0, -1).some((v) => !Number.isInteger(v)) || (n.length > 1 && n[n.length - 1] >= 60) || (n.length > 2 && n[1] >= 60)) return NaN;
  return n.reduce((acc, v) => acc * 60 + v, 0);
}
