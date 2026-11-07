// Panel wiring: what each panel shows under the "wiring" test picture <-> Settings.output.panel_order / panel_flip.
//
// The wiring picture is drawn straight into the card's image: band b (connector J(b+1)) shows the number b+1
// and an arrow to the top of the band. So a panel mounted upright shows its connector's arrow pointing up, one mounted
// rotated 180 degrees shows it pointing down. For the panel at position i (0 = top): number n, arrow up ->
// panel_order[i] = n - 1, panel_flip[i] = false; arrow down -> panel_flip[i] = true. That holds with rotate180 off,
// which the wizard sets. The server applies the same rule (trafficlight/output/wiring.py); keep them in step.

export const PANELS = 6;   // config.PANELS
export const PORTS = 8;    // config.PORTS: the card's connectors J1..J8

/** What one panel shows: its connector number (1..PORTS) and whether the arrow points down; null = stays dark. */
export type Seen = { connector: number; down: boolean } | null;

export const PANEL_NAMES = [
  'Top light, upper panel', 'Top light, lower panel',
  'Middle light, upper panel', 'Middle light, lower panel',
  'Bottom light, upper panel', 'Bottom light, lower panel',
];

export interface WiringProblem { message: string; panels: number[] }

const list = (names: string[]) => (names.length < 2 ? names.join('') : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`);

/** Why these observations cannot be applied (empty = they can). */
export function seenProblems(seen: Seen[]): WiringProblem[] {
  const out: WiringProblem[] = [];
  if (seen.length !== PANELS) return [{ message: `Need what ${PANELS} panels show, got ${seen.length}.`, panels: [] }];
  seen.forEach((s, i) => {
    if (s && !(Number.isInteger(s.connector) && s.connector >= 1 && s.connector <= PORTS)) {
      out.push({ message: `${PANEL_NAMES[i]}: there is no connector ${s.connector} (J1 to J${PORTS}).`, panels: [i] });
    }
  });
  for (let n = 1; n <= PORTS; n++) {
    const on = seen.flatMap((s, i) => (s?.connector === n ? [i] : []));
    if (on.length > 1) {
      out.push({
        message: `${list(on.map((i) => PANEL_NAMES[i]))} ${on.length === 2 ? 'both' : 'all'} show ${n}, but each connector feeds one panel. Look again: upside down, a 6 is a "9" with the bar on top.`,
        panels: on,
      });
    }
  }
  return out;
}

/** panel_order and panel_flip for rotate180 off. A dark panel gets the lowest connector nobody else uses, not
 * flipped, so the others can be saved. Throws when seenProblems() is not empty. */
export function fromSeen(seen: Seen[]): { panel_order: number[]; panel_flip: boolean[] } {
  const problems = seenProblems(seen);
  if (problems.length) throw new Error(problems.map((p) => p.message).join(' '));
  const named = new Set(seen.flatMap((s) => (s ? [s.connector - 1] : [])));
  const free = Array.from({ length: PORTS }, (_, b) => b).filter((b) => !named.has(b));
  return {
    panel_order: seen.map((s) => (s ? s.connector - 1 : free.shift()!)),
    panel_flip: seen.map((s) => (s ? s.down : false)),
  };
}

/** What each panel shows if these settings match the sign (pre-fills the wizard). A sign with rotate180 on is the
 * reversed order with every panel flipped. */
export function toSeen(order: number[], flip: boolean[], rotate180: boolean): Seen[] {
  const o = rotate180 ? order.slice().reverse() : order;
  const f = rotate180 ? flip.slice().reverse().map((x) => !x) : flip;
  return o.map((band, i) => ({ connector: band + 1, down: Boolean(f[i]) }));
}

export const sameSeen = (a: Seen[], b: Seen[]) =>
  a.length === b.length && a.every((s, i) => (s === null ? b[i] === null : b[i] !== null && s.connector === b[i]!.connector && s.down === b[i]!.down));
