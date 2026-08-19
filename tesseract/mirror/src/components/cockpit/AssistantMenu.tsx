import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { useCaptionsStore } from "../../stores/captions";
import { useOrbVisibilityStore } from "../../stores/orbVisibility";
import { Hint } from "../ui/Hint";
import { MenuItem } from "../common/MenuItem";

const GAP = 8;
const PAD = 8;

/** What the assistant shows of itself — the orb, and its captions.
 *
 * Both toggles lived in the bottom HUD's stage stack, three surfaces away from
 * the things they control. They hang off the assistant's own name now: the orb
 * is the assistant's body on screen and the captions are it speaking, so the
 * name is where an operator looks for them.
 */
export function AssistantMenu({ name }: { name: string }) {
  const orbVisible = useOrbVisibilityStore((s) => s.visible);
  const toggleOrb = useOrbVisibilityStore((s) => s.toggle);
  const captionsOn = useCaptionsStore((s) => s.enabled);
  const toggleCaptions = useCaptionsStore((s) => s.toggle);

  const [open, setOpen] = useState(false);
  const btnRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [coords, setCoords] = useState<{ top: number; left: number } | null>(
    null,
  );

  // The HUD pill clips its overflow, so the menu portals to <body> and
  // positions in viewport coords — the same constraint Hint works around.
  const reposition = useCallback(() => {
    const btn = btnRef.current;
    const menu = menuRef.current;
    if (!btn || !menu) return;
    const b = btn.getBoundingClientRect();
    const m = menu.getBoundingClientRect();
    const left = Math.max(
      PAD,
      Math.min(
        b.left + b.width / 2 - m.width / 2,
        window.innerWidth - m.width - PAD,
      ),
    );
    setCoords({ top: b.bottom + GAP, left });
  }, []);

  useLayoutEffect(() => {
    if (open) reposition();
    else setCoords(null);
  }, [open, reposition]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (btnRef.current?.contains(t) || menuRef.current?.contains(t)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    const onMove = () => reposition();
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    window.addEventListener("resize", onMove);
    window.addEventListener("scroll", onMove, true);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", onMove);
      window.removeEventListener("scroll", onMove, true);
    };
  }, [open, reposition]);

  const rows = [
    { key: "orb", label: "Orb", on: orbVisible, toggle: toggleOrb },
    { key: "cc", label: "Captions", on: captionsOn, toggle: toggleCaptions },
  ];

  return (
    <>
      <Hint label={`What ${name} shows — the orb, and its captions`} position="bottom">
        <button
          ref={btnRef}
          type="button"
          className={`top-status-hud__name${open ? " is-open" : ""}`}
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          aria-haspopup="menu"
        >
          {name}
        </button>
      </Hint>
      {open &&
        createPortal(
          <div
            ref={menuRef}
            className="assistant-menu"
            role="menu"
            aria-label={`${name} display`}
            style={{
              top: coords?.top ?? -9999,
              left: coords?.left ?? -9999,
              opacity: coords ? 1 : 0,
            }}
          >
            {rows.map((r) => (
              <MenuItem
                key={r.key}
                role="menuitemcheckbox"
                checked={r.on}
                onClick={r.toggle}
              >
                <span
                  className={`assistant-menu__box${r.on ? " is-on" : ""}`}
                  aria-hidden="true"
                >
                  {r.on ? "✓" : ""}
                </span>
                <span className="assistant-menu__label">{r.label}</span>
              </MenuItem>
            ))}
          </div>,
          document.body,
        )}
    </>
  );
}
