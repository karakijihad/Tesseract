import { RailView } from "../components/common/RailView";
import { SETTINGS_GROUPS } from "./settings/nav";

// P7 item 6 — Settings was one continuous column of fourteen sections, so the
// one you came for was reached by scrolling past the ones you did not. It is
// the app's `RailView` now, the same shape every other view took on
// 2026-08-14: sections up front, one body mounted at a time.
export function SettingsView() {
  return (
    <RailView
      groups={SETTINGS_GROUPS}
      label="Settings sections"
      searchable
      foot="Edits persist to providers.yaml + roles.yaml + permissions.yaml + mirror.yaml; raw config is read-only"
    />
  );
}
