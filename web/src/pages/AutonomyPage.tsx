import { useCallback, useEffect, useState } from "react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { H2 } from "@nous-research/ui/ui/components/typography/h2";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { api } from "@/lib/api";
import type {
  AutonomyAuditResponse,
  AutonomyChangeRequest,
  AutonomyPreviewResponse,
  AutonomyRuleDoc,
  AutonomyRuleExplanation,
  AutonomyStatus,
} from "@/lib/api";

/**
 * Secondary Preferences & Autonomy Center management page.
 *
 * Read/edit surface over the profile-scoped `/api/autonomy/*` endpoints —
 * the same `AutonomyService` behind the CLI and native TUI. The primary
 * authoring surfaces remain terminal-first; this page never becomes a chat
 * surface and its failures render non-destructively.
 */

const SOURCE_LABELS: Record<AutonomyRuleDoc["source"], string> = {
  user_assertion: "User assertion",
  temporary_mandate: "Temporary mandate",
  learned_suggestion: "Suggestion — not authorization",
};

const DEFAULT_CHANGE_TEMPLATE = JSON.stringify(
  {
    set_rules: [
      {
        rule_id: "my-rule",
        effect: "ask",
        action_classes: ["message.send"],
        data_classes: ["public"],
        description: "describe the intent of this rule",
      },
    ],
    remove_rule_ids: [],
  },
  null,
  2,
);

const TEMPORARY_ACCEPT_MS = 7 * 24 * 60 * 60 * 1000; // 7 days

function formatConfidence(ppm: number): string {
  return `${(ppm / 10_000).toFixed(1)}%`;
}

function formatExpiry(expiresAtMs: number | null): string {
  if (expiresAtMs === null) return "—";
  try {
    return new Date(expiresAtMs).toLocaleString();
  } catch {
    return String(expiresAtMs);
  }
}

function formatUses(rule: AutonomyRuleDoc): string {
  if (rule.max_uses === null) return "—";
  return `${rule.remaining_uses ?? 0}/${rule.max_uses}`;
}

function effectTone(effect: AutonomyRuleDoc["effect"]) {
  if (effect === "deny") return "destructive" as const;
  if (effect === "ask") return "warning" as const;
  return "success" as const;
}

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

export default function AutonomyPage() {
  const [status, setStatus] = useState<AutonomyStatus | null>(null);
  const [rules, setRules] = useState<AutonomyRuleDoc[] | null>(null);
  const [mandates, setMandates] = useState<AutonomyRuleDoc[] | null>(null);
  const [audit, setAudit] = useState<AutonomyAuditResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const [explanation, setExplanation] = useState<AutonomyRuleExplanation | null>(
    null,
  );
  const [changeText, setChangeText] = useState(DEFAULT_CHANGE_TEMPLATE);
  const [preview, setPreview] = useState<AutonomyPreviewResponse | null>(null);
  const [previewedChange, setPreviewedChange] =
    useState<AutonomyChangeRequest | null>(null);
  const [applyNotice, setApplyNotice] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const [statusRes, rulesRes, mandatesRes, auditRes] =
      await Promise.allSettled([
        api.getAutonomyStatus(),
        api.getAutonomyRules(),
        api.getAutonomyMandates(),
        api.getAutonomyAudit(100),
      ]);
    if (statusRes.status === "fulfilled") setStatus(statusRes.value);
    if (rulesRes.status === "fulfilled") setRules(rulesRes.value.rules);
    if (mandatesRes.status === "fulfilled")
      setMandates(mandatesRes.value.mandates);
    if (auditRes.status === "fulfilled") setAudit(auditRes.value);
    const failed = [statusRes, rulesRes, mandatesRes, auditRes].filter(
      (r) => r.status === "rejected",
    );
    setLoadError(
      failed.length
        ? "Failed to load autonomy data — existing state is unchanged; retry or manage authority from the terminal (`hades autonomy`)."
        : null,
    );
    setLoading(false);
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const runAction = useCallback(
    async (action: () => Promise<unknown>) => {
      setActionError(null);
      try {
        await action();
        await refresh();
      } catch (err) {
        // Non-destructive: keep everything already rendered.
        setActionError(errorText(err));
      }
    },
    [refresh],
  );

  const handleExplain = useCallback(async (ruleId: string) => {
    setActionError(null);
    try {
      setExplanation(await api.explainAutonomyRule(ruleId));
    } catch (err) {
      setActionError(errorText(err));
    }
  }, []);

  const handlePreview = useCallback(async () => {
    setActionError(null);
    setApplyNotice(null);
    let parsed: AutonomyChangeRequest;
    try {
      parsed = JSON.parse(changeText) as AutonomyChangeRequest;
    } catch {
      setActionError(
        "Invalid change document: expected JSON with set_rules and/or remove_rule_ids.",
      );
      return;
    }
    try {
      const change: AutonomyChangeRequest = {
        set_rules: parsed.set_rules ?? [],
        remove_rule_ids: parsed.remove_rule_ids ?? [],
      };
      const result = await api.previewAutonomyChange(change);
      setPreview(result);
      setPreviewedChange(change);
    } catch (err) {
      setActionError(errorText(err));
    }
  }, [changeText]);

  const handleApply = useCallback(async () => {
    if (!preview || !previewedChange) return;
    setActionError(null);
    try {
      const applied = await api.applyAutonomyPreview({
        ...previewedChange,
        expected_contract_hash: preview.before_contract_hash,
      });
      setApplyNotice(
        `Applied: contract version ${applied.contract_version} (${applied.contract_hash.slice(0, 12)}…)`,
      );
      setPreview(null);
      setPreviewedChange(null);
      await refresh();
    } catch (err) {
      // A stale/cross-profile hash conflicts without writing anything.
      setActionError(errorText(err));
    }
  }, [preview, previewedChange, refresh]);

  const suggestions = (rules ?? []).filter(
    (r) => r.source === "learned_suggestion",
  );

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-6 p-4">
      <div>
        <H2>Autonomy</H2>
        <p className="text-sm text-text-secondary">
          One authority contract per profile: what the agent may do, spend,
          share, remember, interrupt about, or must ask before. Decisions are
          deterministic (deny over ask over allow) and suggestions never
          authorize anything until you explicitly accept them.
        </p>
      </div>

      {loadError && (
        <Card className="border-destructive">
          <CardContent className="py-3 text-sm">{loadError}</CardContent>
        </Card>
      )}
      {actionError && (
        <Card className="border-destructive">
          <CardContent className="py-3 text-sm">{actionError}</CardContent>
        </Card>
      )}
      {applyNotice && (
        <Card>
          <CardContent className="py-3 text-sm">{applyNotice}</CardContent>
        </Card>
      )}

      {loading && <Spinner />}

      {/* Contract status */}
      {status && (
        <Card>
          <CardContent className="flex flex-wrap items-center gap-x-6 gap-y-2 py-3 text-sm">
            <span>
              Profile <strong>{status.profile_id}</strong>
            </span>
            <span>
              Mode <Badge tone="secondary">{status.mode}</Badge>
            </span>
            <span>
              Contract version <strong>{status.contract_version}</strong>
            </span>
            <span className="font-mono text-xs" title="Current contract hash">
              {status.contract_hash.slice(0, 16)}…
            </span>
            <span>
              {status.stable_rules} stable · {status.active_mandates}{" "}
              mandate(s) · {status.pending_suggestions} suggestion(s) pending
            </span>
            {status.pending_apply && (
              <Badge tone="destructive">
                Crashed apply awaits recovery — authority fails closed
              </Badge>
            )}
          </CardContent>
        </Card>
      )}

      {/* Effective rules and suggestions */}
      <section className="flex flex-col gap-2">
        <h3 className="text-sm font-semibold uppercase tracking-wide">
          Rules
        </h3>
        {rules !== null && rules.length === 0 && (
          <p className="text-sm text-text-secondary">No rules yet.</p>
        )}
        {rules !== null && rules.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-current/20 text-xs uppercase text-text-secondary">
                  <th className="px-2 py-1">Rule</th>
                  <th className="px-2 py-1">Source</th>
                  <th className="px-2 py-1">State</th>
                  <th className="px-2 py-1">Effect</th>
                  <th className="px-2 py-1">Confidence</th>
                  <th className="px-2 py-1">Expires</th>
                  <th className="px-2 py-1">Uses</th>
                  <th className="px-2 py-1">Actions</th>
                </tr>
              </thead>
              <tbody>
                {rules.map((rule) => (
                  <tr key={rule.rule_id} className="border-b border-current/10">
                    <td className="px-2 py-1.5 font-mono text-xs">
                      {rule.rule_id}
                    </td>
                    <td className="px-2 py-1.5">
                      {SOURCE_LABELS[rule.source]}
                    </td>
                    <td className="px-2 py-1.5">{rule.state}</td>
                    <td className="px-2 py-1.5">
                      <Badge tone={effectTone(rule.effect)}>
                        {rule.effect}
                      </Badge>
                    </td>
                    <td className="px-2 py-1.5">
                      {formatConfidence(rule.confidence_ppm)}
                    </td>
                    <td className="px-2 py-1.5">
                      {formatExpiry(rule.expires_at_ms)}
                    </td>
                    <td className="px-2 py-1.5">{formatUses(rule)}</td>
                    <td className="px-2 py-1.5">
                      {rule.source === "learned_suggestion" ? (
                        <div className="flex flex-wrap gap-1">
                          <Button
                            size="xs"
                            outlined
                            onClick={() =>
                              void runAction(() =>
                                api.acceptAutonomySuggestion(rule.rule_id, {
                                  destination: "mandate",
                                  expires_in_ms: TEMPORARY_ACCEPT_MS,
                                }),
                              )
                            }
                          >
                            Accept temporary (7d)
                          </Button>
                          <Button
                            size="xs"
                            outlined
                            destructive
                            onClick={() =>
                              void runAction(() =>
                                api.rejectAutonomySuggestion(rule.rule_id, ""),
                              )
                            }
                          >
                            Reject
                          </Button>
                        </div>
                      ) : (
                        <Button
                          size="xs"
                          outlined
                          onClick={() => void handleExplain(rule.rule_id)}
                        >
                          Explain
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {suggestions.length > 0 && (
          <p className="text-xs text-text-secondary">
            Suggestions are shown beside the contract but are excluded from it
            — they never authorize an action until you explicitly accept one.
          </p>
        )}
      </section>

      {/* Explanation drawer */}
      {explanation && (
        <Card>
          <CardContent className="flex flex-col gap-2 py-3 text-sm">
            <div className="flex items-center justify-between">
              <span className="font-mono text-xs">
                {explanation.rule_id} · {explanation.layer} ·{" "}
                {explanation.in_current_contract
                  ? "in current contract"
                  : "NOT in current contract"}
              </span>
              <Button size="xs" ghost onClick={() => setExplanation(null)}>
                Close
              </Button>
            </div>
            <div>
              Effect <Badge tone={effectTone(explanation.effect)}>{explanation.effect}</Badge>{" "}
              — {SOURCE_LABELS[explanation.source]} (confidence{" "}
              {formatConfidence(explanation.confidence_ppm)})
            </div>
            {explanation.conflicts_with.length > 0 && (
              <div>
                Conflicts with:{" "}
                <span className="font-mono text-xs">
                  {explanation.conflicts_with.join(", ")}
                </span>{" "}
                — conflicts resolve conservatively (deny over ask over allow).
              </div>
            )}
            <div className="flex flex-col gap-1">
              <span className="text-xs uppercase text-text-secondary">
                Edit routes
              </span>
              {explanation.edit_route.map((route) => (
                <code key={route} className="text-xs">
                  {route}
                </code>
              ))}
              {explanation.revoke_route.map((route) => (
                <code key={`revoke-${route}`} className="text-xs">
                  {route}
                </code>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      {/* Stable edit: preview then exact-hash apply */}
      <section className="flex flex-col gap-2">
        <h3 className="text-sm font-semibold uppercase tracking-wide">
          Edit stable rules
        </h3>
        <p className="text-xs text-text-secondary">
          Changes are previewed first, then applied only under the exact
          contract hash the preview reported. If authority changes in between,
          the apply is refused and nothing is written.
        </p>
        <textarea
          aria-label="Stable rule change document"
          className="min-h-40 w-full rounded border border-current/20 bg-transparent p-2 font-mono text-xs"
          value={changeText}
          onChange={(e) => setChangeText(e.target.value)}
          spellCheck={false}
        />
        <div>
          <Button onClick={() => void handlePreview()}>Preview change</Button>
        </div>
        {preview && (
          <Card>
            <CardContent className="flex flex-col gap-2 py-3 text-sm">
              <div>Previewed change (not applied):</div>
              {preview.added_rule_ids.length > 0 && (
                <div>added: {preview.added_rule_ids.join(", ")}</div>
              )}
              {preview.changed_rule_ids.length > 0 && (
                <div>changed: {preview.changed_rule_ids.join(", ")}</div>
              )}
              {preview.removed_rule_ids.length > 0 && (
                <div>removed: {preview.removed_rule_ids.join(", ")}</div>
              )}
              {preview.warnings.map((warning) => (
                <div key={warning} className="text-warning">
                  warning: {warning}
                </div>
              ))}
              <div className="font-mono text-xs">
                before hash:{" "}
                <code data-testid="before-contract-hash">
                  {preview.before_contract_hash}
                </code>
              </div>
              <div className="font-mono text-xs">
                after hash: <code>{preview.after_contract_hash}</code>
              </div>
              <div>
                <Button onClick={() => void handleApply()}>
                  Apply exact preview
                </Button>
              </div>
            </CardContent>
          </Card>
        )}
      </section>

      {/* Mandates */}
      <section className="flex flex-col gap-2">
        <h3 className="text-sm font-semibold uppercase tracking-wide">
          Mandates
        </h3>
        {mandates !== null && mandates.length === 0 && (
          <p className="text-sm text-text-secondary">No temporary mandates.</p>
        )}
        {(mandates ?? []).map((mandate) => (
          <div
            key={mandate.rule_id}
            className="flex flex-wrap items-center gap-3 rounded border border-current/10 px-2 py-1.5 text-sm"
          >
            <span className="font-mono text-xs">{mandate.rule_id}</span>
            <Badge tone={effectTone(mandate.effect)}>{mandate.effect}</Badge>
            <span>{mandate.state}</span>
            <span className="text-xs text-text-secondary">
              expires {formatExpiry(mandate.expires_at_ms)}
            </span>
            {mandate.state === "active" && (
              <Button
                size="xs"
                outlined
                destructive
                onClick={() =>
                  void runAction(() =>
                    api.revokeAutonomyMandate(mandate.rule_id, ""),
                  )
                }
              >
                Revoke
              </Button>
            )}
          </div>
        ))}
      </section>

      {/* Audit */}
      <section className="flex flex-col gap-2">
        <h3 className="text-sm font-semibold uppercase tracking-wide">
          Decision audit
        </h3>
        {audit !== null && audit.decisions.length === 0 && (
          <p className="text-sm text-text-secondary">No decisions recorded.</p>
        )}
        {audit !== null && audit.decisions.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-current/20 text-xs uppercase text-text-secondary">
                  <th className="px-2 py-1">When</th>
                  <th className="px-2 py-1">Verdict</th>
                  <th className="px-2 py-1">Code</th>
                  <th className="px-2 py-1">Stage</th>
                  <th className="px-2 py-1">Contract</th>
                </tr>
              </thead>
              <tbody>
                {audit.decisions.map((decision) => (
                  <tr
                    key={decision.decision_id}
                    className="border-b border-current/10"
                  >
                    <td className="px-2 py-1.5 text-xs">
                      {new Date(decision.created_at_ms).toLocaleString()}
                    </td>
                    <td className="px-2 py-1.5">
                      <Badge
                        tone={
                          decision.verdict === "deny"
                            ? "destructive"
                            : decision.verdict === "ask"
                              ? "warning"
                              : "success"
                        }
                      >
                        {decision.verdict}
                      </Badge>
                    </td>
                    <td className="px-2 py-1.5 font-mono text-xs">
                      {decision.code}
                      {(decision.code.startsWith("conflicting_") ||
                        decision.conflicting_rule_ids.length > 0) && (
                        <Badge className="ml-2" tone="warning">
                          Conservative conflict
                        </Badge>
                      )}
                    </td>
                    <td className="px-2 py-1.5">{decision.stage}</td>
                    <td className="px-2 py-1.5 font-mono text-xs">
                      v{decision.authority_version}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="text-xs text-text-secondary">
          Audit rows carry labels, identifiers, and hashes only — never
          message bodies, secrets, or raw recipients. An allow is current
          authority, not proof of completion.
        </p>
      </section>
    </div>
  );
}
