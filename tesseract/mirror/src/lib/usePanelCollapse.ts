import { useCallback, useState } from 'react';

export function usePanelCollapse(
  key: string,
  defaultCollapsed: boolean = false,
): [boolean, () => void] {
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    try {
      const stored = localStorage.getItem(key);
      if (stored === '1') return true;
      if (stored === '0') return false;
      return defaultCollapsed;
    } catch {
      return defaultCollapsed;
    }
  });
  const toggle = useCallback(() => {
    setCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(key, next ? '1' : '0');
      } catch {
        // ignore storage quota / disabled cookies
      }
      return next;
    });
  }, [key]);
  return [collapsed, toggle];
}
