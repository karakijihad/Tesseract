import { useEffect, useRef, useState } from 'react';
import { useEntityStore } from '../../stores/entity';
import { getController } from '../../lib/entity/registry';
import { FAKE_CHAT_SCRIPTS, playFakeChat } from '../../lib/entity/fakeChat';
import type { EntityState } from '../../lib/types';
import { Button } from '../common/Button';
import { Input } from '../common/Input';
import { Range } from '../common/Range';
import { Hint } from '../ui/Hint';

const STATES: EntityState[] = [
  'idle',
  'thinking',
  'speaking',
  'spawning',
  'council',
  'listening',
  'error',
  'happy',
  'deep_focus',
  'dreaming',
];

interface BackendSignalState {
  agents_active: number;
  consolidation_depth: number;
  tokens_per_sec: number;
  mood_intensity: number;
  mood_valence: number;
  dreaming_cycle: number;
}

interface SliderDef {
  key: keyof BackendSignalState;
  min: number;
  max: number;
  step: number;
  format?: (v: number) => string;
}

const BACKEND_SLIDERS: SliderDef[] = [
  { key: 'agents_active',       min: 0,  max: 12, step: 1 },
  { key: 'consolidation_depth', min: 0,  max: 10, step: 1 },
  { key: 'tokens_per_sec',      min: 0,  max: 40, step: 1 },
  { key: 'mood_intensity',      min: 0,  max: 1,  step: 0.01, format: (v) => v.toFixed(2) },
  { key: 'mood_valence',        min: -1, max: 1,  step: 0.01, format: (v) => v.toFixed(2) },
  { key: 'dreaming_cycle',      min: 0,  max: 6,  step: 1,    format: (v) => v > 0 ? String(v) : 'null' },
];

export function MotionTestPanel() {
  const state = useEntityStore((s) => s.state);
  const setState = useEntityStore((s) => s.setState);
  const [manual, setManual] = useState('');
  const [playing, setPlaying] = useState<string | null>(null);
  const [intensity, setIntensity] = useState(0);
  const rafRef = useRef<number | null>(null);
  const [backend, setBackend] = useState<BackendSignalState>({
    agents_active: 0,
    consolidation_depth: 0,
    tokens_per_sec: 0,
    mood_intensity: 0.5,
    mood_valence: 0,
    dreaming_cycle: 0,
  });

  useEffect(() => {
    const loop = () => {
      const c = getController();
      if (c) {
        setIntensity(c.getSignals().computeIntensity(state));
      }
      rafRef.current = requestAnimationFrame(loop);
    };
    rafRef.current = requestAnimationFrame(loop);
    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    };
  }, [state]);

  const runScript = async (id: string) => {
    const script = FAKE_CHAT_SCRIPTS.find((s) => s.id === id);
    if (!script || playing) return;
    setPlaying(id);
    setState('speaking');
    await playFakeChat(script);
    setPlaying(null);
  };

  const runManual = async () => {
    const text = manual.trim();
    if (!text || playing) return;
    setPlaying('manual');
    setState('speaking');
    const chunks = text.match(/\S+\s*/g) ?? [text];
    const c = getController();
    const signals = c?.getSignals();
    for (const chunk of chunks) {
      signals?.onTextDelta(chunk.length);
      await new Promise((r) => setTimeout(r, 150));
    }
    setPlaying(null);
  };

  const triggerStep = () => {
    getController()?.getSignals().onStep();
  };

  const triggerError = () => {
    getController()?.getSignals().onError();
  };

  const resetSignals = () => {
    getController()?.getSignals().onReset();
  };

  const updateBackend = (patch: Partial<BackendSignalState>) => {
    setBackend((prev) => {
      const next = { ...prev, ...patch };
      getController()?.getSignals().ingestBackend({
        v: 1,
        agents_active: next.agents_active,
        consolidation_depth: next.consolidation_depth,
        tokens_per_sec: next.tokens_per_sec,
        mood_intensity: next.mood_intensity,
        mood_valence: next.mood_valence,
        dreaming_cycle: next.dreaming_cycle > 0 ? next.dreaming_cycle : null,
      });
      return next;
    });
  };

  return (
    <div className="motion-test-panel">
      <div className="mtp-header">
        <span className="mtp-title">Motion Test Harness</span>
        <Hint label="live computeIntensity(state)">
          <span className="mtp-intensity">i = {intensity.toFixed(2)}</span>
        </Hint>
      </div>

      <div className="mtp-section">
        <div className="mtp-label">State</div>
        <div className="mtp-state-grid">
          {STATES.map((s) => (
            <Button key={s} active={state === s} onClick={() => setState(s)}>
              {s}
            </Button>
          ))}
        </div>
      </div>

      <div className="mtp-section">
        <div className="mtp-label">Fake chat</div>
        <div className="mtp-row">
          {FAKE_CHAT_SCRIPTS.map((s) => (
            <Hint key={s.id} label={s.description}>
              <Button
                active={playing === s.id}
                onClick={() => runScript(s.id)}
                disabled={playing !== null}
              >
                {s.label}
              </Button>
            </Hint>
          ))}
        </div>
      </div>

      <div className="mtp-section">
        <div className="mtp-label">Manual phrase</div>
        <div className="mtp-row">
          <Input
            className="mtp-input"
            value={manual}
            ariaLabel="Manual phrase"
            onChange={setManual}
            onKeyDown={(e) => {
              if (e.key === 'Enter') runManual();
            }}
            placeholder="Type a sentence, Enter to replay…"
          />
          <Button onClick={runManual} disabled={playing !== null}>
            play
          </Button>
        </div>
      </div>

      <div className="mtp-section">
        <div className="mtp-label">Signals</div>
        <div className="mtp-row">
          <Button onClick={triggerStep}>onStep</Button>
          <Button onClick={triggerError}>onError</Button>
          <Button onClick={resetSignals}>reset</Button>
        </div>
      </div>

      <div className="mtp-section">
        <div className="mtp-label">Backend signals (Layer 2)</div>
        <div className="mtp-sliders">
          {BACKEND_SLIDERS.map(({ key, min, max, step, format }) => (
            <label key={key} className="mtp-slider">
              <span>{key}</span>
              <Range
                min={min} max={max} step={step}
                value={backend[key]}
                ariaLabel={key}
                onChange={(next) => updateBackend({ [key]: next })}
              />
              <span className="mtp-val">
                {format ? format(backend[key]) : backend[key]}
              </span>
            </label>
          ))}
        </div>
      </div>

      <div className="mtp-hint">Ctrl+Shift+D to toggle</div>
    </div>
  );
}
