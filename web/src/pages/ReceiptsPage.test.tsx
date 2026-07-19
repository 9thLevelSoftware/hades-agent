// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom/vitest";

import type {
  ReceiptDetailResponse,
  ReceiptListResponse,
  ReceiptObservationsResponse,
  ReceiptSummary,
} from "@/lib/api";

// Mock the API client module — the page must consume the typed client,
// never fetch directly, so mocking "@/lib/api" intercepts every call.
vi.mock("@/lib/api", () => {
  return {
    api: {
      getReceipts: vi.fn(),
      getReceipt: vi.fn(),
      getReceiptObservations: vi.fn(),
    },
    getManagementProfile: () => "",
  };
});

import { api } from "@/lib/api";
import ReceiptsPage from "./ReceiptsPage";

const RECEIPT_ID = `rct_${"1".repeat(64)}`;
const SECOND_ID = `rct_${"2".repeat(64)}`;
const OBSERVATION_ID = `obs_${"3".repeat(64)}`;
const EVIDENCE_ID = `evd_${"4".repeat(64)}`;
const ARTIFACT_ID = `art_${"5".repeat(64)}`;
const ARTIFACT_SHA = "a".repeat(64);
const DECIDED_AT = "2026-07-10T00:00:00Z";
const OBSERVED_AT = "2026-07-11T09:00:00Z";

function summary(overrides: Partial<ReceiptSummary>): ReceiptSummary {
  return {
    receipt_id: RECEIPT_ID,
    source: { source_kind: "turn", source_id: "s1:t7" },
    subject_kind: "turn",
    subject_id: "s1:t7",
    session_id: "s1",
    status: "verified",
    scorer_id: "hades.code-turn-end-state",
    scorer_version: "1.0",
    decided_at: DECIDED_AT,
    content_hash: `sha256:${"b".repeat(64)}`,
    ...overrides,
  };
}

const LIST: ReceiptListResponse = {
  ok: true,
  receipts: [
    summary({}),
    summary({
      receipt_id: SECOND_ID,
      subject_id: "s1:t8",
      status: "completed_unverified",
    }),
  ],
  count: 2,
  next_cursor: null,
};

const DETAIL: ReceiptDetailResponse = {
  ok: true,
  receipt_id: RECEIPT_ID,
  receipt: {
    receipt_id: RECEIPT_ID,
    source: { source_kind: "turn", source_id: "s1:t7" },
    subject_kind: "turn",
    subject_id: "s1:t7",
    session_id: "s1",
    turn_id: "t7",
    mission_id: null,
    transaction_id: null,
    requested_outcome: {
      outcome_kind: "code_change",
      description: "add marker to README",
      constraints: ["no force push"],
      producer_id: "hades.turn-ledger",
      content_hash: `sha256:${"c".repeat(64)}`,
    },
    status: "verified",
    claims: [],
    evidence: [
      {
        evidence_id: EVIDENCE_ID,
        evidence_kind: "verification_check",
        source_ref: "<redacted>/verification_evidence.db:check:s1:t7",
        producer_id: "hades.verification",
        observed_at: DECIDED_AT,
        fresh_until: null,
        summary: "pytest ran after final edit",
        payload_hash: `sha256:${"d".repeat(64)}`,
        artifact_ids: [ARTIFACT_ID],
        content_hash: `sha256:${"e".repeat(64)}`,
      },
    ],
    artifacts: [
      {
        artifact_id: ARTIFACT_ID,
        source_kind: "code_execution",
        source_ref: "<redacted>/artifacts/report.md",
        display_name: "report.md",
        media_type: "text/markdown",
        size_bytes: 42,
        sha256: ARTIFACT_SHA,
        mtime_ns: null,
        captured_at: DECIDED_AT,
        content_hash: `sha256:${"f".repeat(64)}`,
      },
    ],
    uncertainty: ["recheck depends on the artifact still existing"],
    scorer_id: "hades.code-turn-end-state",
    scorer_version: "1.0",
    decided_at: DECIDED_AT,
    content_hash: `sha256:${"b".repeat(64)}`,
  },
  claim_edges: [
    {
      claim_id: `clm_${"6".repeat(64)}`,
      claim_kind: "effect",
      statement: "README contains marker",
      verdict: "satisfied",
      required: true,
      evidence_ids: [EVIDENCE_ID],
      artifact_ids: [ARTIFACT_ID],
      uncertainty: [],
    },
  ],
  original_status: "verified",
  latest_observation: {
    observation_id: OBSERVATION_ID,
    receipt_id: RECEIPT_ID,
    previous_observation_id: null,
    status: "failed",
    claims: [],
    evidence: [],
    artifacts: [],
    uncertainty: ["Artifact hash changed after issuance"],
    scorer_id: "hades.code-turn-end-state",
    scorer_version: "1.0",
    observed_at: OBSERVED_AT,
    content_hash: `sha256:${"9".repeat(64)}`,
  },
  observation_count: 1,
  attestations: [
    {
      attestation_id: `att_${"7".repeat(64)}`,
      target_kind: "receipt",
      target_id: RECEIPT_ID,
      target_content_hash: `sha256:${"b".repeat(64)}`,
      provider_id: "test-signer",
      key_id: "k1",
      algorithm: "hmac-sha256",
      signature_b64: "c2ln",
      signed_at: DECIDED_AT,
      verification_state: "unverified_import",
      content_hash: `sha256:${"8".repeat(64)}`,
      role: "provenance only",
    },
  ],
  status_note: "independently scored end state",
  recheck_hint: `hades receipt recheck ${RECEIPT_ID} (or /receipt recheck ${RECEIPT_ID})`,
};

const OBSERVATIONS: ReceiptObservationsResponse = {
  ok: true,
  receipt_id: RECEIPT_ID,
  observations: [DETAIL.latest_observation!],
  count: 1,
};

function primeApi() {
  vi.mocked(api.getReceipts).mockResolvedValue(LIST);
  vi.mocked(api.getReceipt).mockResolvedValue(DETAIL);
  vi.mocked(api.getReceiptObservations).mockResolvedValue(OBSERVATIONS);
}

beforeEach(() => {
  vi.clearAllMocks();
  cleanup();
  primeApi();
});

async function openDetail(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByText("s1:t7");
  await user.click(screen.getAllByRole("button", { name: /inspect/i })[0]);
  await screen.findByText("add marker to README");
}

describe("ReceiptsPage", () => {
  it("lists receipts with truthful status labels and filters by status", async () => {
    const user = userEvent.setup();
    render(<ReceiptsPage />);
    await screen.findByText("s1:t7");
    expect(screen.getByText("s1:t8")).toBeVisible();
    // completed_unverified is never rendered as success.
    expect(
      screen.getByText(/claimed — not independently verified/i),
    ).toBeVisible();
    expect(screen.queryByText(/success/i)).toBeNull();

    await user.selectOptions(
      screen.getByLabelText(/filter by status/i),
      "verified",
    );
    await waitFor(() =>
      expect(api.getReceipts).toHaveBeenLastCalledWith(
        expect.objectContaining({ status: "verified" }),
      ),
    );
  });

  it("expands a claim to its evidence and artifact edges with artifact hashes", async () => {
    const user = userEvent.setup();
    render(<ReceiptsPage />);
    await openDetail(user);
    expect(screen.getByText("README contains marker")).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: /evidence & artifacts/i }),
    );
    expect(await screen.findByText(EVIDENCE_ID)).toBeVisible();
    expect(screen.getAllByText(ARTIFACT_ID).length).toBeGreaterThan(0);
    // Artifact digests are shown as full sha256 hashes.
    expect(screen.getByText(new RegExp(ARTIFACT_SHA))).toBeVisible();
  });

  it("distinguishes the original decision from the latest recheck with freshness and uncertainty", async () => {
    const user = userEvent.setup();
    render(<ReceiptsPage />);
    await openDetail(user);
    expect(screen.getByText(/original:/i)).toBeVisible();
    expect(screen.getByText(/latest recheck:/i)).toBeVisible();
    // Freshness facts for both decisions.
    expect(screen.getByText(new RegExp(DECIDED_AT))).toBeVisible();
    expect(screen.getByText(new RegExp(OBSERVED_AT))).toBeVisible();
    // Uncertainty is surfaced, not hidden.
    expect(
      screen.getByText(/recheck depends on the artifact still existing/i),
    ).toBeVisible();
    expect(
      screen.getByText(/artifact hash changed after issuance/i),
    ).toBeVisible();

    // Full append-only history loads through the observations endpoint.
    await user.click(
      screen.getByRole("button", { name: /observation history/i }),
    );
    await waitFor(() =>
      expect(api.getReceiptObservations).toHaveBeenCalledWith(RECEIPT_ID),
    );
    expect(await screen.findByText(OBSERVATION_ID)).toBeVisible();
  });

  it("labels signatures provenance-only and hints at the CLI recheck without mutation controls", async () => {
    const user = userEvent.setup();
    render(<ReceiptsPage />);
    await openDetail(user);
    expect(screen.getByText(/provenance only/i)).toBeVisible();
    // Primary control stays in the terminal: the page links the command.
    expect(
      screen.getByText(new RegExp(`hades receipt recheck ${RECEIPT_ID}`)),
    ).toBeVisible();
    // Inspection-only: no recheck/prune/sign/export/delete controls exist.
    expect(
      screen.queryByRole("button", {
        name: /recheck|prune|sign|export|delete|verify/i,
      }),
    ).toBeNull();
  });

  it("renders loading, empty, and error states", async () => {
    // Loading: a pending fetch shows the loading indicator.
    vi.mocked(api.getReceipts).mockReturnValue(
      new Promise(() => {}) as ReturnType<typeof api.getReceipts>,
    );
    const { unmount } = render(<ReceiptsPage />);
    expect(screen.getByTestId("receipts-loading")).toBeInTheDocument();
    unmount();
    cleanup();

    // Empty: no receipts stored.
    vi.mocked(api.getReceipts).mockResolvedValue({
      ok: true,
      receipts: [],
      count: 0,
      next_cursor: null,
    });
    const empty = render(<ReceiptsPage />);
    expect(await screen.findByText(/no receipts/i)).toBeVisible();
    empty.unmount();
    cleanup();

    // Error: load failures render non-destructively.
    vi.mocked(api.getReceipts).mockRejectedValue(new Error("boom"));
    render(<ReceiptsPage />);
    expect(await screen.findByText(/failed to load/i)).toBeVisible();
  });
});
