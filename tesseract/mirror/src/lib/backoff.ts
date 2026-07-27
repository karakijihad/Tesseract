export const BACKOFF_SCHEDULE = [1000, 2000, 4000, 8000, 10000] as const;

export function nextDelay(attempt: number): number {
  const idx = Math.min(attempt, BACKOFF_SCHEDULE.length - 1);
  return BACKOFF_SCHEDULE[idx];
}
