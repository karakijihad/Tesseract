interface CellProps {
  label: string;
  value: number;
  max: number;
  onChange: (v: number) => void;
}

export function IntervalCell({ label, value, max, onChange }: CellProps) {
  const clamp = (n: number) => Math.max(0, Math.min(max, n));
  return (
    <div className="cadence-cell">
      <button
        type="button"
        className="cadence-cell-step"
        onClick={() => onChange(clamp(value + 1))}
        aria-label={`Increase ${label}`}
      >
        +
      </button>
      <div className="cadence-cell-display">
        <input
          className="cadence-cell-input"
          type="number"
          min={0}
          max={max}
          value={value}
          onChange={(e) => {
            const parsed = parseInt(e.target.value, 10);
            onChange(Number.isFinite(parsed) ? clamp(parsed) : 0);
          }}
          aria-label={label}
        />
        <span className="cadence-cell-label">{label}</span>
      </div>
      <button
        type="button"
        className="cadence-cell-step"
        onClick={() => onChange(clamp(value - 1))}
        aria-label={`Decrease ${label}`}
      >
        −
      </button>
    </div>
  );
}
