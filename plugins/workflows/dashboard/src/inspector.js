import { NODE_COLORS } from "./canvas-nodes.js";
import { SCALAR_INPUT_KINDS, SUPPORTED_NODES, SUPPORTED_TRIGGERS, changeNodeType } from "./editor-model.js";

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function safeString(value) {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

function jsonBlock(value) {
  try {
    return JSON.stringify(value || {}, null, 2);
  } catch (_) {
    return String(value);
  }
}

function profileRows(options) {
  return asArray(options && options.profiles).filter(function (profile) {
    return profile && profile.name;
  });
}

function providerRows(options) {
  return asArray(options && options.providers).filter(function (provider) {
    return provider && (provider.slug || provider.provider);
  });
}

function specToEditorText(spec) {
  return JSON.stringify(spec || {}, null, 2);
}

function findSpecNode(spec, nodeId) {
  if (!spec || !nodeId) return null;
  if (Array.isArray(spec.nodes)) {
    return spec.nodes.find(function (node) { return String(node && (node.id || node.name)) === String(nodeId); }) || null;
  }
  var node = spec.nodes && spec.nodes[nodeId];
  return node ? Object.assign({}, node, { id: nodeId }) : null;
}

// ponytail: per-field wrapper keeps label on its own line above the control.
// Centralizing it stops inline label/value collisions (the trigger dump bug).
function field(props, label, control, hint) {
  var h = props.createElement;
  var id = props.id || ("wf-field-" + label.replace(/[^a-z0-9]+/gi, "-").toLowerCase());
  var labelledLabel = h("label", { htmlFor: id, key: "l" }, label);
  var fieldChildren = [labelledLabel, control];
  if (hint) fieldChildren.push(h("p", { className: "hermes-workflows-field-hint", key: "h" }, hint));
  return h("div", { className: "hermes-workflows-field", key: id }, labelledLabel, control, hint ? h("p", { className: "hermes-workflows-field-hint", key: "h" }, hint) : null);
}

function fieldRow(props, key, cells) {
  var h = props.createElement;
  return h("div", { className: "hermes-workflows-field-row", key: key }, cells);
}

function sectionPanel(props, key, title, body) {
  var h = props.createElement;
  return h("section", { className: "hermes-workflows-section-panel", key: key },
    h("h4", { className: "hermes-workflows-section-panel-title" }, title),
    body
  );
}

function renderAdvancedNodeJson(props, spec) {
  var h = props.createElement;
  return h("div", { className: "hermes-workflows-stack" },
    h("textarea", {
      className: "hermes-workflows-node-json",
      "aria-label": "Advanced node JSON",
      value: props.nodeJson,
      onChange: function (event) { props.setNodeJson(event.target.value); },
    }),
    h("div", { className: "hermes-workflows-row" },
      h("button", { type: "button", onClick: props.applyNodeJson }, "Apply node JSON"),
      h("button", { type: "button", onClick: props.useJsonDraft, disabled: !spec }, "Use JSON draft")
    )
  );
}

function renderPromptAssistant(props) {
  var h = props.createElement;
  if (!props.promptAssistantOpen) return null;
  return h("div", { className: "hermes-workflows-assistant" },
    h("h4", null, "Prompt assistant"),
    field({ createElement: h, id: "wf-prompt-goal" }, "Workflow goal",
      h("textarea", { id: "wf-prompt-goal", value: props.promptAssistantGoal, onChange: function (event) { props.setPromptAssistantGoal(event.target.value); } })
    ),
    props.promptAssistantAdvanced ? h("div", { className: "hermes-workflows-stack" },
      field({ createElement: h, id: "wf-prompt-objective" }, "Cell objective",
        h("textarea", { id: "wf-prompt-objective", value: props.promptAssistantObjective, onChange: function (event) { props.setPromptAssistantObjective(event.target.value); } })
      ),
      field({ createElement: h, id: "wf-prompt-context" }, "Context placeholders",
        h("textarea", { id: "wf-prompt-context", value: props.promptAssistantContext, onChange: function (event) { props.setPromptAssistantContext(event.target.value); } })
      ),
      field({ createElement: h, id: "wf-prompt-output" }, "Output contract JSON",
        h("textarea", { id: "wf-prompt-output", value: props.promptAssistantOutput, onChange: function (event) { props.setPromptAssistantOutput(event.target.value); } })
      ),
      field({ createElement: h, id: "wf-prompt-constraints" }, "Constraints",
        h("textarea", { id: "wf-prompt-constraints", value: props.promptAssistantConstraints, onChange: function (event) { props.setPromptAssistantConstraints(event.target.value); } })
      ),
      h("button", { type: "button", onClick: function () { props.setPromptAssistantAdvanced(false); }, className: "hermes-workflows-link-btn" }, "Hide advanced fields")
    ) : h("button", { type: "button", onClick: function () { props.setPromptAssistantAdvanced(true); }, className: "hermes-workflows-link-btn" }, "Show advanced fields"),
    h("button", { type: "button", onClick: props.draftPromptWithAssistant, className: "hermes-workflows-primary" }, "Draft prompt")
  );
}

function renderAgentTaskParameters(props) {
  var h = props.createElement;
  var profiles = profileRows(props.agentRoutingOptions);
  var providers = providerRows(props.agentRoutingOptions);
  var selectedProvider = providers.find(function (provider) {
    return String(provider.slug || provider.provider || "") === props.agentProvider;
  });
  var models = asArray(selectedProvider && selectedProvider.models);
  return h("div", { className: "hermes-workflows-stack" },
    profiles.length ? field({ createElement: h, id: "wf-agent-profile" }, "Assigned profile",
      h("select", { id: "wf-agent-profile", value: props.agentProfile, onChange: function (event) { props.setAgentProfile(event.target.value); } },
        [h("option", { key: "", value: "" }, "Choose profile")].concat(profiles.map(function (profile) {
          var label = profile.name + (profile.provider || profile.model ? " · " + safeString(profile.provider) + " / " + safeString(profile.model) : "");
          return h("option", { key: profile.name, value: profile.name }, label);
        }))
      )
    ) : field({ createElement: h, id: "wf-agent-profile" }, "Assigned profile",
      h("input", { id: "wf-agent-profile", value: props.agentProfile, onChange: function (event) { props.setAgentProfile(event.target.value); }, placeholder: "reviewer" })
    ),
    field({ createElement: h, id: "wf-agent-provider" }, "Provider override",
      h("select", {
        id: "wf-agent-provider",
        value: props.agentProvider,
        onChange: function (event) {
          props.setAgentProvider(event.target.value);
          props.setAgentModel("");
        },
      }, [h("option", { key: "", value: "" }, "Use profile default provider")].concat(providers.map(function (provider) {
        var slug = String(provider.slug || provider.provider || "");
        return h("option", { key: slug, value: slug }, safeString(provider.label || slug));
      })))
    ),
    models.length ? field({ createElement: h, id: "wf-agent-model" }, "Model override",
      h("select", {
        id: "wf-agent-model",
        value: props.agentModel,
        onChange: function (event) { props.setAgentModel(event.target.value); },
      }, [h("option", { key: "", value: "" }, "Use profile default model")].concat(models.map(function (model) {
        return h("option", { key: String(model), value: String(model) }, safeString(model));
      })))
    ) : field({ createElement: h, id: "wf-agent-model" }, "Model override",
      h("input", {
        id: "wf-agent-model",
        value: props.agentModel,
        onChange: function (event) { props.setAgentModel(event.target.value); },
        placeholder: props.agentProvider ? "Model name for selected provider" : "Use profile default model",
      })
    ),
    field({ createElement: h, id: "wf-agent-title" }, "Task title",
      h("input", { id: "wf-agent-title", value: props.agentTitle, onChange: function (event) { props.setAgentTitle(event.target.value); }, placeholder: "Review change" })
    ),
    field({ createElement: h, id: "wf-agent-notes" }, "Notes",
      h("textarea", {
        id: "wf-agent-notes",
        className: "hermes-workflows-prompt-editor",
        value: props.promptText,
        onChange: function (event) { props.setPromptText(event.target.value); },
        placeholder: "Optional notes for this cell. Use ${ input.foo } or ${ node.previous.output.bar } for workflow context.",
      }),
      "Use ${ input.foo } or ${ node.previous.output.bar } for workflow context."
    )
  );
}

function renderAgentTaskInputOutput(props) {
  var h = props.createElement;
  return h("div", { className: "hermes-workflows-stack" },
    field({ createElement: h, id: "wf-agent-prompt" }, "Agent cell prompt",
      h("textarea", {
        id: "wf-agent-prompt",
        className: "hermes-workflows-prompt-editor",
        value: props.promptText,
        onChange: function (event) { props.setPromptText(event.target.value); },
        placeholder: "Tell the assigned profile exactly what to do.",
      })
    ),
    field({ createElement: h, id: "wf-agent-result-contract" }, "Result contract JSON",
      h("textarea", {
        id: "wf-agent-result-contract",
        className: "hermes-workflows-contract-editor",
        value: props.resultContractText,
        onChange: function (event) { props.setResultContractText(event.target.value); },
      }),
      "Reference prior outputs with ${ input.foo } or ${ node.previous.output.bar }."
    ),
    h("button", { type: "button", onClick: function () { props.setPromptAssistantOpen(!props.promptAssistantOpen); } }, props.promptAssistantOpen ? "Hide Prompt assistant" : "Prompt assistant"),
    renderPromptAssistant(props)
  );
}

function renderTriggerParameters(props) {
  var h = props.createElement;
  return h("div", { className: "hermes-workflows-stack" },
    field({ createElement: h, id: "wf-trigger-type" }, "Trigger type",
      h("select", {
        id: "wf-trigger-type",
        value: props.cellType || "manual",
        onChange: function (event) { props.setCellType(event.target.value); },
      }, SUPPORTED_TRIGGERS.map(function (type) {
        return h("option", { key: type, value: type }, type);
      }))
    ),
    props.cellType === "schedule" ? field({ createElement: h, id: "wf-trigger-schedule" }, "Schedule (cron)",
      h("input", {
        id: "wf-trigger-schedule",
        value: props.triggerSchedule,
        onChange: function (event) { props.setTriggerSchedule(event.target.value); },
        placeholder: "0 9 * * *",
      })
    ) : null
  );
}

function renderTriggerInputOutput(props) {
  var h = props.createElement;
  var rows = asArray(props.triggerInputRows);
  return h("div", { className: "hermes-workflows-stack" },
    sectionPanel(props, "trigger-input-schema", "Input schema",
      h("div", { className: "hermes-workflows-stack" },
        h("p", { className: "hermes-workflows-muted" }, "Phase 1 supports scalar manual and continuous input items. Batch splitting and document uploads are not supported in this release."),
        rows.length ? h("ul", { className: "hermes-workflows-input-field-list" }, rows.map(function (row) {
          return h("li", { key: row.name, className: "hermes-workflows-input-field-row" },
            h("span", { className: "hermes-workflows-input-field-name" }, safeString(row.name)),
            h("span", { className: "hermes-workflows-badge" }, safeString(row.kind)),
            h("span", { className: "hermes-workflows-meta" }, row.required ? "required" : "optional"),
            h("button", { type: "button", className: "hermes-workflows-link-btn", onClick: function () { props.removeTriggerInputField(row.name); } }, "Remove")
          );
        })) : h("p", { className: "hermes-workflows-muted" }, "No input fields yet. Add fields below, then Apply."),
        h("div", { className: "hermes-workflows-stack" },
          field({ createElement: h, id: "wf-trigger-input-name" }, "Input field name",
            h("input", {
              id: "wf-trigger-input-name",
              value: props.triggerInputName,
              onChange: function (event) { props.setTriggerInputName(event.target.value); },
              placeholder: "repo_path",
            })
          ),
          field({ createElement: h, id: "wf-trigger-input-kind" }, "Input field kind",
            h("select", {
              id: "wf-trigger-input-kind",
              value: props.triggerInputKind,
              onChange: function (event) { props.setTriggerInputKind(event.target.value); },
            }, SCALAR_INPUT_KINDS.map(function (kind) {
              return h("option", { key: kind, value: kind }, kind);
            }))
          ),
          field({ createElement: h, id: "wf-trigger-input-required" }, "Required",
            h("label", { htmlFor: "wf-trigger-input-required-checkbox", className: "hermes-workflows-checkbox-row" },
              h("input", {
                id: "wf-trigger-input-required-checkbox",
                type: "checkbox",
                checked: props.triggerInputRequired,
                onChange: function (event) { props.setTriggerInputRequired(event.target.checked); },
              }),
              "Required input"
            )
          ),
          field({ createElement: h, id: "wf-trigger-input-default" }, "Default value",
            h("input", {
              id: "wf-trigger-input-default",
              value: props.triggerInputDefault,
              onChange: function (event) { props.setTriggerInputDefault(event.target.value); },
              placeholder: "optional",
            })
          ),
          fieldRow(props, "trigger-len", [
            field({ createElement: h, id: "wf-trigger-input-min-length" }, "Min length",
              h("input", {
                id: "wf-trigger-input-min-length",
                type: "number",
                min: "0",
                value: props.triggerInputMinLength,
                onChange: function (event) { props.setTriggerInputMinLength(event.target.value); },
                placeholder: "0",
              })
            ),
            field({ createElement: h, id: "wf-trigger-input-max-length" }, "Max length",
              h("input", {
                id: "wf-trigger-input-max-length",
                type: "number",
                min: "0",
                value: props.triggerInputMaxLength,
                onChange: function (event) { props.setTriggerInputMaxLength(event.target.value); },
                placeholder: "optional",
              })
            ),
          ]),
          h("button", { type: "button", onClick: props.addTriggerInputFieldFromUi }, "Add input field")
        )
      )
    ),
    sectionPanel(props, "trigger-intake-mode", "Intake mode",
      h("div", { className: "hermes-workflows-stack" },
        field({ createElement: h, id: "wf-trigger-intake-mode" }, "Mode",
          h("select", {
            id: "wf-trigger-intake-mode",
            value: props.triggerIntakeMode,
            onChange: function (event) { props.setTriggerIntakeMode(event.target.value); },
          },
            h("option", { value: "single" }, "single"),
            h("option", { value: "continuous" }, "continuous")
          )
        ),
        field({ createElement: h, id: "wf-trigger-dedupe-key" }, "Dedupe key",
          h("input", {
            id: "wf-trigger-dedupe-key",
            value: props.triggerDedupeKey,
            onChange: function (event) { props.setTriggerDedupeKey(event.target.value); },
            placeholder: "$.input.repo_path",
          })
        ),
        field({ createElement: h, id: "wf-trigger-ready-path" }, "Ready when field path",
          h("input", {
            id: "wf-trigger-ready-path",
            value: props.triggerReadyPath,
            onChange: function (event) { props.setTriggerReadyPath(event.target.value); },
            placeholder: "$.input.repo_path",
          })
        )
      )
    )
  );
}

function renderSwitchParameters(props) {
  var h = props.createElement;
  return h("div", { className: "hermes-workflows-stack" },
    field({ createElement: h, id: "wf-switch-default" }, "Default target cell",
      h("input", {
        id: "wf-switch-default",
        value: props.switchDefault,
        onChange: function (event) { props.setSwitchDefault(event.target.value); },
        placeholder: "Optional target id; or connect from switch.default",
      })
    ),
    h("div", { className: "hermes-workflows-stack" },
      h("h4", { className: "hermes-workflows-section-panel-title" }, "Switch cases"),
      props.switchCases.length ? h("ul", { className: "hermes-workflows-switch-case-list" }, props.switchCases.map(function (item, idx) {
        if (!item || !item.name) return null;
        return h("li", { key: item.name + "-" + idx, className: "hermes-workflows-switch-case-row" },
          h("span", { className: "hermes-workflows-switch-case-name" }, safeString(item.name)),
          h("span", { className: "hermes-workflows-meta" }, safeString(item.path || "—") + " == " + safeString(item.equals || "—")),
          h("button", { type: "button", className: "hermes-workflows-link-btn", onClick: function () {
            var next = asArray(props.switchCases).filter(function (_, i) { return i !== idx; });
            props.setSwitchCases(next);
          } }, "Remove")
        );
      })) : h("p", { className: "hermes-workflows-muted" }, "No switch cases yet.")
    ),
    h("div", { className: "hermes-workflows-stack" },
      h("h4", { className: "hermes-workflows-section-panel-title" }, "Add switch case"),
      field({ createElement: h, id: "wf-switch-case-name" }, "Case name",
        h("input", {
          id: "wf-switch-case-name",
          value: props.switchCaseName,
          onChange: function (event) { props.setSwitchCaseName(event.target.value); },
          placeholder: "Case name, e.g. approved",
        })
      ),
      field({ createElement: h, id: "wf-switch-case-path" }, "Path",
        h("input", {
          id: "wf-switch-case-path",
          value: props.switchCasePath,
          onChange: function (event) { props.setSwitchCasePath(event.target.value); },
          placeholder: "$.input.status",
        })
      ),
      field({ createElement: h, id: "wf-switch-case-equals" }, "Equals",
        h("input", {
          id: "wf-switch-case-equals",
          value: props.switchCaseEquals,
          onChange: function (event) { props.setSwitchCaseEquals(event.target.value); },
          placeholder: "Equals value",
        })
      ),
      h("button", { type: "button", onClick: props.addSwitchCaseFromUi, className: "hermes-workflows-primary" }, "Add case")
    )
  );
}

function renderSendMessageParameters(props) {
  var h = props.createElement;
  return h("div", { className: "hermes-workflows-stack" },
    field({ createElement: h, id: "wf-send-platform" }, "Platform",
      h("select", {
        id: "wf-send-platform",
        value: props.sendMessagePlatform || "auto",
        onChange: function (event) { props.setSendMessagePlatform(event.target.value); },
      },
        ["auto", "discord", "telegram", "slack"].map(function (platform) {
          return h("option", { key: platform, value: platform }, platform);
        })
      )
    ),
    field({ createElement: h, id: "wf-send-target" }, "Target (channel/chat ID)",
      h("input", {
        id: "wf-send-target",
        value: props.sendMessageTarget || "",
        onChange: function (event) { props.setSendMessageTarget(event.target.value); },
        placeholder: "channel or chat ID",
      })
    ),
    field({ createElement: h, id: "wf-send-text" }, "Message text",
      h("textarea", {
        id: "wf-send-text",
        className: "hermes-workflows-prompt-editor",
        value: props.sendMessageText || "",
        onChange: function (event) { props.setSendMessageText(event.target.value); },
        placeholder: "Message to send",
      })
    )
  );
}

function renderSendMessageInputOutput(props) {
  var h = props.createElement;
  return h("div", { className: "hermes-workflows-stack" },
    sectionPanel(props, "send-preview", "Send preview",
      h("div", { className: "hermes-workflows-stack" },
        h("p", null, h("strong", null, "Platform: "), safeString(props.sendMessagePlatform || "auto")),
        h("p", null, h("strong", null, "Target: "), safeString(props.sendMessageTarget || "—")),
        h("p", { className: "hermes-workflows-preview" }, safeString(props.sendMessageText || ""))
      )
    )
  );
}

function renderSubworkflowParameters(props) {
  var h = props.createElement;
  return h("div", { className: "hermes-workflows-stack" },
    field({ createElement: h, id: "wf-subworkflow-ref" }, "Workflow reference ID",
      h("input", {
        id: "wf-subworkflow-ref",
        value: props.subworkflowRef || "",
        onChange: function (event) { props.setSubworkflowRef(event.target.value); },
        placeholder: "workflow_id",
      })
    ),
    field({ createElement: h, id: "wf-subworkflow-input-mapping" }, "Input mapping JSON",
      h("textarea", {
        id: "wf-subworkflow-input-mapping",
        className: "hermes-workflows-contract-editor",
        value: props.subworkflowInputMappingText || "{}",
        onChange: function (event) { props.setSubworkflowInputMappingText(event.target.value); },
      })
    )
  );
}

function renderWaitParameters(props) {
  var h = props.createElement;
  return h("div", { className: "hermes-workflows-stack" },
    field({ createElement: h, id: "wf-wait-seconds" }, "Wait seconds",
      h("input", {
        id: "wf-wait-seconds",
        type: "number",
        min: "0",
        value: props.cellSeconds,
        onChange: function (event) { props.setCellSeconds(event.target.value); },
        placeholder: "60",
      })
    )
  );
}

function renderPassFailParameters(props, kind) {
  var h = props.createElement;
  return h("div", { className: "hermes-workflows-stack" },
    field({ createElement: h, id: "wf-cell-output-text" }, kind === "fail" ? "Failure message" : "Output text",
      h("textarea", {
        id: "wf-cell-output-text",
        className: "hermes-workflows-prompt-editor",
        value: props.cellOutputText,
        onChange: function (event) { props.setCellOutputText(event.target.value); },
        placeholder: kind === "fail" ? "Why this workflow should fail." : "Optional output text for this cell.",
      })
    )
  );
}

function renderPassFailInputOutput(props, kind) {
  var h = props.createElement;
  return h("div", { className: "hermes-workflows-stack" },
    sectionPanel(props, "pass-fail-output", "Output preview",
      h("pre", { className: "hermes-workflows-preview" }, safeString(props.cellOutputText || ""))
    )
  );
}

function renderMinimalParameters(props) {
  var h = props.createElement;
  return h("div", { className: "hermes-workflows-stack" },
    h("p", { className: "hermes-workflows-muted" }, "Connect incoming and outgoing edges on the canvas."),
    field({ createElement: h, id: "wf-cell-notes" }, "Notes",
      h("textarea", {
        id: "wf-cell-notes",
        className: "hermes-workflows-prompt-editor",
        value: props.promptText,
        onChange: function (event) { props.setPromptText(event.target.value); },
        placeholder: "Optional notes for this cell.",
      })
    )
  );
}

function renderMinimalInputOutput(props) {
  var h = props.createElement;
  return h("p", { className: "hermes-workflows-muted" }, "This cell passes its inputs through to its outputs.");
}

function renderTypeSelector(props, kind) {
  var h = props.createElement;
  if (props.selectedNode.specKind === "trigger") return null;
  return field({ createElement: h, id: "wf-cell-type" }, h("span", null, "Cell type",
    h("select", {
      id: "wf-cell-type",
      value: props.cellType,
      "aria-label": "Change selected cell type",
      onChange: function (event) {
        var nextType = event.target.value;
        var spec = typeof props.activeSpec === "function" ? props.activeSpec() : props.activeSpec;
        if (!spec || !props.selectedNode) {
          props.setCellType(nextType);
          return;
        }
        var preview = changeNodeType(spec, props.selectedNode.id, nextType);
        if (preview.removedFields.length > 0 && typeof window !== "undefined" && typeof window.confirm === "function") {
          var message = "Changing to " + nextType + " will remove: " + preview.removedFields.join(", ") + ". Continue?";
          if (!window.confirm(message)) return;
        }
        props.setCellType(nextType);
        if (props.setDraftSpec) props.setDraftSpec(preview.spec);
        if (props.updateEditorText) props.updateEditorText(specToEditorText(preview.spec));
        if (props.setSelectedDefinition) props.setSelectedDefinition(Object.assign({}, props.selectedDefinition || {}, { spec: preview.spec }));
        var nextNode = findSpecNode(preview.spec, props.selectedNode.id);
        if (nextNode && props.setSelectedNode) {
          props.setSelectedNode(Object.assign({}, nextNode, { id: props.selectedNode.id, specKind: props.selectedNode.specKind }));
          if (props.setNodeJson) props.setNodeJson(jsonBlock(Object.assign({}, nextNode, { id: props.selectedNode.id })));
        }
        if (props.setNodeMessage) props.setNodeMessage(preview.removedFields.length ? "Changed type to " + nextType + "; removed " + preview.removedFields.join(", ") + "." : "Changed type to " + nextType + ".");
      },
    }, SUPPORTED_NODES.map(function (type) {
      return h("option", { key: type, value: type }, type);
    }))),
    "Apply returns any removed fields of the previous type."
  );
}

function renderParametersTab(props, kind) {
  if (kind === "agent_task") return renderAgentTaskParameters(props);
  if (kind === "trigger") return renderTriggerParameters(props);
  if (kind === "switch") return renderSwitchParameters(props);
  if (kind === "send_message") return renderSendMessageParameters(props);
  if (kind === "subworkflow") return renderSubworkflowParameters(props);
  if (kind === "wait") return renderWaitParameters(props);
  if (kind === "pass" || kind === "fail") return renderPassFailParameters(props, kind);
  return renderMinimalParameters(props);
}

function renderInputOutputTab(props, kind) {
  if (kind === "agent_task") return renderAgentTaskInputOutput(props);
  if (kind === "trigger") return renderTriggerInputOutput(props);
  if (kind === "send_message") return renderSendMessageInputOutput(props);
  if (kind === "pass" || kind === "fail") return renderPassFailInputOutput(props, kind);
  return renderMinimalInputOutput(props);
}

function renderSettingsTab(props, kind) {
  var h = props.createElement;
  var applyHandler = kind === "agent_task" ? props.applyAgentCellForm : props.applyBasicCellForm;
  var spec = typeof props.activeSpec === "function" ? props.activeSpec() : props.activeSpec;
  return h("div", { className: "hermes-workflows-stack" },
    sectionPanel(props, "settings-id-type", "Identity",
      h("div", { className: "hermes-workflows-stack" },
        field({ createElement: h, id: "wf-cell-id" }, "Cell id",
          h("input", {
            id: "wf-cell-id",
            value: props.cellId,
            onChange: function (event) { props.setCellId(event.target.value); },
            placeholder: "cell-id",
          })
        ),
        renderTypeSelector(props, kind),
        h("div", { className: "hermes-workflows-row" },
          h("button", { type: "button", onClick: applyHandler, className: "hermes-workflows-primary" }, "Apply"),
          h("button", { type: "button", onClick: props.deleteSelectedCell, className: "hermes-workflows-danger" }, "Delete")
        )
      )
    ),
    sectionPanel(props, "settings-advanced", "Advanced JSON",
      h("div", { className: "hermes-workflows-stack" },
        h("p", { className: "hermes-workflows-muted" }, "Edit the raw node JSON. Apply merges it into the workflow."),
        h("button", { type: "button", onClick: function () { props.setAdvancedJsonOpen(!props.advancedJsonOpen); } }, props.advancedJsonOpen ? "Hide JSON editor" : "Show JSON editor"),
        props.advancedJsonOpen ? renderAdvancedNodeJson(props, spec) : null
      )
    )
  );
}

function renderTabs(props, activeTab, setActiveTab, kind) {
  var h = props.createElement;
  var tabs = [
    { key: "parameters", label: "Parameters" },
    { key: "input_output", label: "Input & Output" },
    { key: "settings", label: "Settings" },
  ];
  return h("div", { className: "hermes-workflows-tabs", role: "tablist", "aria-label": "Node configuration sections" },
    tabs.map(function (t) {
      return h("button", {
        key: t.key,
        type: "button",
        role: "tab",
        id: "wf-tab-" + t.key,
        "aria-selected": activeTab === t.key ? "true" : "false",
        "aria-controls": "wf-tabpanel-" + t.key,
        className: "hermes-workflows-tab" + (activeTab === t.key ? " is-active" : ""),
        onClick: function () { setActiveTab(t.key); },
      }, t.label);
    })
  );
}

export function renderInspector(props) {
  var h = props.createElement;
  if (!props.selectedNode) {
    return h("aside", { className: "hermes-workflows-inspector" },
      h("h3", null, "Node Configuration"),
      h("div", { className: "hermes-workflows-inspector-empty" },
        h("p", null, "Select a node from the canvas to configure it."),
        h("p", { className: "hermes-workflows-muted" }, "Tip: press Add Node (N) to add a new cell from the rail.")
      )
    );
  }

  var spec = typeof props.activeSpec === "function" ? props.activeSpec() : props.activeSpec;
  var kind = props.selectedNode.specKind === "trigger" ? "trigger" : (props.selectedNode.type || props.cellType || "pass");

  var activeTab = props.activeInspectorTab || "parameters";
  var setActiveTab = typeof props.setActiveInspectorTab === "function" ? props.setActiveInspectorTab : function () {};
  var closeInspector = typeof props.closeInspector === "function" ? props.closeInspector : function () {};

  var panelByTab = {
    parameters: { id: "wf-tabpanel-parameters", body: renderParametersTab(props, kind) },
    input_output: { id: "wf-tabpanel-input_output", body: renderInputOutputTab(props, kind) },
    settings: { id: "wf-tabpanel-settings", body: renderSettingsTab(props, kind) },
  };

  return h("aside", { className: "hermes-workflows-inspector" },
    h("div", { className: "hermes-workflows-inspector-sticky-header" },
      h("div", { className: "hermes-workflows-inspector-heading-row" },
        h("h3", { className: "hermes-workflows-inspector-heading" }, "Node Configuration"),
        h("button", {
          type: "button",
          "aria-label": "Close configuration",
          className: "hermes-workflows-link-btn",
          onClick: closeInspector,
        }, "Close")
      ),
      h("div", { className: "hermes-workflows-inspector-meta-row" },
        h("span", { className: "hermes-workflows-muted" }, safeString(props.selectedNode.id)),
        h("span", { className: "hermes-workflows-type-badge", style: { backgroundColor: NODE_COLORS[kind] || "#64748b" } }, kind)
      ),
      props.nodeMessage ? h("p", { className: "hermes-workflows-node-message", role: "status", "aria-live": "polite" }, props.nodeMessage) : null
    ),
    renderTabs(props, activeTab, setActiveTab, kind),
    Object.keys(panelByTab).map(function (tabKey) {
      var panel = panelByTab[tabKey];
      if (activeTab !== tabKey) return null;
      return h("section", {
        key: tabKey,
        role: "tabpanel",
        id: panel.id,
        "aria-labelledby": "wf-tab-" + tabKey,
        className: "hermes-workflows-tabpanel",
      }, panel.body);
    })
  );
}