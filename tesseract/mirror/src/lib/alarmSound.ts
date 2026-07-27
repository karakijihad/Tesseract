// Continuous two-tone alarm tone — classic digital-alarm cadence (alternating
// 880/1320 Hz triangle, ~280 ms beep + 220 ms gap). Loop runs until either
// (a) every active alarm is dismissed/snoozed, or (b) a 30 s safety cap
// elapses (so an unattended page doesn't ring forever).
//
// AudioContext is reused across fires so the browser's user-gesture
// authorization (granted by any first click on the page) is remembered.
// A backgrounded tab's context auto-suspends — `resume()` is best-effort.

const TONES_HZ = [880, 1320] as const;
const BEEP_MS = 280;
const GAP_MS = 220;
const MAX_LOOP_MS = 30_000;
const PEAK_GAIN = 0.5;

let _ctx: AudioContext | null = null;
let _intervalHandle: number | null = null;
let _stopAt: number | null = null;
const _active = new Set<string>();
let _toneIdx = 0;

function _ensureCtx(): AudioContext | null {
  if (_ctx !== null) return _ctx;
  const Ctor = window.AudioContext
    ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!Ctor) return null;
  _ctx = new Ctor();
  return _ctx;
}

function _beepOnce(ctx: AudioContext, freq: number): void {
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.type = 'triangle';
  osc.frequency.value = freq;
  osc.connect(gain);
  gain.connect(ctx.destination);
  const t0 = ctx.currentTime;
  const dur = BEEP_MS / 1000;
  // Sharp attack, brief sustain, fast release — sounds like a digital alarm
  // tick, not a soft chime. exponentialRamp can't end at zero, hence 0.0001.
  gain.gain.setValueAtTime(0.0001, t0);
  gain.gain.exponentialRampToValueAtTime(PEAK_GAIN, t0 + 0.01);
  gain.gain.setValueAtTime(PEAK_GAIN, t0 + dur - 0.03);
  gain.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
  osc.start(t0);
  osc.stop(t0 + dur + 0.02);
}

function _tick(ctx: AudioContext): void {
  if (_stopAt !== null && Date.now() >= _stopAt) {
    _stopLoop();
    return;
  }
  _beepOnce(ctx, TONES_HZ[_toneIdx]);
  _toneIdx = 1 - _toneIdx;
}

function _stopLoop(): void {
  if (_intervalHandle !== null) {
    window.clearInterval(_intervalHandle);
    _intervalHandle = null;
  }
  _stopAt = null;
  _toneIdx = 0;
}

function _startLoop(): void {
  if (_intervalHandle !== null) return;
  const ctx = _ensureCtx();
  if (!ctx) return;
  const begin = () => {
    _stopAt = Date.now() + MAX_LOOP_MS;
    _toneIdx = 0;
    try { _tick(ctx); } catch { /* silent */ }
    _intervalHandle = window.setInterval(() => {
      try { _tick(ctx); } catch { _stopLoop(); }
    }, BEEP_MS + GAP_MS);
  };
  if (ctx.state === 'suspended') {
    void ctx.resume().then(begin).catch(() => { /* silent — toast still surfaces */ });
  } else {
    begin();
  }
}

/** Start (or keep going) the alarm loop on behalf of a specific alarm.
 *  Refreshes the 30 s safety cap so a fresh alarm always gets a full
 *  window even if it lands mid-loop. */
export function startAlarmTone(alarmId: string): void {
  _active.add(alarmId);
  _stopAt = Date.now() + MAX_LOOP_MS;
  _startLoop();
}

/** Mark a single alarm dismissed/snoozed; stops the loop iff none remain. */
export function stopAlarmToneFor(alarmId: string): void {
  _active.delete(alarmId);
  if (_active.size === 0) _stopLoop();
}

/** Force-stop the loop and clear all active alarms (e.g. tab teardown). */
export function stopAllAlarmTones(): void {
  _active.clear();
  _stopLoop();
}
