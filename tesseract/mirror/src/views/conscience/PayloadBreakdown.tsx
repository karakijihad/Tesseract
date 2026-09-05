import { Row } from '../../components/common/Row';
import { usePanelStore } from '../../cockpit/panelStore';
import type { View } from '../../stores/ui';
import type { PayloadReading, PayloadSection } from '../../stores/conscience';

/** What rides every turn, before you type anything.
 *
 * The reading is a sum in three parts — instructions + tools + memory — then
 * the parts themselves, grouped under their own headings rather than run
 * together in one list. Grouping is what makes it legible: the question is
 * never "how big is the prompt", it is "what is it made OF, and can I go and
 * look at the piece that costs the most".
 *
 * So a row is a link wherever a surface exists behind it. The three documents
 * open Identity → Documents, where they are edited; the tool map and the
 * schemas open Settings → Tools.
 *
 * **Nothing here is a budget.** The assembly used to shed sections past a
 * 100,000-char ceiling that was a round number typed into a refactor, never
 * derived from a model — chat_brain runs on a 400,000-token window. Every
 * section is sent now, and this panel exists so the size is optimised on
 * purpose instead of amputated on a threshold.
 */
export function PayloadBreakdown({
  readings,
  onJump,
}: {
  readings: PayloadReading[];
  /** Handles a row whose surface is a section of the panel this is already
   *  in. Returns true when it took the click; false falls through to opening
   *  the named panel. */
  onJump?: (name: string) => boolean;
}) {
  const base = readings[0];
  const others = readings.slice(1);
  if (!base) return null;

  const max = Math.max(1, ...base.sections.map((s) => s.chars));

  return (
    <figure className="payload">
      <header className="payload__head">
        <p className="payload__total">
          <strong>{base.total_tokens.toLocaleString()}</strong>
          <span className="payload__unit">tokens ride every turn</span>
        </p>
        <p className="payload__total-chars t-meta">
          {base.total_chars.toLocaleString()} characters
        </p>
      </header>

      <Formula reading={base} />

      {base.groups.map((group) => {
        const rows = base.sections.filter(
          (s) => s.group === group.name && s.chars > 0,
        );
        if (!rows.length) return null;
        return (
          <section key={group.name} className="payload__group-block">
            <h4 className="payload__group-heading">
              <span className={`payload__swatch payload__swatch--${group.name}`} />
              <span className="t-label">{group.name}</span>
              <span className="payload__group-total t-meta">
                {group.tokens.toLocaleString()}t
              </span>
            </h4>
            <ol className="payload__rows">
              {rows.map((s) => (
                <SectionRow key={s.name} section={s} max={max} onJump={onJump} />
              ))}
            </ol>
          </section>
        );
      })}

      <Residency reading={base} />

      {others.map((r) => (
        <p key={r.surface} className="payload__delta t-meta">
          Through a channel the same turn is{' '}
          {r.total_tokens.toLocaleString()} tokens,{' '}
          {(r.total_tokens - base.total_tokens).toLocaleString()} more for that
          surface&rsquo;s own contract.
        </p>
      ))}

      <figcaption className="payload__caption t-meta">
        Everything listed is sent, every turn. Nothing is dropped or trimmed to
        fit. This is the constant cost before your first message, so it is the
        part that caches; the conversation is charged on top of it. Token counts
        are an estimate at four characters each, the same one the runtime
        compacts by, because no exact count exists until the model answers.
      </figcaption>
    </figure>
  );
}

/** The sum, written as a sum.
 *
 * Three figures and a total, laid out as the arithmetic rather than as a
 * legend, because the point of the grouping is that these three add up to the
 * number above.
 */
function Formula({ reading }: { reading: PayloadReading }) {
  return (
    <ul className="payload__formula" aria-label="How the total divides">
      {reading.groups.map((g, i) => (
        <li key={g.name} className="payload__term">
          {i > 0 && <span className="payload__operator" aria-hidden="true">+</span>}
          <span className="payload__term-body">
            <span className={`payload__swatch payload__swatch--${g.name}`} />
            <span className="payload__term-value">
              {g.tokens.toLocaleString()}t
            </span>
            <span className="payload__term-name t-meta">{g.name}</span>
            <span className="payload__term-chars t-meta">
              ({g.chars.toLocaleString()})
            </span>
          </span>
        </li>
      ))}
      <li className="payload__term payload__term--sum">
        <span className="payload__operator" aria-hidden="true">=</span>
        <span className="payload__term-body">
          <span className="payload__term-value">
            {reading.total_tokens.toLocaleString()}t
          </span>
          <span className="payload__term-name t-meta">every turn</span>
          <span className="payload__term-chars t-meta">
            ({reading.total_chars.toLocaleString()})
          </span>
        </span>
      </li>
    </ul>
  );
}

function SectionRow({
  section,
  max,
  onJump,
}: {
  section: PayloadSection;
  max: number;
  onJump?: (name: string) => boolean;
}) {
  const openPanel = usePanelStore((s) => s.openPanel);
  const body = (
    <>
      <span className="payload__name">{section.label}</span>
      <span className="payload__description t-meta">{section.description}</span>
      <span className="payload__track">
        <span
          className={`payload__bar payload__bar--${section.group}`}
          style={{ width: `${(section.chars / max) * 100}%` }}
        />
      </span>
      <span className="payload__figures">
        <span className="payload__tokens">{section.tokens.toLocaleString()}t</span>
        <span className="payload__chars t-meta">
          ({section.chars.toLocaleString()})
        </span>
      </span>
    </>
  );

  // `Row` owns the cursor, the focus ring and the Enter/Space contract; a
  // section with nowhere to go is a plain list item rather than a control that
  // does nothing when you press it.
  if (!section.panel) return <li className="payload__row">{body}</li>;
  return (
    <Row
      as="li"
      className="payload__row payload__row--link"
      // A surface that is a SECTION of this same panel moves the rail. Opening
      // the window the reader is already looking at does nothing, and a row
      // that answers a click with nothing reads as broken.
      onClick={() => {
        if (onJump?.(section.name)) return;
        openPanel(section.panel as View);
      }}
      ariaLabel={`${section.label}. ${section.description} Open ${section.hint ?? section.panel}.`}
    >
      {body}
    </Row>
  );
}

/** Resident is not the same as retrievable.
 *
 * The capsule inlines a curated slice — MEMORY.md, the last two days, and the
 * freshest few topic pages. The rest of the store is reachable only when the
 * assistant calls `memory_search`. Without this line the panel implies it
 * carries everything it knows, which is the most misleading thing a context
 * readout can say.
 */
function Residency({ reading }: { reading: PayloadReading }) {
  const r = reading.residency;
  if (!r.topic_hubs) return null;
  return (
    <p className="payload__residency t-meta">
      Memory is a slice, not the store.{' '}
      {Math.min(r.topic_hubs_inlined, r.topic_hubs)} of {r.topic_hubs} topic
      pages ride the turn
      {r.daily_captures > 0 && `, with ${r.daily_captures} daily captures on disk`}
      . The rest is reachable, but only if the assistant goes looking with{' '}
      <code>memory_search</code>.
    </p>
  );
}
