import { Select } from "../../components/common/Select";
import {
  APP_FONTS,
  DEFAULT_ACCENT_HUE,
  DEFAULT_FONT,
  DEFAULT_TYPE_SCALE,
  MAX_TYPE_SCALE,
  MIN_TYPE_SCALE,
  useAppearanceStore,
} from "../../stores/appearance";
import { Button } from "../../components/common/Button";
import { ColorWell } from "../../components/common/ColorWell";
import { Range } from "../../components/common/Range";
import { Segmented } from "../../components/common/Segmented";
import { Hint } from "../../components/ui/Hint";
import {
  HAZE_STATES,
  hazeSource,
  resolveHaze,
  type HazeSource,
} from "../../lib/entity/haze";

/** The seven type tiers, in the order they rank. Rendered as live specimens
 *  rather than described: the control changes them, so the control should show
 *  them changing. */
const TIERS: { cls: string; name: string; role: string }[] = [
  { cls: "t-display", name: "Display", role: "One number that answers a view" },
  { cls: "t-head", name: "Head", role: "The name of a surface" },
  { cls: "t-sub", name: "Sub", role: "A section inside one" },
  { cls: "t-ui", name: "UI", role: "Controls and their labels" },
  { cls: "t-body", name: "Body", role: "Prose — replies, summaries, notes" },
  { cls: "t-caption", name: "Caption", role: "Rows, values, dense tables" },
  { cls: "t-meta", name: "Meta", role: "Hints, captions, timestamps" },
];

const SCALES: { label: string; value: number }[] = [
  { label: "Tiny", value: 0.7 },
  { label: "Compact", value: DEFAULT_TYPE_SCALE },
  { label: "Regular", value: 1 },
  { label: "Large", value: 1.15 },
];

/** Named hues, so the common choices are one click and the slider is for
 *  someone who wants a specific one. Saturation and lightness are fixed —
 *  they are what keeps the palette legible against the void background, and
 *  are not the operator's to break. */
const HUES: { label: string; hue: number }[] = [
  { label: "Violet", hue: DEFAULT_ACCENT_HUE },
  { label: "Indigo", hue: 220 },
  { label: "Cyan", hue: 190 },
  { label: "Green", hue: 150 },
  { label: "Amber", hue: 40 },
  { label: "Rose", hue: 350 },
];

/** What the runtime is saying when it wears each state, so the operator is
 *  colouring a moment rather than a variable name. */
const STATE_ROLE: Record<string, string> = {
  idle: "at rest, waiting",
  listening: "your voice is coming in",
  thinking: "working on the turn",
  deep_focus: "a long piece of work",
  speaking: "answering",
  spawning: "starting an agent",
  council: "several agents at once",
  happy: "something went well",
  dreaming: "consolidating memory, unattended",
  error: "something broke",
};

const SOURCE_NOTE: Record<HazeSource, string> = {
  custom: "yours",
  derived: "follows the mirror colour",
  default: "shipped default",
};

export function AppearanceSection() {
  const typeScale = useAppearanceStore((s) => s.typeScale);
  const accentHue = useAppearanceStore((s) => s.accentHue);
  const font = useAppearanceStore((s) => s.font);
  const setTypeScale = useAppearanceStore((s) => s.setTypeScale);
  const setAccentHue = useAppearanceStore((s) => s.setAccentHue);
  const setFont = useAppearanceStore((s) => s.setFont);
  const hazeOverrides = useAppearanceStore((s) => s.hazeOverrides);
  const setHaze = useAppearanceStore((s) => s.setHaze);
  const clearHaze = useAppearanceStore((s) => s.clearHaze);
  const clearHazes = useAppearanceStore((s) => s.clearHazes);
  const reset = useAppearanceStore((s) => s.reset);

  const selectedFont =
    APP_FONTS.find((f) => f.id === font) ?? APP_FONTS[0];

  const accentShifted = Math.round(accentHue) !== DEFAULT_ACCENT_HUE;
  const customCount = Object.keys(hazeOverrides).length;

  const isDefault =
    typeScale === DEFAULT_TYPE_SCALE &&
    accentHue === DEFAULT_ACCENT_HUE &&
    font === DEFAULT_FONT &&
    customCount === 0;

  return (
    <section className="settings-section">
      <div className="appearance-block">
        <div className="appearance-block__head">
          <span className="t-ui">Font</span>
          <span className="t-meta">one face, everywhere</span>
        </div>
        <div className="appearance-font-row">
          <Select
            value={font}
            options={APP_FONTS.map((f) => ({ value: f.id, label: f.label }))}
            onChange={setFont}
            ariaLabel="App font"
            testId="appearance-font-select"
          />
          {/* The specimen is the point of the control — it shows the face the
              dropdown names, in that face, at the size the app renders it. */}
          <span className="appearance-font__specimen t-body" data-font={font}>
            The assistant is listening.
          </span>
        </div>
        <span className="appearance-font__note t-meta">{selectedFont.note}</span>
      </div>

      <div className="appearance-block">
        <div className="appearance-block__head">
          <span className="t-ui">Text size</span>
          <span className="t-meta">{Math.round(typeScale * 100)}%</span>
        </div>
        <Segmented
          items={SCALES.map((s) => ({ key: s.label, label: s.label }))}
          value={SCALES.find((s) => s.value === typeScale)?.label ?? ""}
          onSelect={(key) => {
            const step = SCALES.find((s) => s.label === key);
            if (step) setTypeScale(step.value);
          }}
          label="Text size"
        />
        <Range
          min={MIN_TYPE_SCALE}
          max={MAX_TYPE_SCALE}
          step={0.01}
          value={typeScale}
          ariaLabel="Text size"
          onChange={setTypeScale}
        />
        <ol className="appearance-tiers">
          {TIERS.map((t) => (
            <li key={t.cls} className="appearance-tier">
              <span className={t.cls}>{t.name}</span>
              <span className="appearance-tier__role t-meta">{t.role}</span>
            </li>
          ))}
        </ol>
      </div>

      <div className="appearance-block">
        <div className="appearance-block__head">
          <span className="t-ui">Mirror colour</span>
          <span className="t-meta">hue {Math.round(accentHue)}°</span>
        </div>
        <Segmented
          items={HUES.map((h) => ({
            key: h.hue,
            ariaLabel: h.label,
            style: { ["--swatch-h" as string]: h.hue },
            label: (
              <>
                <span className="appearance-swatch__dot" aria-hidden="true" />
                <span className="appearance-swatch__label">{h.label}</span>
              </>
            ),
          }))}
          value={Math.round(accentHue)}
          onSelect={setAccentHue}
          label="Mirror colour"
          className="appearance-swatches"
        />
        <Range
          track="hue"
          min={0}
          max={360}
          step={1}
          value={accentHue}
          ariaLabel="Mirror colour hue"
          onChange={setAccentHue}
        />
      </div>

      <div className="appearance-block">
        <div className="appearance-block__head">
          <span className="t-ui">Orb</span>
          <span className="t-meta">
            {accentShifted
              ? "following the mirror colour"
              : "shipped defaults"}
            {customCount > 0 && ` · ${customCount} yours`}
          </span>
        </div>
        <p className="appearance-orb__note t-meta">
          One tint per state. Move the mirror colour above and every state
          follows it, each keeping its own offset; pick one here and that state
          stops following until you restore it.
        </p>
        <ul className="appearance-orb">
          {HAZE_STATES.map((state) => {
            const colour = resolveHaze(state, accentHue, accentShifted, hazeOverrides);
            const source = hazeSource(state, accentShifted, hazeOverrides);
            return (
              <li key={state} className="appearance-orb__row">
                {/* The swatch IS the input — a colour well the operator clicks
                    straight into, rather than a preview beside a control. */}
                <ColorWell
                  value={colour}
                  onChange={(next) => setHaze(state, next)}
                  ariaLabel={`${state} haze colour`}
                  testId={`orb-haze-${state}`}
                />
                <span className="appearance-orb__name">{state.replace('_', ' ')}</span>
                <span className="appearance-orb__role t-meta">{STATE_ROLE[state]}</span>
                <span className="appearance-orb__source t-meta">
                  {SOURCE_NOTE[source]}
                </span>
                <Hint
                  label={
                    source === 'custom'
                      ? `Hand ${state} back to ${accentShifted ? 'the mirror colour' : 'its default'}`
                      : undefined
                  }
                >
                  <Button
                    onClick={() => clearHaze(state)}
                    disabled={source !== 'custom'}
                    ariaLabel={`restore ${state} haze`}
                  >
                    restore
                  </Button>
                </Hint>
              </li>
            );
          })}
        </ul>
        <div className="voice-settings-actions">
          <Button onClick={clearHazes} disabled={customCount === 0}>
            restore every state
          </Button>
        </div>
      </div>

      <div className="voice-settings-actions">
        <Button
          onClick={reset}
          disabled={isDefault}
        >
          reset to default
        </Button>
      </div>
    </section>
  );
}
