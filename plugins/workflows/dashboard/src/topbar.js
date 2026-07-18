export function renderTopBar({
  createElement: h,
  activeSpec,
  selectedDefinition,
  workflowIdForDefinition,
  versionForDefinition,
  renderWorkspaceTabs,
  validateDefinition,
  validating,
  deployDefinition,
  deploying,
  deleteWorkflow,
  deleting,
  openRunPanel,
  openNodePalette,
  running,
  refresh,
  loading,
  showAdvancedYaml,
  setShowAdvancedYaml,
  persistedRunCapable,
}) {
  const workflowName = activeSpec
    ? String(activeSpec.name || activeSpec.id || activeSpec.workflow_id || "Untitled workflow")
    : "Untitled workflow";
  const hasDraft = !!activeSpec;
  const workflowId = selectedDefinition && workflowIdForDefinition(selectedDefinition);
  const version = selectedDefinition && versionForDefinition(selectedDefinition);
  const persisted = !!(workflowId && version);
  const runCapable = typeof persistedRunCapable === "function" ? persistedRunCapable() : persisted;

  return h("div", { className: "hermes-workflows-topbar" },
    h("div", { className: "hermes-workflows-topbar-left" },
      h("span", { className: "hermes-workflows-topbar-name" }, workflowName),
      h("span", { className: "hermes-workflows-topbar-status" }, persisted ? "v" + version + " · enabled" : "draft")
    ),
    renderWorkspaceTabs ? renderWorkspaceTabs() : null,
    h("div", { className: "hermes-workflows-topbar-actions" },
      h("button", { type: "button", "aria-label": "Add Node", onClick: function () { if (typeof openNodePalette === "function") openNodePalette(); } }, "Add Node"),
      h("button", { type: "button", onClick: validateDefinition, disabled: validating || !hasDraft }, "Validate"),
      h("button", { type: "button", onClick: deployDefinition, disabled: deploying || !hasDraft, className: "hermes-workflows-primary" }, "Deploy"),
      persisted ? h("button", { type: "button", onClick: deleteWorkflow, disabled: deleting }, "Delete") : null,
      persisted ? h("button", { type: "button", onClick: openRunPanel, disabled: running || !runCapable }, "Run") : null,
      h("button", { type: "button", onClick: refresh, disabled: loading }, "Refresh"),
      h("button", { type: "button", onClick: () => setShowAdvancedYaml(!showAdvancedYaml) }, "YAML")
    )
  );
}
