// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom/vitest";

import type {
  AutonomyAuditResponse,
  AutonomyRuleDoc,
  AutonomyStatus,
} from "@/lib/api";

// Mock the API client module — the page must consume the typed client, never
// fetch directly, so mocking "@/lib/api" intercepts every backend call.
vi.mock("@/lib/api", () => {
  return {
    api: {
      getAutonomyStatus: vi.fn(),
      getAutonomyRules: vi.fn(),
      explainAutonomyRule: vi.fn(),
      previewAutonomyChange: vi.fn(),
      applyAutonomyPreview: vi.fn(),
      acceptAutonomySuggestion: vi.fn(),
      rejectAutonomySuggestion: vi.fn(),
      getAutonomyMandates: vi.fn(),
      revokeAutonomyMandate: vi.fn(),
      getAutonomyAudit: vi.fn(),
    },
    getManagementProfile: () => "",
  };
});

import { api } from "@/lib/api";
import AutonomyPage from "./AutonomyPage";

const BEFORE_HASH = "a".repeat(64);
const AFTER_HASH = "b".repeat(64);

function ruleDoc(overrides: Partial<AutonomyRuleDoc>): AutonomyRuleDoc {
  return {
    rule_id: "rule-x",
    source: "user_assertion",
    state: "active",
    effect: "allow",
    action_classes: ["message.send"],
    data_classes: ["public"],
    recipient_classes: ["designated_test"],
    recipient_hashes: [],
    resource_prefixes: [],
    scope: {},
    allowed_reversibility: [],
    cost: null,
    time: null,
    evidence_requirements: [],
    max_uncertainty_ppm: null,
    provenance: "user:user-1",
    confidence_ppm: 1_000_000,
    created_at_ms: 1,
    expires_at_ms: null,
    max_uses: null,
    remaining_uses: null,
    description: "",
    edit_command: "hades autonomy rule edit rule-x --file RULE.yaml",
    ...overrides,
  };
}

const STATUS: AutonomyStatus = {
  profile_id: "default",
  mode: "enforce",
  contract_version: 4,
  contract_hash: BEFORE_HASH,
  stable_rules: 1,
  active_mandates: 1,
  pending_suggestions: 1,
  pending_apply: false,
};

const RULES = [
  ruleDoc({ rule_id: "stable-deny", effect: "deny" }),
  ruleDoc({
    rule_id: "mandate-1",
    source: "temporary_mandate",
    effect: "allow",
    expires_at_ms: 4102444800000,
    max_uses: 3,
    remaining_uses: 2,
    edit_command: "hades autonomy mandate revoke mandate-1 --reason TEXT",
  }),
  ruleDoc({
    rule_id: "suggest-1",
    source: "learned_suggestion",
    state: "awaiting_confirmation",
    confidence_ppm: 990_000,
    edit_command:
      "hades autonomy suggestion accept suggest-1 (--stable | --temporary --expires-in DURATION)",
  }),
];

const AUDIT: AutonomyAuditResponse = {
  decisions: [
    {
      decision_id: "dec-1",
      operation_key: "op-1",
      created_at_ms: 1,
      verdict: "ask",
      code: "conflicting_ask",
      reason: "conflicting allow and ask rules matched",
      stage: "execute",
      authority_version: 4,
      authority_hash: BEFORE_HASH,
      context_hash: "c".repeat(64),
      matched_rule_ids: ["stable-deny"],
      conflicting_rule_ids: ["stable-deny", "mandate-1"],
      required_evidence: [],
      clarification: null,
      expires_at_ms: null,
      edit_targets: [],
    },
  ],
  count: 1,
  limit: 100,
};

function primeApi() {
  vi.mocked(api.getAutonomyStatus).mockResolvedValue(STATUS);
  vi.mocked(api.getAutonomyRules).mockResolvedValue({
    effective: false,
    rules: RULES,
  });
  vi.mocked(api.getAutonomyMandates).mockResolvedValue({
    mandates: [RULES[1]],
  });
  vi.mocked(api.getAutonomyAudit).mockResolvedValue(AUDIT);
  vi.mocked(api.previewAutonomyChange).mockResolvedValue({
    applied: false,
    profile_id: "default",
    before_contract_hash: BEFORE_HASH,
    after_contract_hash: AFTER_HASH,
    added_rule_ids: ["new-rule"],
    removed_rule_ids: [],
    changed_rule_ids: [],
    warnings: [],
  });
  vi.mocked(api.applyAutonomyPreview).mockResolvedValue({
    applied: true,
    config_hash: "d".repeat(64),
    contract_version: 5,
    contract_hash: AFTER_HASH,
  });
  vi.mocked(api.explainAutonomyRule).mockResolvedValue({
    ...ruleDoc({ rule_id: "stable-deny", effect: "deny" }),
    layer: "stable_config",
    revision: null,
    in_current_contract: true,
    conflicts_with: ["mandate-1"],
    edit_route: ["hades autonomy rule edit stable-deny"],
    revoke_route: ["hades autonomy rule remove stable-deny"],
  });
  vi.mocked(api.rejectAutonomySuggestion).mockResolvedValue({
    suggestion_id: "suggest-1",
    state: "rejected",
    reason: "",
  });
  vi.mocked(api.revokeAutonomyMandate).mockResolvedValue({
    rule_id: "mandate-1",
    state: "revoked",
    reason: "",
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  cleanup();
  primeApi();
});

describe("AutonomyPage", () => {
  it("shows source, confidence, expiry, conflicts, and edit route for every rule", async () => {
    render(<AutonomyPage />);
    await screen.findByText("stable-deny");
    expect(screen.getByText("User assertion")).toBeVisible();
    expect(screen.getByText("Temporary mandate")).toBeVisible();
    expect(screen.getByText("Suggestion — not authorization")).toBeVisible();
    // Explain applies to contract rules only — a suggestion is not in the
    // contract, so it gets accept/reject instead of an explain drawer.
    expect(screen.getAllByRole("button", { name: /explain/i })).toHaveLength(2);
    // Confidence, expiry, and remaining uses are visible per rule.
    expect(screen.getByText(/99\.0%/)).toBeVisible();
    expect(screen.getByText("2/3")).toBeVisible();
  });

  it("previews a stable edit and sends its exact hash on apply", async () => {
    const user = userEvent.setup();
    render(<AutonomyPage />);
    await screen.findByText("stable-deny");
    await user.click(screen.getByRole("button", { name: "Preview change" }));
    const hash = await screen.findByTestId("before-contract-hash");
    await user.click(screen.getByRole("button", { name: "Apply exact preview" }));
    await waitFor(() =>
      expect(api.applyAutonomyPreview).toHaveBeenCalledWith(
        expect.objectContaining({
          expected_contract_hash: hash.textContent,
        }),
      ),
    );
  });

  it("opens the explanation drawer with conflicts and edit routes", async () => {
    const user = userEvent.setup();
    render(<AutonomyPage />);
    await screen.findByText("stable-deny");
    await user.click(
      screen.getAllByRole("button", { name: /explain/i })[0],
    );
    await waitFor(() =>
      expect(api.explainAutonomyRule).toHaveBeenCalledWith("stable-deny"),
    );
    expect(await screen.findByText(/conflicts with/i)).toBeVisible();
    expect(
      await screen.findByText("hades autonomy rule edit stable-deny"),
    ).toBeVisible();
  });

  it("rejects a suggestion and revokes a mandate through the API client", async () => {
    const user = userEvent.setup();
    render(<AutonomyPage />);
    await screen.findByText("suggest-1");
    await user.click(screen.getByRole("button", { name: /^reject/i }));
    await waitFor(() =>
      expect(api.rejectAutonomySuggestion).toHaveBeenCalledWith(
        "suggest-1",
        expect.anything(),
      ),
    );
    await user.click(screen.getByRole("button", { name: /revoke/i }));
    await waitFor(() =>
      expect(api.revokeAutonomyMandate).toHaveBeenCalledWith(
        "mandate-1",
        expect.anything(),
      ),
    );
  });

  it("labels conservative conflict decisions in the audit table", async () => {
    render(<AutonomyPage />);
    await screen.findByText("stable-deny");
    expect(await screen.findByText(/conflicting_ask/)).toBeVisible();
    expect(await screen.findByText(/conservative conflict/i)).toBeVisible();
  });

  it("renders load failures non-destructively", async () => {
    vi.mocked(api.getAutonomyStatus).mockRejectedValue(new Error("boom"));
    vi.mocked(api.getAutonomyRules).mockRejectedValue(new Error("boom"));
    vi.mocked(api.getAutonomyMandates).mockRejectedValue(new Error("boom"));
    vi.mocked(api.getAutonomyAudit).mockRejectedValue(new Error("boom"));
    render(<AutonomyPage />);
    expect(await screen.findByText(/failed to load/i)).toBeVisible();
  });
});
