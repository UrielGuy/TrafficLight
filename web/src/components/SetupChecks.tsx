import { Fragment } from 'react';
import { Link } from 'react-router-dom';
import type { CheckLevel, SetupCheck } from '../api/types';

const MARK: Record<CheckLevel, string> = { error: '!', warning: '!', info: 'i', ok: '✓' };
const LEVEL_NAME: Record<CheckLevel, string> = { error: 'Problem', warning: 'Warning', info: 'Note', ok: 'Fine' };

export const needsAttention = (c: SetupCheck) => c.level === 'error' || c.level === 'warning';

/** Fix texts quote commands in `backticks`: show those as code. */
function Text({ text }: { text: string }) {
  return <>{text.split('`').map((part, i) => (i % 2 ? <code key={i}>{part}</code> : <Fragment key={i}>{part}</Fragment>))}</>;
}

function CheckRow({ c }: { c: SetupCheck }) {
  return (
    <li className={`check ${c.level}`} data-check={c.id}>
      <span className="check-mark" role="img" aria-label={LEVEL_NAME[c.level]}>{MARK[c.level]}</span>
      <div className="check-body">
        <div className="check-title">{c.title}</div>
        <div className="check-detail"><Text text={c.detail} /></div>
        {c.fix && c.level !== 'ok' && <div className="check-fix"><span className="check-fix-label">{c.level === 'info' ? 'Hint: ' : 'What to do: '}</span><Text text={c.fix} /></div>}
      </div>
    </li>
  );
}

/** Every check, fine ones included (Settings). */
export function SetupList({ checks }: { checks: SetupCheck[] }) {
  const open = checks.filter(needsAttention).length;
  return (
    <>
      <p className={`small ${open ? '' : 'muted'}`}>{open ? `${open} thing${open > 1 ? 's' : ''} to fix before the sign works as it should.` : 'Nothing to fix.'}</p>
      <ul className="checks">{checks.map((c) => <CheckRow key={c.id} c={c} />)}</ul>
    </>
  );
}

/** Only what needs attention, with a way to the full list (Dashboard). Nothing when all is well. */
export function AttentionList({ checks }: { checks: SetupCheck[] }) {
  const open = checks.filter(needsAttention);
  if (!open.length) return null;
  return (
    <section className="card attention" role="alert">
      <div className="attention-head">
        <h2>Setup needs attention</h2>
        <Link to="/settings">All setup checks in Settings</Link>
      </div>
      <ul className="checks">{open.map((c) => <CheckRow key={c.id} c={c} />)}</ul>
    </section>
  );
}
