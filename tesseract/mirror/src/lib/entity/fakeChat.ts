import { getController } from './registry';

export interface FakeChatStep {
  /** Chunk of text to feed as a text_delta */
  text: string;
  /** ms to wait after this chunk before the next */
  delay: number;
}

export interface FakeChatScript {
  id: string;
  label: string;
  description: string;
  steps: FakeChatStep[];
}

export const FAKE_CHAT_SCRIPTS: FakeChatScript[] = [
  {
    id: 'calm',
    label: 'Calm',
    description: 'Smooth, low-intensity motion — reflective sentence',
    steps: [
      { text: "I've been ", delay: 140 },
      { text: 'thinking about ', delay: 160 },
      { text: 'this, and ', delay: 180 },
      { text: 'I believe the ', delay: 140 },
      { text: 'answer lies in ', delay: 160 },
      { text: 'the patterns ', delay: 140 },
      { text: 'we have already ', delay: 160 },
      { text: 'seen.', delay: 400 },
    ],
  },
  {
    id: 'urgent',
    label: 'Urgent',
    description: 'Fast, sharp, ejective motion — critical alert',
    steps: [
      { text: 'Warning!', delay: 50 },
      { text: ' Critical', delay: 40 },
      { text: ' failure', delay: 40 },
      { text: ' detected', delay: 40 },
      { text: ' —', delay: 40 },
      { text: ' immediate', delay: 40 },
      { text: ' action', delay: 40 },
      { text: ' required!', delay: 50 },
    ],
  },
  {
    id: 'analytical',
    label: 'Analytical',
    description: 'Steady, deliberate cadence — numbered analysis',
    steps: [
      { text: 'First, ', delay: 260 },
      { text: 'examine the ', delay: 180 },
      { text: 'data. ', delay: 400 },
      { text: 'Second, ', delay: 260 },
      { text: 'cross-reference ', delay: 180 },
      { text: 'against prior ', delay: 180 },
      { text: 'observations. ', delay: 400 },
      { text: 'Third, ', delay: 260 },
      { text: 'draw conclusions.', delay: 300 },
    ],
  },
  {
    id: 'pause',
    label: 'Long Pause',
    description: 'Decay test — text burst, long silence, then another burst',
    steps: [
      { text: 'The signal is ', delay: 140 },
      { text: 'clear at first.', delay: 3000 },
      { text: 'Then it ', delay: 140 },
      { text: 'returns, quietly.', delay: 200 },
    ],
  },
];

/** Run a script against the live IntensitySignals via the controller registry */
export async function playFakeChat(script: FakeChatScript, onTick?: () => void): Promise<void> {
  const controller = getController();
  if (!controller) return;
  const signals = controller.getSignals();
  for (const step of script.steps) {
    signals.onTextDelta(step.text.length);
    onTick?.();
    await new Promise((r) => setTimeout(r, step.delay));
  }
}
