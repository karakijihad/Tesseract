import type { ComponentType, ReactNode } from "react";

import type { NavRailGroup } from "../../components/common/NavRail";

import { AboutSection } from "./About";
import { AppearanceSection } from "./Appearance";
import { CapabilitiesSection } from "./Capabilities";
import { SessionControlSection } from "./SessionControl";
import { CostSection } from "./Cost";
import { KeysSection } from "./Keys";
import { LocalModelsSection } from "./LocalModels";
import { LoopLimitsSection } from "./LoopLimits";
import { ChainsSection } from "./Chains";
import { ModelRolesSection } from "./ModelRoles";
import { RawConfigSection } from "./RawConfig";
import { SystemSection } from "./System";
import { ToolsSection } from "./Tools";
import { VoiceSection } from "./Voice";

// One stroke weight, one box, one colour source (`currentColor`) so a rail row
// tints its icon and its label together.
function Glyph({ children }: { children: ReactNode }) {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.3"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {children}
    </svg>
  );
}

const ICONS: Record<string, ReactNode> = {
  about: (
    <Glyph>
      <circle cx="8" cy="8" r="6" />
      <path d="M8 7.2v4M8 4.9v.6" />
    </Glyph>
  ),
  mode: (
    <Glyph>
      <path d="M8 1.8 13 4v4c0 3-2.2 5.2-5 6.2C5.2 13.2 3 11 3 8V4z" />
      <path d="M6 8l1.5 1.5L10.5 6.5" />
    </Glyph>
  ),
  capabilities: (
    <Glyph>
      <path d="M8 1.8l1.6 3.7 3.7 1.6-3.7 1.6L8 12.4 6.4 8.7 2.7 7.1l3.7-1.6z" />
    </Glyph>
  ),
  voice: (
    <Glyph>
      <path d="M2 7v2M5 4.5v7M8 2.5v11M11 4.5v7M14 7v2" />
    </Glyph>
  ),
  roles: (
    <Glyph>
      <path d="M8 1.8 14.5 5 8 8.2 1.5 5z" />
      <path d="M1.5 8.5 8 11.7l6.5-3.2M1.5 11.5 8 14.7l6.5-3.2" />
    </Glyph>
  ),
  local: (
    <Glyph>
      <rect x="4.5" y="4.5" width="7" height="7" rx="1.2" />
      <path d="M6.5 1.8v2.7M9.5 1.8v2.7M6.5 11.5v2.7M9.5 11.5v2.7M1.8 6.5h2.7M1.8 9.5h2.7M11.5 6.5h2.7M11.5 9.5h2.7" />
    </Glyph>
  ),
  keys: (
    <Glyph>
      <circle cx="5" cy="8" r="2.7" />
      <path d="M7.7 8H14M11.6 8v2.3M13.4 8v1.6" />
    </Glyph>
  ),
  compact: (
    <Glyph>
      <path d="M2.5 8h11" />
      <path d="M5.6 4.6 8 2.2l2.4 2.4M5.6 11.4 8 13.8l2.4-2.4" />
    </Glyph>
  ),
  limits: (
    <Glyph>
      <path d="M3.4 6.4A5 5 0 0 1 13 8" />
      <path d="M12.6 9.6A5 5 0 0 1 3 8" />
      <path d="M13 4.8V8h-3.2M3 11.2V8h3.2" />
    </Glyph>
  ),
  cost: (
    <Glyph>
      <path d="M2.2 13.2h11.6" />
      <path d="M4.2 13.2V8.4M7.4 13.2V4.6M10.6 13.2v-5.9M13 13.2V6" />
    </Glyph>
  ),
  policy: (
    <Glyph>
      <circle cx="8" cy="8" r="6" />
      <path d="M8 4.7V8l2.3 1.4" />
    </Glyph>
  ),
  tools: (
    <Glyph>
      <path d="M10.4 2.4a3.2 3.2 0 0 0-3.9 4.2L2.4 10.7a1.4 1.4 0 0 0 2 2l4.1-4.1a3.2 3.2 0 0 0 4.2-3.9L10.9 6.4 9.6 5.1z" />
    </Glyph>
  ),
  system: (
    <Glyph>
      <rect x="1.8" y="3" width="12.4" height="8" rx="1.3" />
      <path d="M5.8 13.4h4.4M8 11v2.4" />
    </Glyph>
  ),
  raw: (
    <Glyph>
      <path d="M5.6 5 2.6 8l3 3M10.4 5l3 3-3 3M9.2 3.4 6.8 12.6" />
    </Glyph>
  ),
  appearance: (
    <Glyph>
      <circle cx="8" cy="8" r="6" />
      <path d="M8 2v12" />
      <path d="M8 2a6 6 0 0 1 0 12" fill="currentColor" stroke="none" />
    </Glyph>
  ),
};

export interface SettingsSection {
  key: string;
  /** Rail row — short enough to read down a 176px column. */
  label: string;
  /** Pane heading. Defaults to the rail label; set it where the section is
   *  known by a longer name than the rail has room for. */
  title?: string;
  icon: ReactNode;
  Body: ComponentType;
}

export interface SettingsGroup {
  label: string;
  sections: SettingsSection[];
}

/** The rail's contents, and the only place a section's identity is declared.
 *
 * Grouped rather than listed flat: fourteen equal rows is the infinite column
 * again, turned on its side. The order inside a group is the order an operator
 * meets them — Keys sits under Capabilities because that section is what
 * says which key is missing.
 */
export const SETTINGS_GROUPS: SettingsGroup[] = [
  {
    label: "Assistant",
    sections: [
      { key: "about", label: "About", icon: ICONS.about, Body: AboutSection },
      {
        key: "capabilities",
        label: "Capabilities",
        icon: ICONS.capabilities,
        Body: CapabilitiesSection,
      },
      { key: "voice", label: "Voice", icon: ICONS.voice, Body: VoiceSection },
      {
        key: "appearance",
        label: "Appearance",
        icon: ICONS.appearance,
        Body: AppearanceSection,
      },
    ],
  },
  {
    label: "Models",
    sections: [
      {
        key: "roles",
        label: "Model roles",
        icon: ICONS.roles,
        Body: ModelRolesSection,
      },
      {
        key: "chains",
        label: "Chains",
        title: "Chains — the failover orders roles follow",
        icon: ICONS.roles,
        Body: ChainsSection,
      },
      {
        key: "local-models",
        label: "Local models",
        title: "Local models — Ollama",
        icon: ICONS.local,
        Body: LocalModelsSection,
      },
      { key: "keys", label: "Keys", icon: ICONS.keys, Body: KeysSection },
    ],
  },
  {
    label: "Runtime",
    sections: [
      {
        key: "session-control",
        label: "Session control",
        title: "Session control — autosave, resume, compaction",
        icon: ICONS.compact,
        Body: SessionControlSection,
      },
      {
        key: "loop-limits",
        label: "Loop limits",
        icon: ICONS.limits,
        Body: LoopLimitsSection,
      },
      {
        key: "cost",
        label: "Cost",
        title: "Cost & budgets",
        icon: ICONS.cost,
        Body: CostSection,
      },
      { key: "tools", label: "Tools", icon: ICONS.tools, Body: ToolsSection },
    ],
  },
  {
    label: "System",
    sections: [
      {
        key: "system",
        label: "System",
        icon: ICONS.system,
        Body: SystemSection,
      },
      {
        key: "raw-config",
        label: "Raw config",
        icon: ICONS.raw,
        Body: RawConfigSection,
      },
    ],
  },
];

export const SETTINGS_SECTIONS: SettingsSection[] = SETTINGS_GROUPS.flatMap(
  (g) => g.sections,
);

/** The same groups as the shared rail reads them — label and icon only. The
 *  bodies stay here, so the rail primitive never learns what a section is. */
export const SETTINGS_RAIL: NavRailGroup[] = SETTINGS_GROUPS.map((g) => ({
  label: g.label,
  items: g.sections.map(({ key, label, icon }) => ({ key, label, icon })),
}));
