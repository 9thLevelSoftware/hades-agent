import { useCallback, useEffect, useState } from "react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { H2 } from "@nous-research/ui/ui/components/typography/h2";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { api } from "@/lib/api";
import type {
  ReceiptClaimEdge,
  ReceiptDetailResponse,
  ReceiptObservationDoc,
  ReceiptStatus,
  ReceiptSummary,
} from "@/lib/api";

/**
 * Secondary READ-ONLY receipt inspection page.
 *
 * Inspection surface over the profile-scoped `/api/receipts` endpoints —
 * redacted projections of the same immutable receipt store behind the CLI
 * and native TUI. This page never rechecks, exports, signs, or prunes:
 * primary control stays in the terminal (`hades receipt ...` or
 * `/receipt ...`), and the detail view links the exact command.
 */

const STATUS_VALUES: ReceiptStatus[] = [
  "verified",
  "completed_unverified",
  "failed",
  "blocked",
  "unknown_effect",
];

const SUBJECT_KINDS = ["turn", "mission", "transaction", "external"];

// Truthful status language: completed_unverified is never rendered as a
// good outcome, and unknown_effect is never rendered as failed/retry-safe.
const STATUS_LABELS: Record<ReceiptStatus, string> = {
  verified: "Verified — independently scored end state",
  completed_unverified: "Claimed — not independently verified",
  failed: "Failed — the requested end state does not hold",
  blocked: "Blocked before the end state could hold",
  unknown_effect: "Unknown effect — do not retry; recheck evidence",
};

function statusTone(status: ReceiptStatus) {
  if (status === "verified") return "success" as const;
  if (status === "failed") return "destructive" as const;
  if (status === "blocked") return "secondary" as const;
  return "warning" as const;
}

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function StatusBadge({ status }: { status: ReceiptStatus }) {
  return <Badge tone={statusTone(status)}>{STATUS_LABELS[status]}</Badge>;
}

function ClaimEdgeRow({ edge }: { edge: ReceiptClaimEdge }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="flex flex-col gap-1 rounded border border-current/10 px-2 py-1.5 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={edge.verdict === "satisfied" ? "success" : "warning"}>
          {edge.verdict}
        </Badge>
        <span>{edge.statement}</span>
        {edge.required && (
          <span className="text-xs text-text-secondary">(required)</span>
        )}
        <Button size="xs" outlined onClick={() => setExpanded((v) => !v)}>
          Evidence &amp; artifacts
        </Button>
      </div>
      {expanded && (
        <div className="flex flex-col gap-1 pl-2 font-mono text-xs">
          <div>
            evidence:{" "}
            {edge.evidence_ids.length
              ? edge.evidence_ids.map((id) => <span key={id}>{id} </span>)
              : "(none)"}
          </div>
          <div>
            artifacts:{" "}
            {edge.artifact_ids.length
              ? edge.artifact_ids.map((id) => <span key={id}>{id} </span>)
              : "(none)"}
          </div>
          {edge.uncertainty.map((note) => (
            <div key={note}>uncertainty: {note}</div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function ReceiptsPage() {
  const [receipts, setReceipts] = useState<ReceiptSummary[] | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState("");
  const [subjectFilter, setSubjectFilter] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [detail, setDetail] = useState<ReceiptDetailResponse | null>(null);
  const [history, setHistory] = useState<ReceiptObservationDoc[] | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

  const loadList = useCallback(
    async (status: string, subject: string, cursor?: string) => {
      try {
        const page = await api.getReceipts({
          status: (status || undefined) as ReceiptStatus | undefined,
          subject: subject || undefined,
          cursor,
          limit: 100,
        });
        setReceipts((prev) =>
          cursor ? [...(prev ?? []), ...page.receipts] : page.receipts,
        );
        setNextCursor(page.next_cursor);
        setLoadError(null);
      } catch (err) {
        // Non-destructive: keep whatever is already rendered.
        setLoadError(
          "Failed to load receipts — nothing was changed. Retry, or inspect " +
            "from the terminal: hades receipt list",
        );
        void errorText(err);
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    void loadList(statusFilter, subjectFilter);
  }, [loadList, statusFilter, subjectFilter]);

  const openDetail = useCallback(async (receiptId: string) => {
    setDetailError(null);
    setHistory(null);
    try {
      setDetail(await api.getReceipt(receiptId));
    } catch (err) {
      setDetailError(errorText(err));
    }
  }, []);

  const loadHistory = useCallback(async (receiptId: string) => {
    setDetailError(null);
    try {
      const response = await api.getReceiptObservations(receiptId);
      setHistory(response.observations);
    } catch (err) {
      setDetailError(errorText(err));
    }
  }, []);

  const receipt = detail?.receipt ?? null;

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-6 p-4">
      <div>
        <H2>Receipts</H2>
        <p className="text-sm text-text-secondary">
          Immutable, independently scored records of what was asked, what
          evidence exists, what was produced, and what remains uncertain.
          This page is inspection-only: rechecking appends an observation
          from the terminal and never rewrites a receipt.
        </p>
      </div>

      {loadError && (
        <Card className="border-destructive">
          <CardContent className="py-3 text-sm">{loadError}</CardContent>
        </Card>
      )}
      {detailError && (
        <Card className="border-destructive">
          <CardContent className="py-3 text-sm">{detailError}</CardContent>
        </Card>
      )}

      {loading && (
        <div data-testid="receipts-loading">
          <Spinner />
        </div>
      )}

      {detail === null && !loading && (
        <section className="flex flex-col gap-2">
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <select
              aria-label="Filter by status"
              className="rounded border border-current/20 bg-transparent px-2 py-1"
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
            >
              <option value="">All statuses</option>
              {STATUS_VALUES.map((status) => (
                <option key={status} value={status}>
                  {status}
                </option>
              ))}
            </select>
            <select
              aria-label="Filter by subject"
              className="rounded border border-current/20 bg-transparent px-2 py-1"
              value={subjectFilter}
              onChange={(e) => setSubjectFilter(e.target.value)}
            >
              <option value="">All subjects</option>
              {SUBJECT_KINDS.map((kind) => (
                <option key={kind} value={kind}>
                  {kind}
                </option>
              ))}
            </select>
          </div>

          {receipts !== null && receipts.length === 0 && (
            <p className="text-sm text-text-secondary">
              No receipts stored yet.
            </p>
          )}
          {receipts !== null && receipts.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead>
                  <tr className="border-b border-current/20 text-xs uppercase text-text-secondary">
                    <th className="px-2 py-1">Subject</th>
                    <th className="px-2 py-1">Status</th>
                    <th className="px-2 py-1">Decided</th>
                    <th className="px-2 py-1">Scorer</th>
                    <th className="px-2 py-1">Receipt</th>
                    <th className="px-2 py-1" />
                  </tr>
                </thead>
                <tbody>
                  {receipts.map((summary) => (
                    <tr
                      key={summary.receipt_id}
                      className="border-b border-current/10"
                    >
                      <td className="px-2 py-1.5">
                        {summary.subject_kind}: <span>{summary.subject_id}</span>
                      </td>
                      <td className="px-2 py-1.5">
                        <StatusBadge status={summary.status} />
                      </td>
                      <td className="px-2 py-1.5 text-xs">
                        {summary.decided_at}
                      </td>
                      <td className="px-2 py-1.5 text-xs">
                        {summary.scorer_id} v{summary.scorer_version}
                      </td>
                      <td
                        className="max-w-40 truncate px-2 py-1.5 font-mono text-xs"
                        title={summary.receipt_id}
                      >
                        {summary.receipt_id}
                      </td>
                      <td className="px-2 py-1.5">
                        <Button
                          size="xs"
                          outlined
                          onClick={() => void openDetail(summary.receipt_id)}
                        >
                          Inspect
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {nextCursor && (
            <div>
              <Button
                size="xs"
                outlined
                onClick={() =>
                  void loadList(statusFilter, subjectFilter, nextCursor)
                }
              >
                Load more
              </Button>
            </div>
          )}
        </section>
      )}

      {detail !== null && receipt !== null && (
        <section className="flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-semibold uppercase tracking-wide">
              Receipt inspection
            </h3>
            <Button size="xs" ghost onClick={() => setDetail(null)}>
              Back to list
            </Button>
          </div>

          <Card>
            <CardContent className="flex flex-col gap-2 py-3 text-sm">
              <div className="font-mono text-xs" title={receipt.receipt_id}>
                {receipt.receipt_id}
              </div>
              <div>
                Requested outcome: {receipt.requested_outcome.outcome_kind} —{" "}
                <span>{receipt.requested_outcome.description}</span>
              </div>
              {receipt.requested_outcome.constraints.length > 0 && (
                <div className="text-xs text-text-secondary">
                  constraints:{" "}
                  {receipt.requested_outcome.constraints.join("; ")}
                </div>
              )}
              <div className="flex flex-wrap items-center gap-2">
                <span>Original:</span>
                <StatusBadge status={receipt.status} />
                <span className="text-xs text-text-secondary">
                  decided {receipt.decided_at} by {receipt.scorer_id} v
                  {receipt.scorer_version}
                </span>
              </div>
              {detail.latest_observation ? (
                <div className="flex flex-wrap items-center gap-2">
                  <span>Latest recheck:</span>
                  <StatusBadge status={detail.latest_observation.status} />
                  <span className="text-xs text-text-secondary">
                    observed {detail.latest_observation.observed_at} by{" "}
                    {detail.latest_observation.scorer_id} v
                    {detail.latest_observation.scorer_version}
                  </span>
                </div>
              ) : (
                <div>Latest recheck: none recorded yet</div>
              )}
              {[
                ...receipt.uncertainty,
                ...(detail.latest_observation?.uncertainty ?? []),
              ].map((note) => (
                <div key={note} className="text-warning text-xs">
                  uncertainty: {note}
                </div>
              ))}
            </CardContent>
          </Card>

          <div className="flex flex-col gap-2">
            <h4 className="text-xs font-semibold uppercase tracking-wide">
              Claims (claim → evidence → artifacts)
            </h4>
            {detail.claim_edges.length === 0 && (
              <p className="text-sm text-text-secondary">
                No claims recorded.
              </p>
            )}
            {detail.claim_edges.map((edge) => (
              <ClaimEdgeRow key={edge.claim_id} edge={edge} />
            ))}
          </div>

          <div className="flex flex-col gap-1">
            <h4 className="text-xs font-semibold uppercase tracking-wide">
              Artifacts
            </h4>
            {receipt.artifacts.length === 0 && (
              <p className="text-sm text-text-secondary">
                No artifacts recorded.
              </p>
            )}
            {receipt.artifacts.map((artifact) => (
              <div
                key={artifact.artifact_id}
                className="flex flex-col gap-0.5 rounded border border-current/10 px-2 py-1.5 text-xs"
              >
                <div>
                  {artifact.display_name} ({artifact.size_bytes} bytes)
                </div>
                <div className="font-mono">{artifact.artifact_id}</div>
                <div className="font-mono">sha256: {artifact.sha256}</div>
              </div>
            ))}
          </div>

          <div className="flex flex-col gap-1">
            <h4 className="text-xs font-semibold uppercase tracking-wide">
              Attestations — a signature proves who produced bytes, never
              that a claim is true
            </h4>
            {detail.attestations.length === 0 && (
              <p className="text-sm text-text-secondary">None recorded.</p>
            )}
            {detail.attestations.map((attestation) => (
              <div
                key={attestation.attestation_id}
                className="flex flex-wrap items-center gap-2 rounded border border-current/10 px-2 py-1.5 text-xs"
              >
                <span className="font-mono">
                  {attestation.attestation_id}
                </span>
                <span>provider: {attestation.provider_id}</span>
                <span>state: {attestation.verification_state}</span>
                <Badge tone="secondary">{attestation.role}</Badge>
              </div>
            ))}
          </div>

          <div className="flex flex-col gap-1">
            <div className="flex items-center gap-2">
              <h4 className="text-xs font-semibold uppercase tracking-wide">
                Recheck history
              </h4>
              <Button
                size="xs"
                outlined
                onClick={() => void loadHistory(receipt.receipt_id)}
              >
                Observation history ({detail.observation_count})
              </Button>
            </div>
            {history !== null && history.length === 0 && (
              <p className="text-sm text-text-secondary">
                No observations appended yet.
              </p>
            )}
            {(history ?? []).map((observation) => (
              <div
                key={observation.observation_id}
                className="flex flex-wrap items-center gap-2 rounded border border-current/10 px-2 py-1.5 text-xs"
              >
                <span className="font-mono">
                  {observation.observation_id}
                </span>
                <StatusBadge status={observation.status} />
                <span>observed {observation.observed_at}</span>
              </div>
            ))}
            <p className="text-xs text-text-secondary">
              A recheck appends a linked observation and never rewrites the
              original receipt. Run it from the terminal:
            </p>
            <code className="text-xs">{detail.recheck_hint}</code>
          </div>
        </section>
      )}
    </div>
  );
}
