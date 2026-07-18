import { feedActions as defaultFeedActions } from "./run.js";

const INTAKE_SCOPE_NOTE = "Phase 1 supports scalar manual and continuous input items. Batch splitting and document uploads are not supported in this release.";

function safeString(value) {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

function jsonBlock(value) {
  try {
    return JSON.stringify(value || {}, null, 2);
  } catch (_) {
    return "{}";
  }
}

function workflowIdForDefinition(definition) {
  return definition && String(definition.workflow_id || definition.id || "");
}

function versionForDefinition(definition) {
  return definition && definition.version !== undefined && definition.version !== null ? String(definition.version) : "";
}

function getCreateElement(props) {
  return props.createElement || (props.React && props.React.createElement);
}

export function renderRunInputField(field, props) {
  const options = props || {};
  const h = getCreateElement(options);
  const inputFieldValues = options.inputFieldValues || {};
  const runFieldErrors = options.runFieldErrors || {};
  const name = safeString(field && field.name);
  const kind = safeString((field && field.kind) || "text");
  const value = inputFieldValues[name] === undefined || inputFieldValues[name] === null ? "" : inputFieldValues[name];
  const fieldError = runFieldErrors[name] || "";
  function updateValue(event) {
    const next = Object.assign({}, inputFieldValues);
    next[name] = event.target.value;
    options.setInputFieldValues(next);
  }
  return h("label", { key: name, className: "hermes-workflows-run-field" },
    h("span", null, (field && field.label ? safeString(field.label) : name) + (field && field.required ? " *" : "")),
    kind === "boolean" ? h("select", { value: value, onChange: updateValue },
      h("option", { value: "" }, "Not set"),
      h("option", { value: "true" }, "true"),
      h("option", { value: "false" }, "false")
    ) : (kind === "json" || kind === "long_text" || kind === "prompt" || kind === "criteria" || kind === "document") ? h("textarea", {
      value: value,
      onChange: updateValue,
      placeholder: kind === "document" ? "Paste document text" : kind,
      rows: kind === "document" || kind === "prompt" || kind === "criteria" ? 5 : 3,
    }) : h("input", {
      type: kind === "number" || kind === "integer" ? "number" : (kind === "url" ? "url" : "text"),
      step: kind === "integer" ? "1" : "any",
      value: value,
      onChange: updateValue,
      placeholder: kind,
    }),
    fieldError ? h("span", { className: "hermes-workflows-field-error", role: "alert" }, fieldError) : null
  );
}

export function renderFeedInputField(field, props) {
  const options = props || {};
  const h = getCreateElement(options);
  const feedInputValues = options.feedInputValues || {};
  const name = safeString(field && field.name);
  const kind = safeString((field && field.kind) || "text");
  const label = safeString((field && field.label) || name);
  const value = feedInputValues[name] === undefined || feedInputValues[name] === null ? "" : feedInputValues[name];
  const disabled = !!(field && field.disabled);
  function updateValue(event) {
    const next = Object.assign({}, feedInputValues);
    next[name] = event.target.value;
    options.setFeedInputValues(next);
  }
  const hint = field && field.description ? h("span", { className: "hermes-workflows-muted" }, safeString(field.description)) : null;
  return h("label", { key: name, className: "hermes-workflows-run-field" },
    h("span", null, label + (field && field.required ? " *" : "")),
    kind === "boolean" ? h("select", { value: value, disabled: disabled, onChange: updateValue },
      h("option", { value: "" }, "Not set"),
      h("option", { value: "true" }, "true"),
      h("option", { value: "false" }, "false")
    ) : (kind === "json" || kind === "long_text" || kind === "prompt" || kind === "criteria" || kind === "document") ? h("textarea", {
      value: value,
      disabled: disabled,
      onChange: updateValue,
      placeholder: kind,
      rows: kind === "document" || kind === "prompt" || kind === "criteria" ? 5 : 3,
    }) : h("input", {
      type: kind === "number" || kind === "integer" ? "number" : (kind === "url" ? "url" : "text"),
      step: kind === "integer" ? "1" : "any",
      value: value,
      disabled: disabled,
      onChange: updateValue,
      placeholder: kind,
    }),
    hint
  );
}

export function renderInputFeedPanel(props) {
  const options = props || {};
  const h = getCreateElement(options);
  const selectedDefinition = options.selectedDefinition;
  if (!selectedDefinition) return null;
  const spec = selectedDefinition.spec || null;
  const workflowId = options.workflowIdForDefinition(selectedDefinition);
  const trigger = options.selectedInputTrigger(spec);
  const fields = options.inputFieldsForSpec(spec, trigger);
  const inputFeeds = options.inputFeeds || [];
  const selectedFeed = inputFeeds.find(function (feed) { return feed.feed_id === options.selectedFeedId; }) || inputFeeds[0] || null;
  const feedId = selectedFeed && selectedFeed.feed_id;
  const feedOpen = selectedFeed && selectedFeed.status === "open";
  const actions = (options.feedActions || defaultFeedActions)(selectedFeed && selectedFeed.status);
  function handleAction(action) {
    if (action === "start-new") { options.openContinuousFeed(); }
    else { options.setSelectedFeedStatus(action === "pause" ? "paused" : action === "resume" ? "open" : action === "close" ? "closed" : action); }
  }
  return h("div", { className: "hermes-workflows-input-feed-panel" },
    h("div", { className: "hermes-workflows-item-title" },
      h("strong", null, "Continuous input feed"),
      selectedFeed ? h("span", { className: "hermes-workflows-badge" }, safeString(selectedFeed.status)) : h("span", { className: "hermes-workflows-badge" }, "not open")
    ),
    h("p", { className: "hermes-workflows-muted" }, "Open a feed, then add scalar repo paths, prompts, or criteria. Ready items launch normal executions as the dispatcher ticks."),
    h("p", { className: "hermes-workflows-muted" }, INTAKE_SCOPE_NOTE),
    selectedFeed ? h("div", { className: "hermes-workflows-muted", style: {fontSize: "0.76rem"} }, "Version: " + safeString(selectedFeed.version) + " · Updated: " + safeString(selectedFeed.updated_at)) : null,
    h("div", { className: "hermes-workflows-row" },
      !selectedFeed ? h("button", { type: "button", disabled: options.feedBusy || !workflowId, onClick: options.openContinuousFeed, className: "hermes-workflows-primary" }, options.feedBusy ? "Opening…" : "Open Continuous Feed") : null,
      inputFeeds.length ? h("select", { value: options.selectedFeedId, onChange: function (event) { const id = event.target.value; options.setSelectedFeedId(id); options.loadInputFeedItems(id); } }, inputFeeds.map(function (feed) {
        return h("option", { key: feed.feed_id, value: feed.feed_id }, safeString(feed.status) + " · " + safeString(feed.feed_id).slice(0, 12));
      })) : null,
      actions.map(function (action) {
        const label = action === "start-new" ? "Start New Feed" : action === "pause" ? "Pause" : action === "resume" ? "Resume" : action === "close" ? "Close" : action;
        return h("button", { key: action, type: "button", disabled: options.feedBusy, onClick: function () { handleAction(action); } }, label);
      }),
      feedId ? h("button", { type: "button", disabled: options.feedBusy, onClick: function () { options.loadInputFeedItems(feedId); } }, "Refresh Feed Items") : null
    ),
    feedId ? h("section", { className: "hermes-workflows-feed-items", "aria-label": "Input feed items" },
      h("div", { className: "hermes-workflows-item-title" },
        h("strong", null, "Feed items"),
        h("span", { className: "hermes-workflows-badge" }, String((options.inputFeedItems || []).length))
      ),
      (options.inputFeedItems || []).length ? options.inputFeedItems.map(function (item) {
        const itemStatus = safeString(item.status);
        const canUpdate = feedOpen && ["needs_input", "queued"].indexOf(itemStatus) !== -1;
        return h("div", { key: item.item_id, className: "hermes-workflows-feed-item" },
          h("div", { className: "hermes-workflows-item-title" },
            h("strong", null, safeString(item.item_id).slice(0, 18)),
            h("span", { className: "hermes-workflows-badge" }, itemStatus)
          ),
          h("pre", { className: "hermes-workflows-pre" }, jsonBlock(item.input || {})),
          h("button", { type: "button", disabled: options.feedBusy || !canUpdate, onClick: function () { options.updateInputFeedItem(item); } }, canUpdate ? "Update Item From JSON" : "Item Not Editable")
        );
      }) : h("p", { className: "hermes-workflows-muted" }, "No feed items yet.")
    ) : null,
    feedId ? h("form", { className: "hermes-workflows-stack", onSubmit: options.addItemToFeed },
      !feedOpen ? h("p", { className: "hermes-workflows-muted" }, "This feed is " + safeString(selectedFeed.status) + "; resume it before adding items.") : null,
      fields.length && !options.showAdvancedFeedInputJson ? h("div", { className: "hermes-workflows-run-fields" }, fields.map(function (field) { return renderFeedInputField(Object.assign({}, field, { disabled: !feedOpen }), options); })) : null,
      h("label", { className: "hermes-workflows-run-advanced-toggle" },
        h("input", { type: "checkbox", checked: options.showAdvancedFeedInputJson, disabled: !feedOpen, onChange: function (event) { options.setShowAdvancedFeedInputJson(event.target.checked); } }),
        h("span", null, "Use advanced JSON input")
      ),
      options.showAdvancedFeedInputJson ? h("textarea", { value: options.feedInputText, disabled: !feedOpen, onChange: function (event) { options.setFeedInputText(event.target.value); }, rows: 8, "aria-label": "Input feed item JSON" }) : null,
      h("button", { type: "submit", className: "hermes-workflows-primary", disabled: options.feedBusy || !feedOpen }, options.feedBusy ? "Adding…" : "Add Item To Feed")
    ) : null
  );
}

export function renderRunStartPanel(props) {
  const options = props || {};
  const h = getCreateElement(options);
  if (!options.runPanelOpen) return null;
  const runInputSpec = typeof options.runInputSpec === "function" ? options.runInputSpec() : null;
  const fields = typeof options.inputFieldsForSpec === "function" ? options.inputFieldsForSpec(runInputSpec) : [];
  const workflowId = (options.runWorkflowId || "").trim();
  const definitions = options.definitions || [];
  const publishedVersions = definitions.filter(function (d) { return workflowIdForDefinition(d) === workflowId && versionForDefinition(d); });
  return h("div", { ref: options.runPanelRef, className: "hermes-workflows-run-overlay", role: "dialog", "aria-modal": "true", "aria-label": "Start workflow run" },
    h("form", { className: "hermes-workflows-run-panel", onSubmit: options.runWorkflow },
      h("div", { className: "hermes-workflows-run-panel-header" },
        h("div", null,
          h("h3", null, "Start Workflow Run"),
          h("p", { className: "hermes-workflows-muted" }, fields.length ? "Provide the manual trigger input for this execution." : "No start input fields are configured for this workflow. Running will use empty input.")
        ),
        h("button", { type: "button", className: "hermes-workflows-link-button", onClick: function () { options.setRunPanelOpen(false); } }, "Close")
      ),
      publishedVersions.length > 1 ? h("label", { className: "hermes-workflows-run-field" },
        h("span", null, "Version"),
        h("select", {
          value: options.selectedRunVersion(workflowId) || "",
          onChange: function (event) {
            const v = parseInt(event.target.value, 10);
            if (!isNaN(v) && options.loadDefinition) options.loadDefinition(workflowId, v).catch(options.fail);
          },
        }, publishedVersions.map(function (d) {
          return h("option", { key: d.version, value: d.version }, "v" + safeString(d.version));
        }))
      ) : null,
      fields.length && !options.showAdvancedInputJson ? h("div", { className: "hermes-workflows-run-fields" }, fields.map(function (field) { return renderRunInputField(field, options); })) : null,
      h("label", { className: "hermes-workflows-run-advanced-toggle" },
        h("input", { type: "checkbox", checked: options.showAdvancedInputJson, onChange: function (event) { options.setShowAdvancedInputJson(event.target.checked); } }),
        h("span", null, "Use advanced JSON input")
      ),
      options.showAdvancedInputJson ? h("textarea", {
        value: options.runInputText,
        onChange: function (event) { options.setRunInputText(event.target.value); },
        rows: 8,
        "aria-label": "Workflow input JSON",
      }) : null,
      h("div", { className: "hermes-workflows-run-actions" },
        h("button", { type: "button", onClick: function () { options.setRunPanelOpen(false); } }, "Cancel"),
        h("button", { type: "submit", className: "hermes-workflows-primary", disabled: options.running }, options.running ? "Running…" : "Start Run")
      )
    )
  );
}

export function renderBottomPanel(props) {
  const options = props || {};
  const h = getCreateElement(options);
  return h("div", { className: "hermes-workflows-bottom-panel" + (options.bottomCollapsed ? " is-collapsed" : "") },
    h("div", { className: "hermes-workflows-bottom-tabs" },
      h("button", {
        type: "button",
        className: "hermes-workflows-bottom-tab is-active",
        role: "tab",
        "aria-selected": "true",
      }, "Validation"),
      h("button", { type: "button", className: "hermes-workflows-bottom-toggle", "aria-expanded": options.bottomCollapsed ? "false" : "true", "aria-controls": "hermes-workflows-bottom-content", onClick: function () { options.setBottomCollapsed(!options.bottomCollapsed); } }, options.bottomCollapsed ? "▴ Expand" : "▾ Collapse")
    ),
    h("div", { id: "hermes-workflows-bottom-content", className: "hermes-workflows-bottom-content" },
      options.bottomCollapsed ? null : (typeof options.renderValidationChecklist === "function" ? options.renderValidationChecklist({ activeSpec: options.activeSpec, workflowCapabilities: options.workflowCapabilities }) : null)
    )
  );
}

export function renderDiagnosticsPanel(props) {
  const options = props || {};
  const h = getCreateElement(options);
  return h("section", { className: "hermes-workflows-diagnostics" },
    h("button", {
      type: "button",
      className: "hermes-workflows-diagnostics-toggle",
      "aria-expanded": options.diagnosticsOpen ? "true" : "false",
      "aria-controls": "hermes-workflows-diagnostics-body",
      onClick: function () { options.setDiagnosticsOpen(!options.diagnosticsOpen); },
    }, options.diagnosticsOpen ? "Hide diagnostics" : "Show diagnostics"),
    options.diagnosticsOpen ? h("div", { id: "hermes-workflows-diagnostics-body", className: "hermes-workflows-diagnostics-body" },
      h("p", { className: "hermes-workflows-muted" }, "Manual advance for queued workflows and dispatcher status."),
      typeof options.renderExecutionStallWarning === "function" ? options.renderExecutionStallWarning() : null,
      h("div", { className: "hermes-workflows-row" },
        h("button", { type: "button", disabled: options.ticking, onClick: options.manualTick }, options.ticking ? "Ticking…" : "Manual Tick"),
        h("button", { type: "button", disabled: options.loading, onClick: function () { options.refresh(); } }, options.loading ? "Refreshing…" : "Refresh")
      )
    ) : null
  );
}

export function renderHistoryMode(props) {
  const options = props || {};
  const h = getCreateElement(options);
  const executions = options.executions || [];
  return h("section", {
    id: "hermes-workflows-mode-history",
    role: "tabpanel",
    "aria-label": "Workflow execution history",
    className: "hermes-workflows-history-mode",
  },
    h("div", { className: "hermes-workflows-history-toolbar" },
      h("button", { type: "button", disabled: options.loading, onClick: function () { options.refresh(); } }, options.loading ? "Refreshing…" : "Refresh executions")
    ),
    h("div", { className: "hermes-workflows-history-list" },
      executions.length ? executions.slice(0, 50).map(function (execution) {
        const eid = safeString(execution.execution_id || execution.id);
        const execStatus = safeString(execution.status);
        const statusClass = execStatus === "succeeded" ? " is-succeeded" : execStatus === "failed" ? " is-failed" : "";
        const isActive = options.selectedExecution && String(options.selectedExecution.execution_id || options.selectedExecution.id || "") === eid;
        return h("button", {
          key: eid,
          type: "button",
          "data-execution-id": eid,
          className: "hermes-workflows-history-row" + (isActive ? " is-selected" : ""),
          onClick: function () {
            options.loadExecution(eid).catch(options.fail);
            options.pushMode("history", { execution: eid });
          },
        },
          h("span", { className: "hermes-workflows-history-row-id" }, eid.slice(0, 16)),
          h("span", { className: "hermes-workflows-history-row-status" + statusClass }, execStatus)
        );
      }) : h("p", { className: "hermes-workflows-muted" }, "No executions yet.")
    ),
    h("div", { className: "hermes-workflows-history-detail" },
      typeof options.renderTimeline === "function" ? options.renderTimeline() : null
    )
  );
}
