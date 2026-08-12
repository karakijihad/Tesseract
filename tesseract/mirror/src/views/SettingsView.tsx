import { AboutSection } from "./settings/About";
import { CapabilitiesSection } from "./settings/Capabilities";
import { CompactSection } from "./settings/Compact";
import { CostSection } from "./settings/Cost";
import { KeysSection } from "./settings/Keys";
import { LocalModelsSection } from "./settings/LocalModels";
import { LoopLimitsSection } from "./settings/LoopLimits";
import { ModelRolesSection } from "./settings/ModelRoles";
import { ModeSection } from "./settings/Mode";
import { RawConfigSection } from "./settings/RawConfig";
import { SessionPolicySection } from "./settings/SessionPolicy";
import { SystemSection } from "./settings/System";
import { ToolsSection } from "./settings/Tools";
import { VoiceSection } from "./settings/Voice";

export function SettingsView() {
  return (
    <div className="settings-panel">
      <div className="settings-header">
        <span className="settings-header__title">Settings</span>
        <span className="settings-header__meta t-meta">
          Runtime controls — edits persist to providers.yaml + roles.yaml +
          permissions.yaml + mirror.yaml; raw config is read-only
        </span>
      </div>
      <div className="settings-body">
        <AboutSection />
        <ModeSection />
        <CapabilitiesSection />
        {/* Directly under Capabilities: that section says which key is
            missing, and this is where it gets set. */}
        <KeysSection />
        <ModelRolesSection />
        <CompactSection />
        <LoopLimitsSection />
        <CostSection />
        <VoiceSection />
        <LocalModelsSection />
        <SystemSection />
        <SessionPolicySection />
        <ToolsSection />
        <RawConfigSection />
      </div>
    </div>
  );
}
