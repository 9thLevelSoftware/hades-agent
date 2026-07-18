(function() {
	//#region ../plugins/workflows/dashboard/src/graph.js
	function graphItems(spec) {
		const triggers = (spec?.triggers || []).map((trigger, index) => ({
			id: trigger.id || trigger.name || `trigger_${index + 1}`,
			rendererType: "trigger",
			specKind: "trigger",
			triggerType: trigger.type,
			spec: trigger
		}));
		const nodes = Object.entries(spec?.nodes || {}).map(([id, node]) => ({
			id,
			rendererType: node.type,
			specKind: "node",
			triggerType: null,
			spec: node
		}));
		return [...triggers, ...nodes];
	}
	function decorateGraphItems(items, statuses) {
		return items.map((item) => ({
			...item,
			status: statuses[item.id] || "idle"
		}));
	}
	//#endregion
	//#region ../plugins/workflows/dashboard/src/api.js
	function formatApiError(error) {
		if (error instanceof Error && typeof error.message === "string") return formatApiError(error.message);
		if (error && typeof error === "object") {
			if (typeof error.message === "string" && error.message) return error.message;
			if (typeof error.detail === "string" && error.detail) return formatApiError(error.detail);
		}
		if (typeof error === "string") {
			const jsonStart = error.indexOf("{");
			if (jsonStart !== -1) try {
				return formatApiError(JSON.parse(error.slice(jsonStart)));
			} catch {}
			return error;
		}
		return "Unknown error";
	}
	function createApi(fetchJSON, basePath) {
		if (typeof fetchJSON !== "function") throw new Error("createApi requires the SDK fetchJSON implementation");
		const base = String(basePath || "").replace(/\/$/, "");
		return function api(path, options) {
			return fetchJSON(base + String(path || ""), options);
		};
	}
	//#endregion
	//#region ../plugins/workflows/dashboard/src/build.js
	function isDraftDirty(state) {
		return JSON.stringify(state.savedDraft) !== JSON.stringify(state.workingDraft);
	}
	function buildApiHelpers(api) {
		return {
			putDraft: function(workflowId, body) {
				return api("/definitions/" + encodeURIComponent(workflowId) + "/draft", {
					method: "PUT",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify(body)
				});
			},
			getDraft: function(workflowId) {
				return api("/definitions/" + encodeURIComponent(workflowId) + "/draft", { method: "GET" });
			},
			deleteDraft: function(workflowId) {
				return api("/definitions/" + encodeURIComponent(workflowId) + "/draft", { method: "DELETE" });
			},
			publish: function(workflowId, body) {
				return api("/definitions/" + encodeURIComponent(workflowId) + "/publish", {
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify(body)
				});
			}
		};
	}
	//#endregion
	//#region ../plugins/workflows/dashboard/src/run.js
	var ACTIONS_BY_STATUS = {
		open: ["pause", "close"],
		paused: ["resume", "close"],
		closed: ["start-new"]
	};
	var ACTIVE_STATUSES = /* @__PURE__ */ new Set([
		"queued",
		"running",
		"needs_input"
	]);
	function feedActions(status) {
		return ACTIONS_BY_STATUS[status] || [];
	}
	function shouldPollFeed(items) {
		if (!Array.isArray(items) || !items.length) return false;
		return items.some(function(item) {
			return ACTIVE_STATUSES.has(item.status);
		});
	}
	function fieldErrors(error) {
		if (error && typeof error === "object" && !Array.isArray(error)) {
			const detail = error.detail;
			if (detail && typeof detail === "object" && !Array.isArray(detail) && detail.field_errors && typeof detail.field_errors === "object") return detail.field_errors;
		}
		return {};
	}
	//#endregion
	//#region ../plugins/workflows/dashboard/src/panels.js
	var INTAKE_SCOPE_NOTE = "Phase 1 supports scalar manual and continuous input items. Batch splitting and document uploads are not supported in this release.";
	function safeString$3(value) {
		if (value === null || value === void 0 || value === "") return "—";
		return String(value);
	}
	function jsonBlock$1(value) {
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
		return definition && definition.version !== void 0 && definition.version !== null ? String(definition.version) : "";
	}
	function getCreateElement(props) {
		return props.createElement || props.React && props.React.createElement;
	}
	function renderRunInputField(field, props) {
		const options = props || {};
		const h = getCreateElement(options);
		const inputFieldValues = options.inputFieldValues || {};
		const runFieldErrors = options.runFieldErrors || {};
		const name = safeString$3(field && field.name);
		const kind = safeString$3(field && field.kind || "text");
		const value = inputFieldValues[name] === void 0 || inputFieldValues[name] === null ? "" : inputFieldValues[name];
		const fieldError = runFieldErrors[name] || "";
		function updateValue(event) {
			const next = Object.assign({}, inputFieldValues);
			next[name] = event.target.value;
			options.setInputFieldValues(next);
		}
		return h("label", {
			key: name,
			className: "hermes-workflows-run-field"
		}, h("span", null, (field && field.label ? safeString$3(field.label) : name) + (field && field.required ? " *" : "")), kind === "boolean" ? h("select", {
			value,
			onChange: updateValue
		}, h("option", { value: "" }, "Not set"), h("option", { value: "true" }, "true"), h("option", { value: "false" }, "false")) : kind === "json" || kind === "long_text" || kind === "prompt" || kind === "criteria" || kind === "document" ? h("textarea", {
			value,
			onChange: updateValue,
			placeholder: kind === "document" ? "Paste document text" : kind,
			rows: kind === "document" || kind === "prompt" || kind === "criteria" ? 5 : 3
		}) : h("input", {
			type: kind === "number" || kind === "integer" ? "number" : kind === "url" ? "url" : "text",
			step: kind === "integer" ? "1" : "any",
			value,
			onChange: updateValue,
			placeholder: kind
		}), fieldError ? h("span", {
			className: "hermes-workflows-field-error",
			role: "alert"
		}, fieldError) : null);
	}
	function renderFeedInputField(field, props) {
		const options = props || {};
		const h = getCreateElement(options);
		const feedInputValues = options.feedInputValues || {};
		const name = safeString$3(field && field.name);
		const kind = safeString$3(field && field.kind || "text");
		const label = safeString$3(field && field.label || name);
		const value = feedInputValues[name] === void 0 || feedInputValues[name] === null ? "" : feedInputValues[name];
		const disabled = !!(field && field.disabled);
		function updateValue(event) {
			const next = Object.assign({}, feedInputValues);
			next[name] = event.target.value;
			options.setFeedInputValues(next);
		}
		const hint = field && field.description ? h("span", { className: "hermes-workflows-muted" }, safeString$3(field.description)) : null;
		return h("label", {
			key: name,
			className: "hermes-workflows-run-field"
		}, h("span", null, label + (field && field.required ? " *" : "")), kind === "boolean" ? h("select", {
			value,
			disabled,
			onChange: updateValue
		}, h("option", { value: "" }, "Not set"), h("option", { value: "true" }, "true"), h("option", { value: "false" }, "false")) : kind === "json" || kind === "long_text" || kind === "prompt" || kind === "criteria" || kind === "document" ? h("textarea", {
			value,
			disabled,
			onChange: updateValue,
			placeholder: kind,
			rows: kind === "document" || kind === "prompt" || kind === "criteria" ? 5 : 3
		}) : h("input", {
			type: kind === "number" || kind === "integer" ? "number" : kind === "url" ? "url" : "text",
			step: kind === "integer" ? "1" : "any",
			value,
			disabled,
			onChange: updateValue,
			placeholder: kind
		}), hint);
	}
	function renderInputFeedPanel(props) {
		const options = props || {};
		const h = getCreateElement(options);
		const selectedDefinition = options.selectedDefinition;
		if (!selectedDefinition) return null;
		const spec = selectedDefinition.spec || null;
		const workflowId = options.workflowIdForDefinition(selectedDefinition);
		const trigger = options.selectedInputTrigger(spec);
		const fields = options.inputFieldsForSpec(spec, trigger);
		const inputFeeds = options.inputFeeds || [];
		const selectedFeed = inputFeeds.find(function(feed) {
			return feed.feed_id === options.selectedFeedId;
		}) || inputFeeds[0] || null;
		const feedId = selectedFeed && selectedFeed.feed_id;
		const feedOpen = selectedFeed && selectedFeed.status === "open";
		const actions = (options.feedActions || feedActions)(selectedFeed && selectedFeed.status);
		function handleAction(action) {
			if (action === "start-new") options.openContinuousFeed();
			else options.setSelectedFeedStatus(action === "pause" ? "paused" : action === "resume" ? "open" : action === "close" ? "closed" : action);
		}
		return h("div", { className: "hermes-workflows-input-feed-panel" }, h("div", { className: "hermes-workflows-item-title" }, h("strong", null, "Continuous input feed"), selectedFeed ? h("span", { className: "hermes-workflows-badge" }, safeString$3(selectedFeed.status)) : h("span", { className: "hermes-workflows-badge" }, "not open")), h("p", { className: "hermes-workflows-muted" }, "Open a feed, then add scalar repo paths, prompts, or criteria. Ready items launch normal executions as the dispatcher ticks."), h("p", { className: "hermes-workflows-muted" }, INTAKE_SCOPE_NOTE), selectedFeed ? h("div", {
			className: "hermes-workflows-muted",
			style: { fontSize: "0.76rem" }
		}, "Version: " + safeString$3(selectedFeed.version) + " · Updated: " + safeString$3(selectedFeed.updated_at)) : null, h("div", { className: "hermes-workflows-row" }, !selectedFeed ? h("button", {
			type: "button",
			disabled: options.feedBusy || !workflowId,
			onClick: options.openContinuousFeed,
			className: "hermes-workflows-primary"
		}, options.feedBusy ? "Opening…" : "Open Continuous Feed") : null, inputFeeds.length ? h("select", {
			value: options.selectedFeedId,
			onChange: function(event) {
				const id = event.target.value;
				options.setSelectedFeedId(id);
				options.loadInputFeedItems(id);
			}
		}, inputFeeds.map(function(feed) {
			return h("option", {
				key: feed.feed_id,
				value: feed.feed_id
			}, safeString$3(feed.status) + " · " + safeString$3(feed.feed_id).slice(0, 12));
		})) : null, actions.map(function(action) {
			const label = action === "start-new" ? "Start New Feed" : action === "pause" ? "Pause" : action === "resume" ? "Resume" : action === "close" ? "Close" : action;
			return h("button", {
				key: action,
				type: "button",
				disabled: options.feedBusy,
				onClick: function() {
					handleAction(action);
				}
			}, label);
		}), feedId ? h("button", {
			type: "button",
			disabled: options.feedBusy,
			onClick: function() {
				options.loadInputFeedItems(feedId);
			}
		}, "Refresh Feed Items") : null), feedId ? h("section", {
			className: "hermes-workflows-feed-items",
			"aria-label": "Input feed items"
		}, h("div", { className: "hermes-workflows-item-title" }, h("strong", null, "Feed items"), h("span", { className: "hermes-workflows-badge" }, String((options.inputFeedItems || []).length))), (options.inputFeedItems || []).length ? options.inputFeedItems.map(function(item) {
			const itemStatus = safeString$3(item.status);
			const canUpdate = feedOpen && ["needs_input", "queued"].indexOf(itemStatus) !== -1;
			return h("div", {
				key: item.item_id,
				className: "hermes-workflows-feed-item"
			}, h("div", { className: "hermes-workflows-item-title" }, h("strong", null, safeString$3(item.item_id).slice(0, 18)), h("span", { className: "hermes-workflows-badge" }, itemStatus)), h("pre", { className: "hermes-workflows-pre" }, jsonBlock$1(item.input || {})), h("button", {
				type: "button",
				disabled: options.feedBusy || !canUpdate,
				onClick: function() {
					options.updateInputFeedItem(item);
				}
			}, canUpdate ? "Update Item From JSON" : "Item Not Editable"));
		}) : h("p", { className: "hermes-workflows-muted" }, "No feed items yet.")) : null, feedId ? h("form", {
			className: "hermes-workflows-stack",
			onSubmit: options.addItemToFeed
		}, !feedOpen ? h("p", { className: "hermes-workflows-muted" }, "This feed is " + safeString$3(selectedFeed.status) + "; resume it before adding items.") : null, fields.length && !options.showAdvancedFeedInputJson ? h("div", { className: "hermes-workflows-run-fields" }, fields.map(function(field) {
			return renderFeedInputField(Object.assign({}, field, { disabled: !feedOpen }), options);
		})) : null, h("label", { className: "hermes-workflows-run-advanced-toggle" }, h("input", {
			type: "checkbox",
			checked: options.showAdvancedFeedInputJson,
			disabled: !feedOpen,
			onChange: function(event) {
				options.setShowAdvancedFeedInputJson(event.target.checked);
			}
		}), h("span", null, "Use advanced JSON input")), options.showAdvancedFeedInputJson ? h("textarea", {
			value: options.feedInputText,
			disabled: !feedOpen,
			onChange: function(event) {
				options.setFeedInputText(event.target.value);
			},
			rows: 8,
			"aria-label": "Input feed item JSON"
		}) : null, h("button", {
			type: "submit",
			className: "hermes-workflows-primary",
			disabled: options.feedBusy || !feedOpen
		}, options.feedBusy ? "Adding…" : "Add Item To Feed")) : null);
	}
	function renderRunStartPanel(props) {
		const options = props || {};
		const h = getCreateElement(options);
		if (!options.runPanelOpen) return null;
		const runInputSpec = typeof options.runInputSpec === "function" ? options.runInputSpec() : null;
		const fields = typeof options.inputFieldsForSpec === "function" ? options.inputFieldsForSpec(runInputSpec) : [];
		const workflowId = (options.runWorkflowId || "").trim();
		const publishedVersions = (options.definitions || []).filter(function(d) {
			return workflowIdForDefinition(d) === workflowId && versionForDefinition(d);
		});
		return h("div", {
			ref: options.runPanelRef,
			className: "hermes-workflows-run-overlay",
			role: "dialog",
			"aria-modal": "true",
			"aria-label": "Start workflow run"
		}, h("form", {
			className: "hermes-workflows-run-panel",
			onSubmit: options.runWorkflow
		}, h("div", { className: "hermes-workflows-run-panel-header" }, h("div", null, h("h3", null, "Start Workflow Run"), h("p", { className: "hermes-workflows-muted" }, fields.length ? "Provide the manual trigger input for this execution." : "No start input fields are configured for this workflow. Running will use empty input.")), h("button", {
			type: "button",
			className: "hermes-workflows-link-button",
			onClick: function() {
				options.setRunPanelOpen(false);
			}
		}, "Close")), publishedVersions.length > 1 ? h("label", { className: "hermes-workflows-run-field" }, h("span", null, "Version"), h("select", {
			value: options.selectedRunVersion(workflowId) || "",
			onChange: function(event) {
				const v = parseInt(event.target.value, 10);
				if (!isNaN(v) && options.loadDefinition) options.loadDefinition(workflowId, v).catch(options.fail);
			}
		}, publishedVersions.map(function(d) {
			return h("option", {
				key: d.version,
				value: d.version
			}, "v" + safeString$3(d.version));
		}))) : null, fields.length && !options.showAdvancedInputJson ? h("div", { className: "hermes-workflows-run-fields" }, fields.map(function(field) {
			return renderRunInputField(field, options);
		})) : null, h("label", { className: "hermes-workflows-run-advanced-toggle" }, h("input", {
			type: "checkbox",
			checked: options.showAdvancedInputJson,
			onChange: function(event) {
				options.setShowAdvancedInputJson(event.target.checked);
			}
		}), h("span", null, "Use advanced JSON input")), options.showAdvancedInputJson ? h("textarea", {
			value: options.runInputText,
			onChange: function(event) {
				options.setRunInputText(event.target.value);
			},
			rows: 8,
			"aria-label": "Workflow input JSON"
		}) : null, h("div", { className: "hermes-workflows-run-actions" }, h("button", {
			type: "button",
			onClick: function() {
				options.setRunPanelOpen(false);
			}
		}, "Cancel"), h("button", {
			type: "submit",
			className: "hermes-workflows-primary",
			disabled: options.running
		}, options.running ? "Running…" : "Start Run"))));
	}
	function renderBottomPanel(props) {
		const options = props || {};
		const h = getCreateElement(options);
		return h("div", { className: "hermes-workflows-bottom-panel" + (options.bottomCollapsed ? " is-collapsed" : "") }, h("div", { className: "hermes-workflows-bottom-tabs" }, h("button", {
			type: "button",
			className: "hermes-workflows-bottom-tab is-active",
			role: "tab",
			"aria-selected": "true"
		}, "Validation"), h("button", {
			type: "button",
			className: "hermes-workflows-bottom-toggle",
			"aria-expanded": options.bottomCollapsed ? "false" : "true",
			"aria-controls": "hermes-workflows-bottom-content",
			onClick: function() {
				options.setBottomCollapsed(!options.bottomCollapsed);
			}
		}, options.bottomCollapsed ? "▴ Expand" : "▾ Collapse")), h("div", {
			id: "hermes-workflows-bottom-content",
			className: "hermes-workflows-bottom-content"
		}, options.bottomCollapsed ? null : typeof options.renderValidationChecklist === "function" ? options.renderValidationChecklist({
			activeSpec: options.activeSpec,
			workflowCapabilities: options.workflowCapabilities
		}) : null));
	}
	function renderDiagnosticsPanel(props) {
		const options = props || {};
		const h = getCreateElement(options);
		return h("section", { className: "hermes-workflows-diagnostics" }, h("button", {
			type: "button",
			className: "hermes-workflows-diagnostics-toggle",
			"aria-expanded": options.diagnosticsOpen ? "true" : "false",
			"aria-controls": "hermes-workflows-diagnostics-body",
			onClick: function() {
				options.setDiagnosticsOpen(!options.diagnosticsOpen);
			}
		}, options.diagnosticsOpen ? "Hide diagnostics" : "Show diagnostics"), options.diagnosticsOpen ? h("div", {
			id: "hermes-workflows-diagnostics-body",
			className: "hermes-workflows-diagnostics-body"
		}, h("p", { className: "hermes-workflows-muted" }, "Manual advance for queued workflows and dispatcher status."), typeof options.renderExecutionStallWarning === "function" ? options.renderExecutionStallWarning() : null, h("div", { className: "hermes-workflows-row" }, h("button", {
			type: "button",
			disabled: options.ticking,
			onClick: options.manualTick
		}, options.ticking ? "Ticking…" : "Manual Tick"), h("button", {
			type: "button",
			disabled: options.loading,
			onClick: function() {
				options.refresh();
			}
		}, options.loading ? "Refreshing…" : "Refresh"))) : null);
	}
	function renderHistoryMode(props) {
		const options = props || {};
		const h = getCreateElement(options);
		const executions = options.executions || [];
		return h("section", {
			id: "hermes-workflows-mode-history",
			role: "tabpanel",
			"aria-label": "Workflow execution history",
			className: "hermes-workflows-history-mode"
		}, h("div", { className: "hermes-workflows-history-toolbar" }, h("button", {
			type: "button",
			disabled: options.loading,
			onClick: function() {
				options.refresh();
			}
		}, options.loading ? "Refreshing…" : "Refresh executions")), h("div", { className: "hermes-workflows-history-list" }, executions.length ? executions.slice(0, 50).map(function(execution) {
			const eid = safeString$3(execution.execution_id || execution.id);
			const execStatus = safeString$3(execution.status);
			const statusClass = execStatus === "succeeded" ? " is-succeeded" : execStatus === "failed" ? " is-failed" : "";
			return h("button", {
				key: eid,
				type: "button",
				"data-execution-id": eid,
				className: "hermes-workflows-history-row" + (options.selectedExecution && String(options.selectedExecution.execution_id || options.selectedExecution.id || "") === eid ? " is-selected" : ""),
				onClick: function() {
					options.loadExecution(eid).catch(options.fail);
					options.pushMode("history", { execution: eid });
				}
			}, h("span", { className: "hermes-workflows-history-row-id" }, eid.slice(0, 16)), h("span", { className: "hermes-workflows-history-row-status" + statusClass }, execStatus));
		}) : h("p", { className: "hermes-workflows-muted" }, "No executions yet.")), h("div", { className: "hermes-workflows-history-detail" }, typeof options.renderTimeline === "function" ? options.renderTimeline() : null));
	}
	//#endregion
	//#region ../plugins/workflows/dashboard/src/history.js
	var CANCELABLE_STATUSES = /* @__PURE__ */ new Set([
		"queued",
		"running",
		"waiting"
	]);
	function serializeFilters(opts) {
		if (!opts || typeof opts !== "object") return "";
		const parts = [];
		if (opts.workflowId) parts.push("workflow_id=" + encodeURIComponent(opts.workflowId));
		if (opts.status) parts.push("status=" + encodeURIComponent(opts.status));
		if (opts.version != null) parts.push("version=" + encodeURIComponent(opts.version));
		if (opts.triggerId) parts.push("trigger_id=" + encodeURIComponent(opts.triggerId));
		if (opts.before && opts.before.createdAt != null && opts.before.executionId) {
			parts.push("before_created_at=" + encodeURIComponent(opts.before.createdAt));
			parts.push("before_execution_id=" + encodeURIComponent(opts.before.executionId));
		}
		if (opts.limit != null) parts.push("limit=" + encodeURIComponent(opts.limit));
		return parts.join("&");
	}
	function historyListPath(opts) {
		const qs = serializeFilters(opts);
		return "/executions" + (qs ? "?" + qs : "");
	}
	function detailUrl(executionId) {
		if (!executionId) return "";
		return "/executions/" + encodeURIComponent(executionId) + "/detail";
	}
	function canCancel(status) {
		return CANCELABLE_STATUSES.has(status);
	}
	function buildRerunBody(input) {
		return { input: input && typeof input === "object" ? input : {} };
	}
	//#endregion
	//#region ../plugins/workflows/dashboard/src/workspace.js
	var WORKSPACE_MODES = [
		"build",
		"run",
		"history"
	];
	var WORKSPACE_PATH_PREFIX = "/workflows/";
	var MODE_TO_QUERY_KEY = {
		run: "feed",
		history: "execution"
	};
	function isMode(value) {
		return WORKSPACE_MODES.indexOf(value) !== -1;
	}
	function snapFlowPosition(position, gridSize = 20) {
		const size = Number.isFinite(gridSize) && gridSize > 0 ? gridSize : 20;
		const x = Number.isFinite(position && position.x) ? position.x : 0;
		const y = Number.isFinite(position && position.y) ? position.y : 0;
		return {
			x: Math.round(x / size) * size,
			y: Math.round(y / size) * size
		};
	}
	function libraryGroups(categories) {
		if (!Array.isArray(categories)) return [];
		return categories.map(function(category) {
			const nodes = category && Array.isArray(category.nodes) ? category.nodes : [];
			return {
				name: category && category.name,
				color: category && category.color,
				types: nodes.filter(Array.isArray).map(function(node) {
					return node[0];
				})
			};
		});
	}
	function trimSlashes(value) {
		return String(value || "").replace(/^\/+|\/+$/g, "");
	}
	function parseLocation(location) {
		if (!location || typeof location !== "object") return {
			workflowId: "",
			mode: "build",
			search: ""
		};
		const segments = trimSlashes(String(location.pathname || "")).replace(/^\/+/, "").split("/").filter(Boolean);
		let workflowId = "";
		let modeSegment = "";
		if (segments.length >= 2 && segments[0] === "workflows") {
			workflowId = segments[1];
			modeSegment = segments[2] || "";
		}
		const search = String(location.search || "");
		const mode = isMode(modeSegment) ? modeSegment : "build";
		return {
			workflowId,
			mode,
			search
		};
	}
	function modeForLocation(location) {
		return parseLocation(location).mode;
	}
	function selectionForMode(mode, selection) {
		const key = MODE_TO_QUERY_KEY[mode];
		if (!key) return "";
		const value = selection && typeof selection === "object" ? selection[key] : "";
		return value ? String(value) : "";
	}
	function locationForMode(workflowId, mode, selection) {
		const id = trimSlashes(workflowId);
		const safeMode = isMode(mode) ? mode : "build";
		const base = WORKSPACE_PATH_PREFIX + id + (safeMode === "build" ? "" : "/" + safeMode);
		const value = selectionForMode(safeMode, selection);
		return value ? base + "?" + MODE_TO_QUERY_KEY[safeMode] + "=" + encodeURIComponent(value) : base;
	}
	//#endregion
	//#region ../plugins/workflows/dashboard/src/topbar.js
	function renderTopBar({ createElement: h, activeSpec, selectedDefinition, workflowIdForDefinition, versionForDefinition, renderWorkspaceTabs, validateDefinition, validating, deployDefinition, deploying, deleteWorkflow, deleting, openRunPanel, openNodePalette, running, refresh, loading, showAdvancedYaml, setShowAdvancedYaml, persistedRunCapable }) {
		const workflowName = activeSpec ? String(activeSpec.name || activeSpec.id || activeSpec.workflow_id || "Untitled workflow") : "Untitled workflow";
		const hasDraft = !!activeSpec;
		const workflowId = selectedDefinition && workflowIdForDefinition(selectedDefinition);
		const version = selectedDefinition && versionForDefinition(selectedDefinition);
		const persisted = !!(workflowId && version);
		const runCapable = typeof persistedRunCapable === "function" ? persistedRunCapable() : persisted;
		return h("div", { className: "hermes-workflows-topbar" }, h("div", { className: "hermes-workflows-topbar-left" }, h("span", { className: "hermes-workflows-topbar-name" }, workflowName), h("span", { className: "hermes-workflows-topbar-status" }, persisted ? "v" + version + " · enabled" : "draft")), renderWorkspaceTabs ? renderWorkspaceTabs() : null, h("div", { className: "hermes-workflows-topbar-actions" }, h("button", {
			type: "button",
			"aria-label": "Add Node",
			onClick: function() {
				if (typeof openNodePalette === "function") openNodePalette();
			}
		}, "Add Node"), h("button", {
			type: "button",
			onClick: validateDefinition,
			disabled: validating || !hasDraft
		}, "Validate"), h("button", {
			type: "button",
			onClick: deployDefinition,
			disabled: deploying || !hasDraft,
			className: "hermes-workflows-primary"
		}, "Deploy"), persisted ? h("button", {
			type: "button",
			onClick: deleteWorkflow,
			disabled: deleting
		}, "Delete") : null, persisted ? h("button", {
			type: "button",
			onClick: openRunPanel,
			disabled: running || !runCapable
		}, "Run") : null, h("button", {
			type: "button",
			onClick: refresh,
			disabled: loading
		}, "Refresh"), h("button", {
			type: "button",
			onClick: () => setShowAdvancedYaml(!showAdvancedYaml)
		}, "YAML")));
	}
	//#endregion
	//#region ../plugins/workflows/dashboard/src/editor-model.js
	var SUPPORTED_TRIGGERS = [
		"manual",
		"schedule",
		"webhook",
		"kanban_event"
	];
	var SUPPORTED_NODES = [
		"agent_task",
		"fail",
		"join",
		"parallel",
		"pass",
		"switch",
		"wait",
		"send_message",
		"subworkflow"
	];
	var SCALAR_INPUT_KINDS = [
		"text",
		"long_text",
		"prompt",
		"criteria",
		"url",
		"repo_path",
		"boolean",
		"number",
		"integer"
	];
	var NODE_TYPE_FIELDS = {
		agent_task: [
			"profile",
			"prompt",
			"result_contract",
			"provider",
			"model",
			"skills",
			"workspace_kind",
			"workspace_path",
			"goal_mode",
			"goal_max_turns",
			"max_retries"
		],
		switch: ["cases", "default"],
		wait: ["seconds"],
		pass: ["output"],
		fail: ["output"],
		parallel: [],
		join: [],
		send_message: [
			"target",
			"message_text",
			"platform"
		],
		subworkflow: ["workflow_ref", "input_mapping"]
	};
	function defaultTriggerForType(type, triggerId) {
		var trigger = {
			type,
			id: triggerId
		};
		if (type === "schedule") trigger.schedule = "0 9 * * *";
		else if (type === "webhook") trigger.path = "/webhook/" + triggerId;
		return trigger;
	}
	function changeNodeType(spec, nodeId, nextType) {
		var next = JSON.parse(JSON.stringify(spec || {}));
		var node = (next.nodes || {})[nodeId];
		if (!node) return {
			spec: next,
			removedFields: []
		};
		var oldFields = NODE_TYPE_FIELDS[node.type || "pass"] || [];
		var newFieldSet = {};
		(NODE_TYPE_FIELDS[nextType] || []).forEach(function(f) {
			newFieldSet[f] = true;
		});
		var removedFields = [];
		oldFields.forEach(function(f) {
			if (!newFieldSet[f] && node[f] !== void 0) {
				removedFields.push(f);
				delete node[f];
			}
		});
		node.type = nextType;
		if (nextType === "agent_task") {
			if (!node.profile) node.profile = "default";
			if (!node.prompt) node.prompt = "Return JSON only matching the result contract.";
			if (!node.result_contract) node.result_contract = {
				summary: "string",
				status: "string"
			};
		} else if (nextType === "wait") {
			if (node.seconds === void 0) node.seconds = 60;
		} else if (nextType === "fail") {
			if (!node.output) node.output = "Workflow failed.";
		} else if (nextType === "switch") {
			if (!node.cases) node.cases = [];
		} else if (nextType === "pass") {
			if (!node.output) node.output = {};
		} else if (nextType === "send_message") {
			if (node.target === void 0) node.target = "";
			if (node.message_text === void 0) node.message_text = "";
			if (node.platform === void 0) node.platform = "telegram";
		} else if (nextType === "subworkflow") {
			if (node.workflow_ref === void 0) node.workflow_ref = "";
			if (node.input_mapping === void 0) node.input_mapping = {};
		}
		return {
			spec: next,
			removedFields: removedFields.sort()
		};
	}
	function workflowIdFromText(value) {
		var slug = String(value || "workflow draft").trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 64);
		if (slug && /^[a-z]/.test(slug)) return slug;
		return "workflow_draft";
	}
	function inputRowsFromTrigger(trigger) {
		var schema = trigger && trigger.input_schema && typeof trigger.input_schema === "object" && !Array.isArray(trigger.input_schema) ? trigger.input_schema : {};
		return Object.keys(schema).sort().map(function(name) {
			var field = schema[name] && typeof schema[name] === "object" && !Array.isArray(schema[name]) ? schema[name] : {};
			var kind = String(field.kind || "text");
			if (kind === "object" || kind === "json" || kind === "document") kind = "text";
			if (SCALAR_INPUT_KINDS.indexOf(kind) === -1) kind = "text";
			return {
				name,
				kind,
				required: !!field.required,
				defaultValue: field.default === void 0 || field.default === null ? "" : String(field.default),
				minLength: field.min_length === void 0 || field.min_length === null ? "" : String(field.min_length),
				maxLength: field.max_length === void 0 || field.max_length === null ? "" : String(field.max_length)
			};
		});
	}
	function inputSchemaFromRows(rows) {
		var schema = {};
		(rows || []).forEach(function(row) {
			var name = workflowIdFromText(row && row.name ? row.name : "").replace(/-/g, "_");
			if (!name) return;
			var field = { kind: SCALAR_INPUT_KINDS.indexOf(row.kind) !== -1 ? row.kind : "text" };
			if (row.required) field.required = true;
			if (row.defaultValue !== void 0 && String(row.defaultValue).trim() !== "") field.default = String(row.defaultValue);
			var minLength = String(row.minLength || "").trim();
			var maxLength = String(row.maxLength || "").trim();
			if (minLength) field.min_length = Math.max(0, parseInt(minLength, 10) || 0);
			if (maxLength) field.max_length = Math.max(0, parseInt(maxLength, 10) || 0);
			schema[name] = field;
		});
		return schema;
	}
	function triggerIntakeFromForm(mode, dedupeKey, readyPath) {
		var intake = { mode: mode === "continuous" ? "continuous" : "single" };
		var dedupe = String(dedupeKey || "").trim();
		var ready = String(readyPath || "").trim();
		if (dedupe) intake.dedupe_key = dedupe;
		if (ready) intake.ready_when = {
			op: "exists",
			path: ready
		};
		return intake;
	}
	function readyPathFromTrigger(trigger) {
		var cond = trigger && trigger.intake && trigger.intake.ready_when;
		if (!cond || typeof cond !== "object" || Array.isArray(cond)) return "";
		if (String(cond.op || "") === "exists" && typeof cond.path === "string") return cond.path;
		return "";
	}
	//#endregion
	//#region ../plugins/workflows/dashboard/src/canvas-nodes.js
	var NODE_COLORS = {
		trigger: "#ff6d5a",
		pass: "#1abc9c",
		switch: "#3498db",
		agent_task: "#9b59b6",
		wait: "#f39c12",
		parallel: "#27ae60",
		join: "#2ecc71",
		fail: "#e74c3c",
		send_message: "#e67e22",
		subworkflow: "#2c3e50"
	};
	var NODE_ICONS = {
		trigger: "⚡",
		manual: "▶",
		schedule: "⏰",
		webhook: "🔗",
		kanban_event: "📋",
		pass: "▣",
		switch: "◇",
		agent_task: "🤖",
		wait: "⏱",
		parallel: "⇉",
		join: "⇥",
		fail: "✕",
		send_message: "✉",
		subworkflow: "⎌"
	};
	function safeString$2(value) {
		if (value === null || value === void 0 || value === "") return "—";
		return String(value);
	}
	function classSafe(value) {
		return String(value || "unknown").replace(/[^a-z0-9_-]+/gi, "-").toLowerCase();
	}
	function makeWorkflowNode(kind, SDK) {
		const h = SDK.React.createElement;
		const FlowSDK = SDK.ReactFlow || SDK.reactFlow || {};
		const Handle = FlowSDK.Handle;
		const Position = FlowSDK.Position || {
			Left: "left",
			Right: "right"
		};
		const color = NODE_COLORS[kind] || "#64748b";
		return function WorkflowNode(props) {
			const data = props && props.data || {};
			const status = data.status || "idle";
			const node = data.node || {};
			const label = node.id || data.id;
			const icon = NODE_ICONS[node.trigger_type || node.triggerType || node.type || kind] || NODE_ICONS[kind] || "•";
			return h("div", {
				className: "hermes-workflows-rf-node is-" + classSafe(kind) + " is-status-" + classSafe(status),
				role: "button",
				tabIndex: 0,
				"aria-label": "Edit workflow cell " + safeString$2(label || "cell"),
				onClick: function(event) {
					event.stopPropagation();
					if (data.onSelect) data.onSelect(node);
				},
				onKeyDown: function(event) {
					if (event.key === "Enter" || event.key === " ") {
						event.preventDefault();
						event.stopPropagation();
						if (data.onSelect) data.onSelect(node);
					}
				}
			}, Handle ? h(Handle, {
				type: "target",
				position: Position.Left
			}) : null, h("div", {
				className: "hermes-workflows-rf-node-header",
				style: { background: color }
			}, h("span", { className: "hermes-workflows-rf-node-icon" }, icon), h("span", { className: "hermes-workflows-rf-node-label" }, safeString$2(label))), h("div", { className: "hermes-workflows-rf-node-body" }, h("div", { className: "hermes-workflows-rf-node-type" }, String(kind).replace(/_/g, " ")), status && status !== "idle" ? h("div", { className: "hermes-workflows-rf-node-status" }, safeString$2(status)) : null), Handle ? h(Handle, {
				type: "source",
				position: Position.Right
			}) : null);
		};
	}
	//#endregion
	//#region ../plugins/workflows/dashboard/src/palette.js
	var NODE_CATEGORIES = [
		{
			name: "Triggers",
			color: "trigger",
			nodes: [
				[
					"manual",
					"Manual Trigger",
					"Start the workflow on demand."
				],
				[
					"schedule",
					"Schedule Trigger",
					"Start the workflow on a schedule."
				],
				[
					"webhook",
					"Webhook",
					"Start the workflow from an HTTP request."
				]
			]
		},
		{
			name: "AI & Logic",
			color: "agent_task",
			nodes: [
				[
					"agent_task",
					"AI Agent Task",
					"Delegate work to a Hermes profile."
				],
				[
					"switch",
					"Switch/Branch",
					"Route based on a value or condition."
				],
				[
					"pass",
					"Pass/Transform",
					"Shape or summarize data."
				]
			]
		},
		{
			name: "Flow Control",
			color: "parallel",
			nodes: [
				[
					"parallel",
					"Parallel Split",
					"Run independent branches."
				],
				[
					"join",
					"Join",
					"Wait for branches to complete."
				],
				[
					"wait",
					"Wait/Delay",
					"Pause before continuing."
				]
			]
		},
		{
			name: "Actions",
			color: "send_message",
			nodes: [[
				"send_message",
				"Send Message",
				"Send a message to a configured target."
			], [
				"fail",
				"Fail/Reject",
				"Stop the workflow with an error."
			]]
		}
	];
	function safeString$1(value) {
		return value === null || value === void 0 ? "" : String(value);
	}
	function selectionKey(definition) {
		if (!definition) return "";
		return safeString$1(definition.workflow_id || definition.id) + ":" + safeString$1(definition.version);
	}
	function isSupported(type, trigger) {
		return (trigger ? SUPPORTED_TRIGGERS : SUPPORTED_NODES).indexOf(type) !== -1;
	}
	function renderWorkflowForm(h, props) {
		function call(handler) {
			if (typeof handler === "function") return handler.apply(null, Array.prototype.slice.call(arguments, 1));
		}
		const nameId = props.workflowNameInputId || "hermes-workflow-name";
		const goalId = props.workflowGoalInputId || "hermes-workflow-goal";
		return h("section", { className: "hermes-workflows-palette-section hermes-workflows-palette-creation" }, h("h3", null, props.activeSpec ? "New workflow / prompt" : "New workflow"), h("p", { className: "hermes-workflows-muted" }, "Describe it or start from blank."), h("form", {
			className: "hermes-workflows-stack",
			"aria-label": "Create workflow",
			onSubmit: props.draftFromGoal
		}, h("label", { htmlFor: nameId }, "Workflow name"), h("input", {
			id: nameId,
			value: props.newWorkflowName || "",
			onChange: function(event) {
				call(props.setNewWorkflowName, event.target.value);
			},
			placeholder: "Workflow name"
		}), h("label", { htmlFor: goalId }, "Describe workflow goal"), h("textarea", {
			id: goalId,
			"aria-label": "Describe workflow goal",
			value: props.goalText || "",
			onChange: function(event) {
				call(props.setGoalText, event.target.value);
			},
			placeholder: "Example: review code changes, run tests, then deploy if approved."
		}), h("div", { className: "hermes-workflows-row" }, h("button", {
			type: "submit",
			disabled: props.drafting,
			className: "hermes-workflows-primary"
		}, props.drafting ? "Generating…" : "Generate From Prompt"), h("button", {
			type: "button",
			"aria-label": "Start from scratch",
			onClick: props.startBlankWorkflow
		}, "Start From Scratch"))), props.activeSpec ? h("form", {
			className: "hermes-workflows-stack",
			onSubmit: props.refineWorkflow
		}, h("textarea", {
			value: props.refineText || "",
			onChange: function(event) {
				call(props.setRefineText, event.target.value);
			},
			placeholder: "Refine: add a step, change routing, etc.",
			"aria-label": "Refine workflow"
		}), h("button", {
			type: "submit",
			disabled: props.refining
		}, props.refining ? "Refining…" : "Refine")) : null, props.draftResult ? h("div", { className: "hermes-workflows-palette-draft hermes-workflows-stack" }, props.draftResult.summary ? h("p", { className: "hermes-workflows-muted" }, props.draftResult.summary) : null, (props.draftResult.assumptions || []).length ? h("p", null, h("strong", null, "Assumptions: "), props.draftResult.assumptions.join("; ")) : null, (props.draftResult.warnings || []).length ? h("p", { className: "hermes-workflows-muted" }, h("strong", null, "Warnings: "), props.draftResult.warnings.join("; ")) : null, h("div", { className: "hermes-workflows-row" }, h("button", {
			type: "button",
			onClick: props.acceptDraftCandidate,
			className: "hermes-workflows-primary"
		}, props.candidateSource === "generate" ? "Accept Draft" : "Accept Changes"), h("button", {
			type: "button",
			onClick: props.rejectDraftCandidate
		}, "Reject"))) : null);
	}
	function renderWorkflowOnboarding(props) {
		return props.createElement("div", { className: "hermes-workflows-onboarding-form" }, renderWorkflowForm(props.createElement, props));
	}
	function WorkflowRail(props) {
		const h = props.createElement;
		const useState = (props.React || {}).useState || function(initial) {
			return [initial, function() {}];
		};
		const definitions = Array.isArray(props.definitions) ? props.definitions : [];
		const executions = Array.isArray(props.executions) ? props.executions : [];
		const groups = Array.isArray(props.libraryGroups) ? props.libraryGroups : [];
		const call = function(handler) {
			if (typeof handler === "function") return handler.apply(null, Array.prototype.slice.call(arguments, 1));
		};
		const workflowsExpandedState = useState(true);
		const executionsExpandedState = useState(true);
		const libraryExpandedState = useState(true);
		const [workflowsExpanded, setWorkflowsExpanded] = workflowsExpandedState;
		const [executionsExpanded, setExecutionsExpanded] = executionsExpandedState;
		const [libraryExpanded, setLibraryExpanded] = libraryExpandedState;
		function disclosure(label, controlsId, expanded, setExpanded) {
			return h("button", {
				type: "button",
				className: "hermes-workflows-rail-disclosure" + (expanded ? " is-expanded" : ""),
				"aria-expanded": expanded ? "true" : "false",
				"aria-controls": controlsId,
				onClick: function() {
					setExpanded(!expanded);
				}
			}, h("span", { className: "hermes-workflows-rail-disclosure-label" }, label), h("span", {
				className: "hermes-workflows-rail-disclosure-caret",
				"aria-hidden": "true"
			}, "▾"));
		}
		const workflowsSection = h("section", { className: "hermes-workflows-rail-section hermes-workflows-rail-workflows" }, disclosure("Workflows", "hermes-workflows-rail-workflows-panel", workflowsExpanded, setWorkflowsExpanded), workflowsExpanded ? h("div", {
			id: "hermes-workflows-rail-workflows-panel",
			className: "hermes-workflows-palette-panel"
		}, props.hideWorkflowForm ? null : renderWorkflowForm(h, props), definitions.length ? h("div", { className: "hermes-workflows-sidebar-list" }, definitions.map(function(definition) {
			const id = definition.workflow_id || definition.id;
			const key = selectionKey(definition);
			return h("button", {
				key,
				type: "button",
				className: "hermes-workflows-sidebar-item" + (key === selectionKey(props.selectedDefinition) ? " is-selected" : ""),
				onClick: function() {
					call(props.loadDefinition, id, definition.version);
				}
			}, h("span", { className: "hermes-workflows-sidebar-item-title" }, safeString$1(definition.name || id)), h("span", { className: "hermes-workflows-sidebar-badge" + (definition.enabled ? " is-enabled" : "") }, definition.enabled ? "on" : "off"));
		})) : h("p", { className: "hermes-workflows-muted" }, "No workflows deployed.")) : null);
		const executionsSection = h("section", { className: "hermes-workflows-rail-section hermes-workflows-rail-executions" }, disclosure("Executions", "hermes-workflows-rail-executions-panel", executionsExpanded, setExecutionsExpanded), executionsExpanded ? h("div", {
			id: "hermes-workflows-rail-executions-panel",
			className: "hermes-workflows-palette-panel"
		}, executions.length ? h("div", { className: "hermes-workflows-sidebar-list" }, executions.slice(0, 20).map(function(execution) {
			const id = safeString$1(execution.execution_id || execution.id);
			const status = safeString$1(execution.status);
			const statusClass = status === "succeeded" ? " is-succeeded" : status === "failed" ? " is-failed" : "";
			return h("button", {
				key: id,
				type: "button",
				className: "hermes-workflows-sidebar-item",
				onClick: function() {
					call(props.loadExecution, id);
				}
			}, h("span", { className: "hermes-workflows-sidebar-item-title" }, id.slice(0, 16)), h("span", { className: "hermes-workflows-sidebar-badge" + statusClass }, status));
		})) : h("p", { className: "hermes-workflows-muted" }, "No executions yet.")) : null);
		function safeId(name) {
			return safeString$1(name).replace(/[^a-z0-9_-]+/gi, "-").toLowerCase();
		}
		const libraryGroupsContent = groups.length ? h("div", { className: "hermes-workflows-rail-library-groups" }, groups.map(function(group) {
			const label = safeString$1(group && group.name);
			const controlsId = "rail-library-group-" + safeId(label);
			const color = group && group.color || "inherit";
			const types = Array.isArray(group && group.types) ? group.types : [];
			return h("div", {
				key: label,
				className: "hermes-workflows-rail-library-group"
			}, h("button", {
				type: "button",
				className: "hermes-workflows-rail-library-group-button",
				"aria-expanded": "false",
				"aria-controls": controlsId,
				onClick: function() {
					call(props.openNodePalette, label);
				}
			}, h("span", {
				className: "hermes-workflows-rail-library-group-name",
				style: { color: NODE_COLORS[color] || "inherit" }
			}, label), h("span", {
				className: "hermes-workflows-rail-library-group-count",
				"aria-hidden": "true"
			}, String(types.length))), h("span", {
				id: controlsId,
				className: "hermes-workflows-sr-only"
			}, types.join(", ")));
		})) : h("p", { className: "hermes-workflows-muted" }, "No node library loaded.");
		return h("aside", { className: "hermes-workflows-sidebar hermes-workflows-rail" }, workflowsSection, executionsSection, h("section", { className: "hermes-workflows-rail-section hermes-workflows-rail-library" }, disclosure("Library", "hermes-workflows-rail-library-panel", libraryExpanded, setLibraryExpanded), libraryExpanded ? h("div", {
			id: "hermes-workflows-rail-library-panel",
			className: "hermes-workflows-palette-panel"
		}, libraryGroupsContent) : null));
	}
	function NodePaletteOverlay(props) {
		const h = props.createElement;
		const React = props.React || {};
		const searchState = (React.useState || function(initial) {
			return [initial, function() {}];
		})("");
		const search = searchState[0];
		const setSearch = searchState[1];
		if (!props.isOpen) return null;
		const groupName = safeString$1(props.nodePaletteGroup).trim().toLowerCase();
		const needle = safeString$1(search).trim().toLowerCase();
		const categories = (Array.isArray(props.categories) ? props.categories : NODE_CATEGORIES).map(function(category) {
			const nodes = Array.isArray(category && category.nodes) ? category.nodes.filter(function(node) {
				const type = safeString$1(node && node[0]).toLowerCase();
				const label = safeString$1(node && node[1]).toLowerCase();
				return (!needle || type.includes(needle) || label.includes(needle)) && isSupported(type, category && category.name === "Triggers");
			}) : [];
			return Object.assign({}, category, { nodes });
		}).filter(function(category) {
			return (!groupName || safeString$1(category && category.name).trim().toLowerCase() === groupName) && category.nodes.length > 0;
		});
		function call(handler) {
			if (typeof handler === "function") return handler.apply(null, Array.prototype.slice.call(arguments, 1));
		}
		function dragProps(type) {
			return {
				draggable: true,
				onDragStart: function(event) {
					if (event.dataTransfer && event.dataTransfer.setData) event.dataTransfer.setData("text/plain", type);
					if (typeof window !== "undefined") window.__HERMES_DRAG_NODE_TYPE = type;
				}
			};
		}
		function selectNode(type, trigger) {
			if (typeof props.onSelectNode === "function") return props.onSelectNode(type, trigger);
			if (trigger) return call(props.addTriggerOfType, type);
			return call(props.addWorkflowCellAtPosition, type);
		}
		function renderTile(category, node) {
			const type = node[0];
			const trigger = category.name === "Triggers";
			return h("button", Object.assign({
				key: type,
				type: "button",
				className: "hermes-workflows-palette-card hermes-workflows-node-palette-tile",
				"aria-label": "Add " + node[1],
				onClick: function() {
					selectNode(type, trigger);
				},
				style: { "--wf-palette-color": NODE_COLORS[category.color] || "inherit" }
			}, dragProps(type)), h("span", {
				className: "hermes-workflows-palette-icon",
				"aria-hidden": "true"
			}, NODE_ICONS[type] || "•"), h("span", { className: "hermes-workflows-palette-text" }, h("span", { className: "hermes-workflows-palette-title" }, node[1]), h("span", { className: "hermes-workflows-palette-desc" }, node[2])));
		}
		const Fragment = React.Fragment || "div";
		return h("div", {
			className: "hermes-workflows-node-palette-overlay",
			role: "dialog",
			"aria-label": "Add workflow node"
		}, h("div", { className: "hermes-workflows-node-palette-panel" }, h("div", { className: "hermes-workflows-node-palette-header" }, h("h2", null, "Add Node"), h("button", {
			type: "button",
			"aria-label": "Close",
			onClick: props.closeNodePalette
		}, "Close")), h("input", {
			type: "search",
			className: "hermes-workflows-palette-search-input",
			placeholder: "Search nodes...",
			"aria-label": "Search nodes",
			value: search,
			onChange: function(event) {
				setSearch(event.target.value);
			}
		}), categories.length ? categories.map(function(category) {
			return h(Fragment, { key: category.name }, h("h3", {
				className: "hermes-workflows-palette-category",
				style: { color: NODE_COLORS[category.color] || "inherit" }
			}, category.name), h("div", { className: "hermes-workflows-node-palette" }, category.nodes.map(function(node) {
				return renderTile(category, node);
			})));
		}) : h("p", { className: "hermes-workflows-muted" }, "No nodes match the current filter.")));
	}
	//#endregion
	//#region ../plugins/workflows/dashboard/src/inspector.js
	function asArray(value) {
		return Array.isArray(value) ? value : [];
	}
	function safeString(value) {
		if (value === null || value === void 0 || value === "") return "—";
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
		return asArray(options && options.profiles).filter(function(profile) {
			return profile && profile.name;
		});
	}
	function providerRows(options) {
		return asArray(options && options.providers).filter(function(provider) {
			return provider && (provider.slug || provider.provider);
		});
	}
	function specToEditorText(spec) {
		return JSON.stringify(spec || {}, null, 2);
	}
	function findSpecNode(spec, nodeId) {
		if (!spec || !nodeId) return null;
		if (Array.isArray(spec.nodes)) return spec.nodes.find(function(node) {
			return String(node && (node.id || node.name)) === String(nodeId);
		}) || null;
		var node = spec.nodes && spec.nodes[nodeId];
		return node ? Object.assign({}, node, { id: nodeId }) : null;
	}
	function field(props, label, control, hint) {
		var h = props.createElement;
		var id = props.id || "wf-field-" + label.replace(/[^a-z0-9]+/gi, "-").toLowerCase();
		var labelledLabel = h("label", {
			htmlFor: id,
			key: "l"
		}, label);
		var fieldChildren = [labelledLabel, control];
		if (hint) fieldChildren.push(h("p", {
			className: "hermes-workflows-field-hint",
			key: "h"
		}, hint));
		return h("div", {
			className: "hermes-workflows-field",
			key: id
		}, labelledLabel, control, hint ? h("p", {
			className: "hermes-workflows-field-hint",
			key: "h"
		}, hint) : null);
	}
	function fieldRow(props, key, cells) {
		var h = props.createElement;
		return h("div", {
			className: "hermes-workflows-field-row",
			key
		}, cells);
	}
	function sectionPanel(props, key, title, body) {
		var h = props.createElement;
		return h("section", {
			className: "hermes-workflows-section-panel",
			key
		}, h("h4", { className: "hermes-workflows-section-panel-title" }, title), body);
	}
	function renderAdvancedNodeJson(props, spec) {
		var h = props.createElement;
		return h("div", { className: "hermes-workflows-stack" }, h("textarea", {
			className: "hermes-workflows-node-json",
			"aria-label": "Advanced node JSON",
			value: props.nodeJson,
			onChange: function(event) {
				props.setNodeJson(event.target.value);
			}
		}), h("div", { className: "hermes-workflows-row" }, h("button", {
			type: "button",
			onClick: props.applyNodeJson
		}, "Apply node JSON"), h("button", {
			type: "button",
			onClick: props.useJsonDraft,
			disabled: !spec
		}, "Use JSON draft")));
	}
	function renderPromptAssistant(props) {
		var h = props.createElement;
		if (!props.promptAssistantOpen) return null;
		return h("div", { className: "hermes-workflows-assistant" }, h("h4", null, "Prompt assistant"), field({
			createElement: h,
			id: "wf-prompt-goal"
		}, "Workflow goal", h("textarea", {
			id: "wf-prompt-goal",
			value: props.promptAssistantGoal,
			onChange: function(event) {
				props.setPromptAssistantGoal(event.target.value);
			}
		})), props.promptAssistantAdvanced ? h("div", { className: "hermes-workflows-stack" }, field({
			createElement: h,
			id: "wf-prompt-objective"
		}, "Cell objective", h("textarea", {
			id: "wf-prompt-objective",
			value: props.promptAssistantObjective,
			onChange: function(event) {
				props.setPromptAssistantObjective(event.target.value);
			}
		})), field({
			createElement: h,
			id: "wf-prompt-context"
		}, "Context placeholders", h("textarea", {
			id: "wf-prompt-context",
			value: props.promptAssistantContext,
			onChange: function(event) {
				props.setPromptAssistantContext(event.target.value);
			}
		})), field({
			createElement: h,
			id: "wf-prompt-output"
		}, "Output contract JSON", h("textarea", {
			id: "wf-prompt-output",
			value: props.promptAssistantOutput,
			onChange: function(event) {
				props.setPromptAssistantOutput(event.target.value);
			}
		})), field({
			createElement: h,
			id: "wf-prompt-constraints"
		}, "Constraints", h("textarea", {
			id: "wf-prompt-constraints",
			value: props.promptAssistantConstraints,
			onChange: function(event) {
				props.setPromptAssistantConstraints(event.target.value);
			}
		})), h("button", {
			type: "button",
			onClick: function() {
				props.setPromptAssistantAdvanced(false);
			},
			className: "hermes-workflows-link-btn"
		}, "Hide advanced fields")) : h("button", {
			type: "button",
			onClick: function() {
				props.setPromptAssistantAdvanced(true);
			},
			className: "hermes-workflows-link-btn"
		}, "Show advanced fields"), h("button", {
			type: "button",
			onClick: props.draftPromptWithAssistant,
			className: "hermes-workflows-primary"
		}, "Draft prompt"));
	}
	function renderAgentTaskParameters(props) {
		var h = props.createElement;
		var profiles = profileRows(props.agentRoutingOptions);
		var providers = providerRows(props.agentRoutingOptions);
		var selectedProvider = providers.find(function(provider) {
			return String(provider.slug || provider.provider || "") === props.agentProvider;
		});
		var models = asArray(selectedProvider && selectedProvider.models);
		return h("div", { className: "hermes-workflows-stack" }, profiles.length ? field({
			createElement: h,
			id: "wf-agent-profile"
		}, "Assigned profile", h("select", {
			id: "wf-agent-profile",
			value: props.agentProfile,
			onChange: function(event) {
				props.setAgentProfile(event.target.value);
			}
		}, [h("option", {
			key: "",
			value: ""
		}, "Choose profile")].concat(profiles.map(function(profile) {
			var label = profile.name + (profile.provider || profile.model ? " · " + safeString(profile.provider) + " / " + safeString(profile.model) : "");
			return h("option", {
				key: profile.name,
				value: profile.name
			}, label);
		})))) : field({
			createElement: h,
			id: "wf-agent-profile"
		}, "Assigned profile", h("input", {
			id: "wf-agent-profile",
			value: props.agentProfile,
			onChange: function(event) {
				props.setAgentProfile(event.target.value);
			},
			placeholder: "reviewer"
		})), field({
			createElement: h,
			id: "wf-agent-provider"
		}, "Provider override", h("select", {
			id: "wf-agent-provider",
			value: props.agentProvider,
			onChange: function(event) {
				props.setAgentProvider(event.target.value);
				props.setAgentModel("");
			}
		}, [h("option", {
			key: "",
			value: ""
		}, "Use profile default provider")].concat(providers.map(function(provider) {
			var slug = String(provider.slug || provider.provider || "");
			return h("option", {
				key: slug,
				value: slug
			}, safeString(provider.label || slug));
		})))), models.length ? field({
			createElement: h,
			id: "wf-agent-model"
		}, "Model override", h("select", {
			id: "wf-agent-model",
			value: props.agentModel,
			onChange: function(event) {
				props.setAgentModel(event.target.value);
			}
		}, [h("option", {
			key: "",
			value: ""
		}, "Use profile default model")].concat(models.map(function(model) {
			return h("option", {
				key: String(model),
				value: String(model)
			}, safeString(model));
		})))) : field({
			createElement: h,
			id: "wf-agent-model"
		}, "Model override", h("input", {
			id: "wf-agent-model",
			value: props.agentModel,
			onChange: function(event) {
				props.setAgentModel(event.target.value);
			},
			placeholder: props.agentProvider ? "Model name for selected provider" : "Use profile default model"
		})), field({
			createElement: h,
			id: "wf-agent-title"
		}, "Task title", h("input", {
			id: "wf-agent-title",
			value: props.agentTitle,
			onChange: function(event) {
				props.setAgentTitle(event.target.value);
			},
			placeholder: "Review change"
		})), field({
			createElement: h,
			id: "wf-agent-notes"
		}, "Notes", h("textarea", {
			id: "wf-agent-notes",
			className: "hermes-workflows-prompt-editor",
			value: props.promptText,
			onChange: function(event) {
				props.setPromptText(event.target.value);
			},
			placeholder: "Optional notes for this cell. Use ${ input.foo } or ${ node.previous.output.bar } for workflow context."
		}), "Use ${ input.foo } or ${ node.previous.output.bar } for workflow context."));
	}
	function renderAgentTaskInputOutput(props) {
		var h = props.createElement;
		return h("div", { className: "hermes-workflows-stack" }, field({
			createElement: h,
			id: "wf-agent-prompt"
		}, "Agent cell prompt", h("textarea", {
			id: "wf-agent-prompt",
			className: "hermes-workflows-prompt-editor",
			value: props.promptText,
			onChange: function(event) {
				props.setPromptText(event.target.value);
			},
			placeholder: "Tell the assigned profile exactly what to do."
		})), field({
			createElement: h,
			id: "wf-agent-result-contract"
		}, "Result contract JSON", h("textarea", {
			id: "wf-agent-result-contract",
			className: "hermes-workflows-contract-editor",
			value: props.resultContractText,
			onChange: function(event) {
				props.setResultContractText(event.target.value);
			}
		}), "Reference prior outputs with ${ input.foo } or ${ node.previous.output.bar }."), h("button", {
			type: "button",
			onClick: function() {
				props.setPromptAssistantOpen(!props.promptAssistantOpen);
			}
		}, props.promptAssistantOpen ? "Hide Prompt assistant" : "Prompt assistant"), renderPromptAssistant(props));
	}
	function renderTriggerParameters(props) {
		var h = props.createElement;
		return h("div", { className: "hermes-workflows-stack" }, field({
			createElement: h,
			id: "wf-trigger-type"
		}, "Trigger type", h("select", {
			id: "wf-trigger-type",
			value: props.cellType || "manual",
			onChange: function(event) {
				props.setCellType(event.target.value);
			}
		}, SUPPORTED_TRIGGERS.map(function(type) {
			return h("option", {
				key: type,
				value: type
			}, type);
		}))), props.cellType === "schedule" ? field({
			createElement: h,
			id: "wf-trigger-schedule"
		}, "Schedule (cron)", h("input", {
			id: "wf-trigger-schedule",
			value: props.triggerSchedule,
			onChange: function(event) {
				props.setTriggerSchedule(event.target.value);
			},
			placeholder: "0 9 * * *"
		})) : null);
	}
	function renderTriggerInputOutput(props) {
		var h = props.createElement;
		var rows = asArray(props.triggerInputRows);
		return h("div", { className: "hermes-workflows-stack" }, sectionPanel(props, "trigger-input-schema", "Input schema", h("div", { className: "hermes-workflows-stack" }, h("p", { className: "hermes-workflows-muted" }, "Phase 1 supports scalar manual and continuous input items. Batch splitting and document uploads are not supported in this release."), rows.length ? h("ul", { className: "hermes-workflows-input-field-list" }, rows.map(function(row) {
			return h("li", {
				key: row.name,
				className: "hermes-workflows-input-field-row"
			}, h("span", { className: "hermes-workflows-input-field-name" }, safeString(row.name)), h("span", { className: "hermes-workflows-badge" }, safeString(row.kind)), h("span", { className: "hermes-workflows-meta" }, row.required ? "required" : "optional"), h("button", {
				type: "button",
				className: "hermes-workflows-link-btn",
				onClick: function() {
					props.removeTriggerInputField(row.name);
				}
			}, "Remove"));
		})) : h("p", { className: "hermes-workflows-muted" }, "No input fields yet. Add fields below, then Apply."), h("div", { className: "hermes-workflows-stack" }, field({
			createElement: h,
			id: "wf-trigger-input-name"
		}, "Input field name", h("input", {
			id: "wf-trigger-input-name",
			value: props.triggerInputName,
			onChange: function(event) {
				props.setTriggerInputName(event.target.value);
			},
			placeholder: "repo_path"
		})), field({
			createElement: h,
			id: "wf-trigger-input-kind"
		}, "Input field kind", h("select", {
			id: "wf-trigger-input-kind",
			value: props.triggerInputKind,
			onChange: function(event) {
				props.setTriggerInputKind(event.target.value);
			}
		}, SCALAR_INPUT_KINDS.map(function(kind) {
			return h("option", {
				key: kind,
				value: kind
			}, kind);
		}))), field({
			createElement: h,
			id: "wf-trigger-input-required"
		}, "Required", h("label", {
			htmlFor: "wf-trigger-input-required-checkbox",
			className: "hermes-workflows-checkbox-row"
		}, h("input", {
			id: "wf-trigger-input-required-checkbox",
			type: "checkbox",
			checked: props.triggerInputRequired,
			onChange: function(event) {
				props.setTriggerInputRequired(event.target.checked);
			}
		}), "Required input")), field({
			createElement: h,
			id: "wf-trigger-input-default"
		}, "Default value", h("input", {
			id: "wf-trigger-input-default",
			value: props.triggerInputDefault,
			onChange: function(event) {
				props.setTriggerInputDefault(event.target.value);
			},
			placeholder: "optional"
		})), fieldRow(props, "trigger-len", [field({
			createElement: h,
			id: "wf-trigger-input-min-length"
		}, "Min length", h("input", {
			id: "wf-trigger-input-min-length",
			type: "number",
			min: "0",
			value: props.triggerInputMinLength,
			onChange: function(event) {
				props.setTriggerInputMinLength(event.target.value);
			},
			placeholder: "0"
		})), field({
			createElement: h,
			id: "wf-trigger-input-max-length"
		}, "Max length", h("input", {
			id: "wf-trigger-input-max-length",
			type: "number",
			min: "0",
			value: props.triggerInputMaxLength,
			onChange: function(event) {
				props.setTriggerInputMaxLength(event.target.value);
			},
			placeholder: "optional"
		}))]), h("button", {
			type: "button",
			onClick: props.addTriggerInputFieldFromUi
		}, "Add input field")))), sectionPanel(props, "trigger-intake-mode", "Intake mode", h("div", { className: "hermes-workflows-stack" }, field({
			createElement: h,
			id: "wf-trigger-intake-mode"
		}, "Mode", h("select", {
			id: "wf-trigger-intake-mode",
			value: props.triggerIntakeMode,
			onChange: function(event) {
				props.setTriggerIntakeMode(event.target.value);
			}
		}, h("option", { value: "single" }, "single"), h("option", { value: "continuous" }, "continuous"))), field({
			createElement: h,
			id: "wf-trigger-dedupe-key"
		}, "Dedupe key", h("input", {
			id: "wf-trigger-dedupe-key",
			value: props.triggerDedupeKey,
			onChange: function(event) {
				props.setTriggerDedupeKey(event.target.value);
			},
			placeholder: "$.input.repo_path"
		})), field({
			createElement: h,
			id: "wf-trigger-ready-path"
		}, "Ready when field path", h("input", {
			id: "wf-trigger-ready-path",
			value: props.triggerReadyPath,
			onChange: function(event) {
				props.setTriggerReadyPath(event.target.value);
			},
			placeholder: "$.input.repo_path"
		})))));
	}
	function renderSwitchParameters(props) {
		var h = props.createElement;
		return h("div", { className: "hermes-workflows-stack" }, field({
			createElement: h,
			id: "wf-switch-default"
		}, "Default target cell", h("input", {
			id: "wf-switch-default",
			value: props.switchDefault,
			onChange: function(event) {
				props.setSwitchDefault(event.target.value);
			},
			placeholder: "Optional target id; or connect from switch.default"
		})), h("div", { className: "hermes-workflows-stack" }, h("h4", { className: "hermes-workflows-section-panel-title" }, "Switch cases"), props.switchCases.length ? h("ul", { className: "hermes-workflows-switch-case-list" }, props.switchCases.map(function(item, idx) {
			if (!item || !item.name) return null;
			return h("li", {
				key: item.name + "-" + idx,
				className: "hermes-workflows-switch-case-row"
			}, h("span", { className: "hermes-workflows-switch-case-name" }, safeString(item.name)), h("span", { className: "hermes-workflows-meta" }, safeString(item.path || "—") + " == " + safeString(item.equals || "—")), h("button", {
				type: "button",
				className: "hermes-workflows-link-btn",
				onClick: function() {
					var next = asArray(props.switchCases).filter(function(_, i) {
						return i !== idx;
					});
					props.setSwitchCases(next);
				}
			}, "Remove"));
		})) : h("p", { className: "hermes-workflows-muted" }, "No switch cases yet.")), h("div", { className: "hermes-workflows-stack" }, h("h4", { className: "hermes-workflows-section-panel-title" }, "Add switch case"), field({
			createElement: h,
			id: "wf-switch-case-name"
		}, "Case name", h("input", {
			id: "wf-switch-case-name",
			value: props.switchCaseName,
			onChange: function(event) {
				props.setSwitchCaseName(event.target.value);
			},
			placeholder: "Case name, e.g. approved"
		})), field({
			createElement: h,
			id: "wf-switch-case-path"
		}, "Path", h("input", {
			id: "wf-switch-case-path",
			value: props.switchCasePath,
			onChange: function(event) {
				props.setSwitchCasePath(event.target.value);
			},
			placeholder: "$.input.status"
		})), field({
			createElement: h,
			id: "wf-switch-case-equals"
		}, "Equals", h("input", {
			id: "wf-switch-case-equals",
			value: props.switchCaseEquals,
			onChange: function(event) {
				props.setSwitchCaseEquals(event.target.value);
			},
			placeholder: "Equals value"
		})), h("button", {
			type: "button",
			onClick: props.addSwitchCaseFromUi,
			className: "hermes-workflows-primary"
		}, "Add case")));
	}
	function renderSendMessageParameters(props) {
		var h = props.createElement;
		return h("div", { className: "hermes-workflows-stack" }, field({
			createElement: h,
			id: "wf-send-platform"
		}, "Platform", h("select", {
			id: "wf-send-platform",
			value: props.sendMessagePlatform || "auto",
			onChange: function(event) {
				props.setSendMessagePlatform(event.target.value);
			}
		}, [
			"auto",
			"discord",
			"telegram",
			"slack"
		].map(function(platform) {
			return h("option", {
				key: platform,
				value: platform
			}, platform);
		}))), field({
			createElement: h,
			id: "wf-send-target"
		}, "Target (channel/chat ID)", h("input", {
			id: "wf-send-target",
			value: props.sendMessageTarget || "",
			onChange: function(event) {
				props.setSendMessageTarget(event.target.value);
			},
			placeholder: "channel or chat ID"
		})), field({
			createElement: h,
			id: "wf-send-text"
		}, "Message text", h("textarea", {
			id: "wf-send-text",
			className: "hermes-workflows-prompt-editor",
			value: props.sendMessageText || "",
			onChange: function(event) {
				props.setSendMessageText(event.target.value);
			},
			placeholder: "Message to send"
		})));
	}
	function renderSendMessageInputOutput(props) {
		var h = props.createElement;
		return h("div", { className: "hermes-workflows-stack" }, sectionPanel(props, "send-preview", "Send preview", h("div", { className: "hermes-workflows-stack" }, h("p", null, h("strong", null, "Platform: "), safeString(props.sendMessagePlatform || "auto")), h("p", null, h("strong", null, "Target: "), safeString(props.sendMessageTarget || "—")), h("p", { className: "hermes-workflows-preview" }, safeString(props.sendMessageText || "")))));
	}
	function renderSubworkflowParameters(props) {
		var h = props.createElement;
		return h("div", { className: "hermes-workflows-stack" }, field({
			createElement: h,
			id: "wf-subworkflow-ref"
		}, "Workflow reference ID", h("input", {
			id: "wf-subworkflow-ref",
			value: props.subworkflowRef || "",
			onChange: function(event) {
				props.setSubworkflowRef(event.target.value);
			},
			placeholder: "workflow_id"
		})), field({
			createElement: h,
			id: "wf-subworkflow-input-mapping"
		}, "Input mapping JSON", h("textarea", {
			id: "wf-subworkflow-input-mapping",
			className: "hermes-workflows-contract-editor",
			value: props.subworkflowInputMappingText || "{}",
			onChange: function(event) {
				props.setSubworkflowInputMappingText(event.target.value);
			}
		})));
	}
	function renderWaitParameters(props) {
		var h = props.createElement;
		return h("div", { className: "hermes-workflows-stack" }, field({
			createElement: h,
			id: "wf-wait-seconds"
		}, "Wait seconds", h("input", {
			id: "wf-wait-seconds",
			type: "number",
			min: "0",
			value: props.cellSeconds,
			onChange: function(event) {
				props.setCellSeconds(event.target.value);
			},
			placeholder: "60"
		})));
	}
	function renderPassFailParameters(props, kind) {
		var h = props.createElement;
		return h("div", { className: "hermes-workflows-stack" }, field({
			createElement: h,
			id: "wf-cell-output-text"
		}, kind === "fail" ? "Failure message" : "Output text", h("textarea", {
			id: "wf-cell-output-text",
			className: "hermes-workflows-prompt-editor",
			value: props.cellOutputText,
			onChange: function(event) {
				props.setCellOutputText(event.target.value);
			},
			placeholder: kind === "fail" ? "Why this workflow should fail." : "Optional output text for this cell."
		})));
	}
	function renderPassFailInputOutput(props, kind) {
		var h = props.createElement;
		return h("div", { className: "hermes-workflows-stack" }, sectionPanel(props, "pass-fail-output", "Output preview", h("pre", { className: "hermes-workflows-preview" }, safeString(props.cellOutputText || ""))));
	}
	function renderMinimalParameters(props) {
		var h = props.createElement;
		return h("div", { className: "hermes-workflows-stack" }, h("p", { className: "hermes-workflows-muted" }, "Connect incoming and outgoing edges on the canvas."), field({
			createElement: h,
			id: "wf-cell-notes"
		}, "Notes", h("textarea", {
			id: "wf-cell-notes",
			className: "hermes-workflows-prompt-editor",
			value: props.promptText,
			onChange: function(event) {
				props.setPromptText(event.target.value);
			},
			placeholder: "Optional notes for this cell."
		})));
	}
	function renderMinimalInputOutput(props) {
		var h = props.createElement;
		return h("p", { className: "hermes-workflows-muted" }, "This cell passes its inputs through to its outputs.");
	}
	function renderTypeSelector(props, kind) {
		var h = props.createElement;
		if (props.selectedNode.specKind === "trigger") return null;
		return field({
			createElement: h,
			id: "wf-cell-type"
		}, h("span", null, "Cell type", h("select", {
			id: "wf-cell-type",
			value: props.cellType,
			"aria-label": "Change selected cell type",
			onChange: function(event) {
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
					props.setSelectedNode(Object.assign({}, nextNode, {
						id: props.selectedNode.id,
						specKind: props.selectedNode.specKind
					}));
					if (props.setNodeJson) props.setNodeJson(jsonBlock(Object.assign({}, nextNode, { id: props.selectedNode.id })));
				}
				if (props.setNodeMessage) props.setNodeMessage(preview.removedFields.length ? "Changed type to " + nextType + "; removed " + preview.removedFields.join(", ") + "." : "Changed type to " + nextType + ".");
			}
		}, SUPPORTED_NODES.map(function(type) {
			return h("option", {
				key: type,
				value: type
			}, type);
		}))), "Apply returns any removed fields of the previous type.");
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
		return h("div", { className: "hermes-workflows-stack" }, sectionPanel(props, "settings-id-type", "Identity", h("div", { className: "hermes-workflows-stack" }, field({
			createElement: h,
			id: "wf-cell-id"
		}, "Cell id", h("input", {
			id: "wf-cell-id",
			value: props.cellId,
			onChange: function(event) {
				props.setCellId(event.target.value);
			},
			placeholder: "cell-id"
		})), renderTypeSelector(props, kind), h("div", { className: "hermes-workflows-row" }, h("button", {
			type: "button",
			onClick: applyHandler,
			className: "hermes-workflows-primary"
		}, "Apply"), h("button", {
			type: "button",
			onClick: props.deleteSelectedCell,
			className: "hermes-workflows-danger"
		}, "Delete")))), sectionPanel(props, "settings-advanced", "Advanced JSON", h("div", { className: "hermes-workflows-stack" }, h("p", { className: "hermes-workflows-muted" }, "Edit the raw node JSON. Apply merges it into the workflow."), h("button", {
			type: "button",
			onClick: function() {
				props.setAdvancedJsonOpen(!props.advancedJsonOpen);
			}
		}, props.advancedJsonOpen ? "Hide JSON editor" : "Show JSON editor"), props.advancedJsonOpen ? renderAdvancedNodeJson(props, spec) : null)));
	}
	function renderTabs(props, activeTab, setActiveTab, kind) {
		var h = props.createElement;
		return h("div", {
			className: "hermes-workflows-tabs",
			role: "tablist",
			"aria-label": "Node configuration sections"
		}, [
			{
				key: "parameters",
				label: "Parameters"
			},
			{
				key: "input_output",
				label: "Input & Output"
			},
			{
				key: "settings",
				label: "Settings"
			}
		].map(function(t) {
			return h("button", {
				key: t.key,
				type: "button",
				role: "tab",
				id: "wf-tab-" + t.key,
				"aria-selected": activeTab === t.key ? "true" : "false",
				"aria-controls": "wf-tabpanel-" + t.key,
				className: "hermes-workflows-tab" + (activeTab === t.key ? " is-active" : ""),
				onClick: function() {
					setActiveTab(t.key);
				}
			}, t.label);
		}));
	}
	function renderInspector(props) {
		var h = props.createElement;
		if (!props.selectedNode) return h("aside", { className: "hermes-workflows-inspector" }, h("h3", null, "Node Configuration"), h("div", { className: "hermes-workflows-inspector-empty" }, h("p", null, "Select a node from the canvas to configure it."), h("p", { className: "hermes-workflows-muted" }, "Tip: press Add Node (N) to add a new cell from the rail.")));
		typeof props.activeSpec === "function" ? props.activeSpec() : props.activeSpec;
		var kind = props.selectedNode.specKind === "trigger" ? "trigger" : props.selectedNode.type || props.cellType || "pass";
		var activeTab = props.activeInspectorTab || "parameters";
		var setActiveTab = typeof props.setActiveInspectorTab === "function" ? props.setActiveInspectorTab : function() {};
		var closeInspector = typeof props.closeInspector === "function" ? props.closeInspector : function() {};
		var panelByTab = {
			parameters: {
				id: "wf-tabpanel-parameters",
				body: renderParametersTab(props, kind)
			},
			input_output: {
				id: "wf-tabpanel-input_output",
				body: renderInputOutputTab(props, kind)
			},
			settings: {
				id: "wf-tabpanel-settings",
				body: renderSettingsTab(props, kind)
			}
		};
		return h("aside", { className: "hermes-workflows-inspector" }, h("div", { className: "hermes-workflows-inspector-sticky-header" }, h("div", { className: "hermes-workflows-inspector-heading-row" }, h("h3", { className: "hermes-workflows-inspector-heading" }, "Node Configuration"), h("button", {
			type: "button",
			"aria-label": "Close configuration",
			className: "hermes-workflows-link-btn",
			onClick: closeInspector
		}, "Close")), h("div", { className: "hermes-workflows-inspector-meta-row" }, h("span", { className: "hermes-workflows-muted" }, safeString(props.selectedNode.id)), h("span", {
			className: "hermes-workflows-type-badge",
			style: { backgroundColor: NODE_COLORS[kind] || "#64748b" }
		}, kind)), props.nodeMessage ? h("p", {
			className: "hermes-workflows-node-message",
			role: "status",
			"aria-live": "polite"
		}, props.nodeMessage) : null), renderTabs(props, activeTab, setActiveTab, kind), Object.keys(panelByTab).map(function(tabKey) {
			var panel = panelByTab[tabKey];
			if (activeTab !== tabKey) return null;
			return h("section", {
				key: tabKey,
				role: "tabpanel",
				id: panel.id,
				"aria-labelledby": "wf-tab-" + tabKey,
				className: "hermes-workflows-tabpanel"
			}, panel.body);
		}));
	}
	//#endregion
	//#region ../plugins/workflows/dashboard/src/app.js
	(function() {
		"use strict";
		const SDK = window.__HERMES_PLUGIN_SDK__;
		const REG = window.__HERMES_PLUGINS__;
		if (!SDK || !REG || typeof REG.register !== "function" || !SDK.React || !SDK.fetchJSON) return;
		const React = SDK.React;
		const h = React.createElement;
		const Card = SDK.components && SDK.components.Card ? SDK.components.Card : "section";
		const FlowSDK = SDK.ReactFlow || SDK.reactFlow || {};
		const ReactFlow = FlowSDK.ReactFlow;
		const ReactFlowProvider = FlowSDK.ReactFlowProvider;
		const Controls = FlowSDK.Controls;
		FlowSDK.MiniMap;
		const MarkerType = FlowSDK.MarkerType || { ArrowClosed: "arrowclosed" };
		const addEdge = FlowSDK.addEdge;
		const applyNodeChanges = FlowSDK.applyNodeChanges;
		const applyEdgeChanges = FlowSDK.applyEdgeChanges;
		const API = "/api/plugins/workflows";
		const DEFINITIONS_API = "/api/plugins/workflows/definitions";
		const NODE_KIND_LIST = [
			"trigger",
			"pass",
			"switch",
			"agent_task",
			"wait",
			"parallel",
			"join",
			"fail",
			"send_message",
			"subworkflow"
		];
		const FALLBACK_IMPLEMENTED_TRIGGER_TYPES = ["manual", "schedule"];
		const FALLBACK_IMPLEMENTED_NODE_TYPES = [
			"pass",
			"switch",
			"agent_task",
			"wait",
			"parallel",
			"join",
			"fail"
		];
		const EXAMPLE_DEFINITION = [
			"id: dashboard_demo",
			"name: Dashboard Demo",
			"version: 1",
			"enabled: true",
			"triggers:",
			"  - id: manual",
			"    type: manual",
			"nodes:",
			"  start:",
			"    type: pass",
			"    output:",
			"      ok: true",
			"edges: []"
		].join("\n");
		const api = createApi(SDK.fetchJSON, API);
		const formatApiError$1 = formatApiError;
		function errorMessage(err) {
			return formatApiError(err);
		}
		const PRIVACY_NOTE = "Workflow inputs and outputs are stored locally in Hermes workflow/Kanban history. Do not paste secrets; common secret-looking keys are redacted in dashboard views.";
		function asArray(value) {
			return Array.isArray(value) ? value : [];
		}
		function safeString(value) {
			if (value === null || value === void 0 || value === "") return "—";
			return String(value);
		}
		function classSafe(value) {
			return String(value || "unknown").replace(/[^a-z0-9_-]+/gi, "-").toLowerCase();
		}
		function jsonBlock(value) {
			try {
				return JSON.stringify(value || {}, null, 2);
			} catch (_) {
				return String(value);
			}
		}
		function hasPreviewValue(value) {
			if (value === null || value === void 0) return false;
			if (typeof value === "string") return !!value.trim();
			if (Array.isArray(value)) return value.length > 0;
			if (typeof value === "object") return Object.keys(value).length > 0;
			return true;
		}
		function previewJson(value) {
			let text;
			try {
				text = JSON.stringify(value, null, 2);
			} catch (_) {
				text = String(value);
			}
			if (text === void 0) text = String(value);
			return text.length > 800 ? text.slice(0, 800) + "\n…" : text;
		}
		function parseJsonObject(text) {
			try {
				const parsed = JSON.parse(text);
				return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
			} catch (_) {
				return null;
			}
		}
		function initialExecutionIdFromLocation() {
			if (typeof URLSearchParams === "undefined" || !window.location) return "";
			return new URLSearchParams(window.location.search || "").get("execution") || "";
		}
		function currentLocationShape() {
			if (typeof window === "undefined" || !window.location) return {
				pathname: "",
				search: ""
			};
			return {
				pathname: window.location.pathname || "",
				search: window.location.search || ""
			};
		}
		function workflowIdFromLocation() {
			if (typeof window === "undefined" || !window.location) return "";
			const match = String(window.location.pathname || "").match(/^\/workflows\/([^\/]+)/);
			return match ? decodeURIComponent(match[1]) : "";
		}
		function pushMode(nextMode, selection) {
			if (typeof window === "undefined" || !window.history) return;
			if (nextMode === modeForLocation(currentLocationShape())) return;
			const target = locationForMode(workflowIdFromLocation() || "__draft__", nextMode, selection || {});
			if (typeof window.history.pushState === "function") window.history.pushState({ workspaceMode: nextMode }, "", target);
			else window.location.href = target;
		}
		function nodeList(spec) {
			const nodes = spec && spec.nodes ? spec.nodes : {};
			if (Array.isArray(nodes)) return nodes.map(function(node, index) {
				return Object.assign({
					id: node.id || node.name || "node_" + (index + 1),
					specKind: "node"
				}, node || {});
			});
			return Object.keys(nodes).map(function(id) {
				return Object.assign({
					id,
					specKind: "node"
				}, nodes[id] || {});
			});
		}
		function isTriggerSource(spec, sourceId) {
			const requested = String(sourceId || "");
			return graphItems(spec).some(function(item) {
				return item.specKind === "trigger" && item.id === requested;
			});
		}
		function splitPort(value) {
			const parts = safeString(value).split(".");
			return {
				nodeId: parts.shift() || "?",
				port: parts.join(".")
			};
		}
		function edgeList(spec) {
			return asArray(spec && spec.edges).map(function(edge, index) {
				const source = splitPort(edge.from || edge.source || edge.start || "?");
				const target = splitPort(edge.to || edge.target || edge.end || "?");
				return {
					id: edge.id || String(index + 1),
					from: source.nodeId,
					to: target.nodeId,
					label: edge.label || edge.condition || source.port || target.port || "",
					raw: edge
				};
			});
		}
		function promptObjectiveText(prompt) {
			if (typeof prompt === "string") return prompt;
			if (Array.isArray(prompt)) return prompt.map(promptObjectiveText).filter(Boolean).join("; ");
			if (prompt && typeof prompt === "object") return prompt.task || prompt.objective || prompt.title || prompt.goal || prompt.description || "";
			return "";
		}
		function inputFieldsForSpec(spec, preferredTrigger) {
			function kindForInputValue(value) {
				function kindForLiteral(literal) {
					if (typeof literal === "boolean") return "boolean";
					if (typeof literal === "number") return "number";
					if (literal && typeof literal === "object") return "json";
					return "text";
				}
				if (Array.isArray(value)) return "json";
				if (value && typeof value === "object") {
					const type = String(value.kind || value.type || "").toLowerCase();
					if ({
						text: true,
						long_text: true,
						document: true,
						prompt: true,
						criteria: true,
						url: true,
						repo_path: true,
						boolean: true,
						number: true,
						integer: true,
						object: true
					}[type]) return type === "object" ? "json" : type;
					if (["float"].indexOf(type) !== -1) return "number";
					if (["bool"].indexOf(type) !== -1) return "boolean";
					if (["array"].indexOf(type) !== -1) return "json";
					if (type === "string") return "text";
					if (value.default !== void 0) return kindForLiteral(value.default);
					if (value.example !== void 0) return kindForLiteral(value.example);
					return "json";
				}
				return kindForLiteral(value);
			}
			function triggerInputSchemaObject(trigger) {
				const schema = trigger && trigger.input_schema;
				if (!schema || typeof schema !== "object" || Array.isArray(schema)) return {};
				return schema;
			}
			function triggerInputObject(trigger) {
				const rawInput = trigger && trigger.input;
				if (!rawInput || typeof rawInput !== "object" || Array.isArray(rawInput)) return {};
				if (rawInput.type === "object") {
					if (rawInput.properties && typeof rawInput.properties === "object" && !Array.isArray(rawInput.properties)) return rawInput.properties;
					const schemaMetadataKeys = {
						type: true,
						required: true,
						additionalProperties: true,
						description: true,
						title: true,
						$schema: true
					};
					if (Object.keys(rawInput).every(function(key) {
						return schemaMetadataKeys[key];
					})) return {};
				}
				return rawInput;
			}
			function fieldsFromInputSchema(schema) {
				return Object.keys(schema).map(function(key) {
					const field = schema[key] && typeof schema[key] === "object" && !Array.isArray(schema[key]) ? schema[key] : {};
					return {
						name: key,
						kind: kindForInputValue(field),
						label: String(field.label || key),
						required: !!field.required,
						description: String(field.description || "")
					};
				});
			}
			const preferredSchema = triggerInputSchemaObject(preferredTrigger);
			if (Object.keys(preferredSchema).length) return fieldsFromInputSchema(preferredSchema);
			const triggers = asArray(spec && spec.triggers);
			const schemaTrigger = triggers.find(function(trigger) {
				if (!trigger || typeof trigger !== "object") return false;
				return String(trigger.type || trigger.trigger_type || "") === "manual" && Object.keys(triggerInputSchemaObject(trigger)).length;
			}) || triggers.find(function(trigger) {
				return Object.keys(triggerInputSchemaObject(trigger)).length;
			});
			if (schemaTrigger) return fieldsFromInputSchema(triggerInputSchemaObject(schemaTrigger));
			const inputTrigger = triggers.find(function(trigger) {
				if (!trigger || typeof trigger !== "object") return false;
				return String(trigger.type || trigger.trigger_type || "") === "manual" && Object.keys(triggerInputObject(trigger)).length;
			}) || triggers.find(function(trigger) {
				return Object.keys(triggerInputObject(trigger)).length;
			});
			const triggerInput = inputTrigger ? triggerInputObject(inputTrigger) : {};
			const keys = Object.keys(triggerInput);
			if (keys.length) return keys.map(function(key) {
				return {
					name: key,
					kind: kindForInputValue(triggerInput[key])
				};
			});
			const found = Object.create(null);
			function addFallbackInputField(key) {
				const rawKey = String(key || "");
				const parts = rawKey.split(".");
				const name = parts[0].replace(/\[.*$/, "");
				if (!name) return;
				const kind = parts.length > 1 || rawKey.indexOf("[") !== -1 ? "json" : "text";
				if (kind === "json" || !found[name]) found[name] = kind;
			}
			const text = JSON.stringify(spec || {});
			text.replace(/\$\{\s*input\.([a-zA-Z0-9_\-.\[\]]+)\s*\}/g, function(_, key) {
				addFallbackInputField(key);
				return "";
			});
			text.replace(/\$\.input\.([a-zA-Z0-9_\-.\[\]]+)/g, function(_, key) {
				addFallbackInputField(key);
				return "";
			});
			return Object.keys(found).sort().map(function(name) {
				return {
					name,
					kind: found[name]
				};
			});
		}
		function inputObjectForFields(fields, values) {
			const input = {};
			asArray(fields).forEach(function(field) {
				const name = field && field.name;
				if (!name) return;
				const raw = values && values[name];
				if (raw === void 0 || raw === null || raw === "") return;
				if (field.kind === "number" || field.kind === "integer") {
					const errorKind = field.kind === "integer" ? "integer" : "number";
					const trimmed = String(raw).trim();
					if (!trimmed) return;
					if (field.kind === "integer" && !/^-?\d+(?:[eE][+-]?\d+)?$/.test(trimmed)) throw new Error("Invalid integer for input field " + name);
					if (field.kind !== "integer" && !/^-?(?:\d+|\d*\.\d+)(?:[eE][+-]?\d+)?$/.test(trimmed)) throw new Error("Invalid number for input field " + name);
					const numberValue = Number(trimmed);
					if (!Number.isFinite(numberValue)) throw new Error("Invalid " + errorKind + " for input field " + name);
					if (numberValue === 0 && /[1-9]/.test(trimmed.replace(/[eE].*$/, ""))) throw new Error("Invalid " + errorKind + " for input field " + name);
					if (field.kind === "integer") {
						if (!Number.isInteger(numberValue) || !Number.isSafeInteger(numberValue)) throw new Error("Invalid integer for input field " + name);
					} else {
						const fractionMatch = trimmed.match(/\.(\d+)(?:[eE][+-]?\d+)?$/);
						if (fractionMatch && /[1-9]/.test(fractionMatch[1]) && Number.isInteger(numberValue) && Math.abs(numberValue) >= Number.MAX_SAFE_INTEGER / 10) throw new Error("Invalid number for input field " + name);
						if (Number.isInteger(numberValue) && !Number.isSafeInteger(numberValue)) throw new Error("Invalid number for input field " + name);
					}
					input[name] = numberValue;
					return;
				}
				if (field.kind === "boolean") {
					if (typeof raw === "boolean") {
						input[name] = raw;
						return;
					}
					const trimmed = String(raw).trim();
					if (!trimmed) return;
					if (trimmed === "true") {
						input[name] = true;
						return;
					}
					if (trimmed === "false") {
						input[name] = false;
						return;
					}
					throw new Error("Invalid boolean for input field " + name);
				}
				if (field.kind === "json") {
					if (raw && typeof raw === "object") {
						input[name] = raw;
						return;
					}
					const trimmed = String(raw).trim();
					if (!trimmed) return;
					try {
						input[name] = JSON.parse(trimmed);
					} catch (err) {
						throw new Error("Invalid JSON for input field " + name);
					}
					return;
				}
				input[name] = raw;
			});
			return input;
		}
		function hasPromptValue(prompt) {
			if (typeof prompt === "string") return !!prompt.trim();
			if (Array.isArray(prompt)) return prompt.length > 0;
			if (prompt && typeof prompt === "object") return Object.keys(prompt).length > 0;
			return !!prompt;
		}
		function hasProfileValue(profile) {
			return typeof profile === "string" && !!profile.trim();
		}
		function checklistNodeValues(spec) {
			const nodes = spec && spec.nodes;
			if (Array.isArray(nodes)) return [];
			if (nodes && typeof nodes === "object") return Object.keys(nodes).map(function(id) {
				return nodes[id] && typeof nodes[id] === "object" ? nodes[id] : {};
			});
			return [];
		}
		function checklistNodesShapeValid(spec) {
			const nodes = spec && spec.nodes;
			if (Array.isArray(nodes)) return false;
			if (nodes && typeof nodes === "object") return Object.keys(nodes).every(function(id) {
				const node = nodes[id];
				return node && typeof node === "object" && !Array.isArray(node);
			});
			return false;
		}
		function checklistNodeIdsValid(spec) {
			const nodes = spec && spec.nodes;
			if (!nodes || typeof nodes !== "object" || Array.isArray(nodes)) return false;
			return Object.keys(nodes).every(function(id) {
				return /^[a-z][a-z0-9_-]{0,63}$/.test(id);
			});
		}
		function checklistWorkflowIdValid(value) {
			return typeof value === "string" && /^[a-z][a-z0-9_-]{0,63}$/.test(value);
		}
		function hasStringValue(value) {
			return typeof value === "string" && !!value.trim();
		}
		function implementedTriggerTypesFromCapabilities(capabilities) {
			const values = capabilities && capabilities.triggers && capabilities.triggers.implemented;
			return Array.isArray(values) && values.length ? values : FALLBACK_IMPLEMENTED_TRIGGER_TYPES;
		}
		function implementedNodeTypesFromCapabilities(capabilities) {
			const values = capabilities && capabilities.nodes && capabilities.nodes.implemented;
			return Array.isArray(values) && values.length ? values : FALLBACK_IMPLEMENTED_NODE_TYPES;
		}
		function checklistVersionValid(version) {
			if (typeof version === "boolean" || Array.isArray(version)) return false;
			const value = Number(String(version).trim());
			return Number.isInteger(value) && value >= 1;
		}
		function checklistTriggersImplemented(spec, capabilities) {
			if (!spec || !Object.prototype.hasOwnProperty.call(spec, "triggers")) return true;
			const triggers = spec.triggers;
			if (!Array.isArray(triggers)) return false;
			const implementedTriggers = implementedTriggerTypesFromCapabilities(capabilities);
			return triggers.every(function(trigger) {
				if (!trigger || typeof trigger !== "object" || Array.isArray(trigger)) return false;
				if (typeof trigger.type !== "string") return false;
				return implementedTriggers.indexOf(trigger.type) !== -1;
			});
		}
		function checklistNodesImplemented(nodes, capabilities) {
			const implementedNodes = implementedNodeTypesFromCapabilities(capabilities);
			return !nodes.some(function(node) {
				if (!node || typeof node !== "object" || Array.isArray(node)) return true;
				if (typeof node.type !== "string") return true;
				return implementedNodes.indexOf(node.type) === -1;
			});
		}
		function checklistEdgesReferToKnownNodes(spec) {
			if (!spec || !Object.prototype.hasOwnProperty.call(spec, "edges")) return true;
			const edges = spec.edges;
			if (!Array.isArray(edges)) return false;
			if (!edges.length) return true;
			if (!checklistNodesShapeValid(spec) || !checklistNodeIdsValid(spec)) return false;
			const nodes = spec.nodes;
			function hasNode(id) {
				return Object.prototype.hasOwnProperty.call(nodes, id);
			}
			return edges.every(function(edge) {
				if (!edge || typeof edge !== "object" || Array.isArray(edge)) return false;
				const hasFrom = Object.prototype.hasOwnProperty.call(edge, "from");
				const hasFromAlias = Object.prototype.hasOwnProperty.call(edge, "from_");
				if (!hasFrom && !hasFromAlias) return false;
				if (!Object.prototype.hasOwnProperty.call(edge, "to")) return false;
				const source = String((hasFrom ? edge.from : edge.from_) || "").trim();
				const target = String(edge.to || "").trim();
				if (!source || !target || target.indexOf(".") !== -1 || !hasNode(target)) return false;
				let sourceBase = source;
				let branch = null;
				const dotIndex = source.indexOf(".");
				if (dotIndex !== -1) {
					sourceBase = source.slice(0, dotIndex);
					branch = source.slice(dotIndex + 1);
				}
				if (!sourceBase || !hasNode(sourceBase)) return false;
				const sourceType = nodes[sourceBase] && nodes[sourceBase].type;
				if (typeof sourceType !== "string") return false;
				if (branch === null) return sourceType !== "parallel";
				return !!branch && (sourceType === "switch" || sourceType === "parallel");
			});
		}
		function checklistGraphRulesValid(spec) {
			if (!spec || typeof spec !== "object" || Array.isArray(spec)) return false;
			const triggers = Object.prototype.hasOwnProperty.call(spec, "triggers") ? spec.triggers : [];
			if (!Array.isArray(triggers)) return false;
			for (let index = 0; index < triggers.length; index += 1) {
				const trigger = triggers[index];
				if (!trigger || typeof trigger !== "object" || Array.isArray(trigger)) return false;
				if (typeof trigger.type !== "string") return false;
				if (trigger.type === "schedule" && !hasStringValue(trigger.cron) && !hasStringValue(trigger.schedule) && !hasStringValue(trigger.expr)) return false;
			}
			if (!checklistNodesShapeValid(spec) || !checklistNodeIdsValid(spec)) return false;
			const nodes = spec.nodes;
			function hasNode(id) {
				return Object.prototype.hasOwnProperty.call(nodes, id);
			}
			const edges = Object.prototype.hasOwnProperty.call(spec, "edges") ? spec.edges : [];
			if (!Array.isArray(edges)) return false;
			const outgoingSources = {};
			for (let index = 0; index < edges.length; index += 1) {
				const edge = edges[index];
				if (!edge || typeof edge !== "object" || Array.isArray(edge)) return false;
				const hasFrom = Object.prototype.hasOwnProperty.call(edge, "from");
				const hasFromAlias = Object.prototype.hasOwnProperty.call(edge, "from_");
				if (!hasFrom && !hasFromAlias) return false;
				if (!Object.prototype.hasOwnProperty.call(edge, "to")) return false;
				const source = hasFrom ? edge.from : edge.from_;
				if (typeof source !== "string" || typeof edge.to !== "string") return false;
				let sourceBase = source;
				let branch = null;
				const dotIndex = source.indexOf(".");
				if (dotIndex !== -1) {
					sourceBase = source.slice(0, dotIndex);
					branch = source.slice(dotIndex + 1);
				}
				if (!hasNode(sourceBase) || !hasNode(edge.to)) return false;
				const sourceType = nodes[sourceBase].type;
				if (typeof sourceType !== "string") return false;
				if (branch === null && sourceType === "parallel") return false;
				if (branch !== null && (!branch || sourceType !== "switch" && sourceType !== "parallel")) return false;
				outgoingSources[sourceBase] = true;
			}
			return Object.keys(nodes).every(function(nodeId) {
				const node = nodes[nodeId];
				if (node.catch !== null && node.catch !== void 0) {
					if (typeof node.catch !== "string" || node.catch === nodeId || !hasNode(node.catch)) return false;
				}
				if (node.type === "switch") {
					if (node.default !== null && node.default !== void 0) return typeof node.default === "string" && hasNode(node.default);
					return !!outgoingSources[nodeId];
				}
				return true;
			});
		}
		function validationChecklist(spec, capabilities) {
			const safeNodes = checklistNodeValues(spec);
			return [
				{
					label: "Workflow id set",
					ok: checklistWorkflowIdValid(spec && spec.id)
				},
				{
					label: "Workflow name set",
					ok: hasStringValue(spec && spec.name)
				},
				{
					label: "Version set",
					ok: checklistVersionValid(spec && spec.version)
				},
				{
					label: "At least one node",
					ok: safeNodes.length > 0
				},
				{
					label: "Node definitions are objects",
					ok: checklistNodesShapeValid(spec)
				},
				{
					label: "Node ids are valid",
					ok: checklistNodeIdsValid(spec)
				},
				{
					label: "No unsupported triggers (implemented today)",
					ok: checklistTriggersImplemented(spec, capabilities)
				},
				{
					label: "No unsupported nodes (implemented today)",
					ok: checklistNodesImplemented(safeNodes, capabilities)
				},
				{
					label: "Edges refer to known nodes",
					ok: checklistEdgesReferToKnownNodes(spec)
				},
				{
					label: "Graph rules pass",
					ok: checklistGraphRulesValid(spec)
				},
				{
					label: "Agent cells have profile and prompt",
					ok: !safeNodes.some(function(node) {
						return node.type === "agent_task" && (!hasProfileValue(node.profile) || !hasPromptValue(node.prompt));
					})
				}
			];
		}
		function eventStatus(row) {
			const payload = row && row.payload || {};
			const kind = String(row && row.kind || "");
			if (payload.status) return payload.status;
			if (kind.indexOf("node_succeeded") !== -1) return "succeeded";
			if (kind.indexOf("node_failed") !== -1) return "failed";
			if (kind.indexOf("execution_blocked") !== -1) return "blocked";
			if (kind.indexOf("execution_waiting") !== -1) return "waiting";
			return "";
		}
		function statusByNode(events) {
			const statuses = {};
			asArray(events).forEach(function(row) {
				const payload = row && row.payload || {};
				const status = eventStatus(row);
				asArray(payload.waiting_nodes).forEach(function(nodeId) {
					statuses[nodeId] = "waiting";
				});
				const nodeId = payload.node_id || payload.nodeId || payload.node || payload.error && payload.error.node || row.node_id;
				if (nodeId && status) statuses[nodeId] = status;
			});
			return statuses;
		}
		const NODE_TYPES = {
			trigger: makeWorkflowNode("trigger", SDK),
			pass: makeWorkflowNode("pass", SDK),
			switch: makeWorkflowNode("switch", SDK),
			agent_task: makeWorkflowNode("agent_task", SDK),
			wait: makeWorkflowNode("wait", SDK),
			parallel: makeWorkflowNode("parallel", SDK),
			join: makeWorkflowNode("join", SDK),
			fail: makeWorkflowNode("fail", SDK),
			send_message: makeWorkflowNode("send_message", SDK),
			subworkflow: makeWorkflowNode("subworkflow", SDK)
		};
		function buildFlowNodes(spec, statuses, selectedNode, onSelect, nodePositions) {
			return decorateGraphItems(graphItems(spec), statuses || {}).map(function(item, index) {
				const id = item.id || item.spec.id || item.spec.name || "node_" + (index + 1);
				const rendererType = item.rendererType;
				const kind = NODE_TYPES[rendererType] ? rendererType : "pass";
				const pos = (nodePositions || {})[id] || {
					x: index % 3 * 250,
					y: Math.floor(index / 3) * 155
				};
				const legacyNode = item.specKind === "trigger" ? Object.assign({}, item.spec, {
					id,
					specKind: "trigger",
					type: "trigger",
					trigger_type: item.triggerType
				}) : Object.assign({}, item.spec, {
					id,
					specKind: "node",
					type: rendererType
				});
				return {
					id,
					type: kind,
					position: pos,
					className: "hermes-workflows-rf-node-shell is-status-" + classSafe(item.status) + (selectedNode && selectedNode.id === id ? " is-selected" : ""),
					data: {
						id,
						node: legacyNode,
						status: item.status,
						onSelect
					}
				};
			});
		}
		function buildFlowEdges(spec) {
			return edgeList(spec).map(function(edge) {
				return {
					id: edge.id,
					source: edge.from,
					target: edge.to,
					label: edge.label,
					markerEnd: { type: MarkerType.ArrowClosed }
				};
			});
		}
		function cleanedNodeForSpec(node) {
			const cleaned = Object.assign({}, node || {});
			const providerText = providerValue(cleaned).trim();
			const modelText = modelValue(cleaned).trim();
			delete cleaned.specKind;
			delete cleaned.trigger_type;
			delete cleaned.provider_override;
			delete cleaned.model_override;
			delete cleaned.provider;
			delete cleaned.model;
			if (cleaned.type === "agent_task") {
				if (providerText) cleaned.provider = providerText;
				if (modelText) cleaned.model = modelText;
			} else {
				delete cleaned.profile;
				delete cleaned.result_contract;
				delete cleaned.skills;
				delete cleaned.workspace_kind;
				delete cleaned.workspace_path;
				delete cleaned.goal_mode;
				delete cleaned.goal_max_turns;
				delete cleaned.max_retries;
			}
			if (cleaned.type !== "switch") {
				delete cleaned.cases;
				delete cleaned.default;
			}
			if (cleaned.type !== "wait") delete cleaned.seconds;
			return cleaned;
		}
		function cloneSpec(spec) {
			return JSON.parse(JSON.stringify(spec || {}));
		}
		function specToEditorText(spec) {
			return JSON.stringify(spec || {}, null, 2);
		}
		function findSpecNode(spec, nodeId) {
			if (!spec || !nodeId) return null;
			if (Array.isArray(spec.nodes)) return spec.nodes.find(function(node) {
				return (node.id || node.name) === nodeId;
			}) || null;
			return spec.nodes && spec.nodes[nodeId] ? Object.assign({ id: nodeId }, spec.nodes[nodeId]) : null;
		}
		function upsertSpecNode(spec, nodeId, nextNode) {
			const next = cloneSpec(spec);
			const clean = Object.assign({}, nextNode || {});
			const nextId = clean.id || nodeId;
			delete clean.id;
			if (!nextId) return next;
			if (Array.isArray(next.nodes)) {
				const nodeWithId = Object.assign({ id: nextId }, clean);
				const sourceIndex = next.nodes.findIndex(function(node) {
					return (node && (node.id || node.name)) === nodeId;
				});
				const targetIndex = next.nodes.findIndex(function(node) {
					return (node && (node.id || node.name)) === nextId;
				});
				const index = sourceIndex >= 0 ? sourceIndex : targetIndex;
				if (index >= 0) {
					next.nodes[index] = nodeWithId;
					next.nodes = next.nodes.filter(function(node, nodeIndex) {
						const id = node && (node.id || node.name);
						return nodeIndex === index || id !== nextId;
					});
				} else next.nodes.push(nodeWithId);
			} else {
				next.nodes = next.nodes || {};
				if (nodeId && nodeId !== nextId) delete next.nodes[nodeId];
				next.nodes[nextId] = clean;
			}
			if (nodeId && nodeId !== nextId) rewriteSpecNodeReferences(next, nodeId, nextId);
			return next;
		}
		function rewriteSpecNodeReferences(spec, oldId, newId) {
			const oldText = String(oldId || "");
			const newText = String(newId || "");
			if (!oldText || !newText || oldText === newText) return spec;
			spec.edges = asArray(spec.edges).map(function(edge) {
				const nextEdge = Object.assign({}, edge || {});
				["from", "source"].forEach(function(key) {
					if (typeof nextEdge[key] !== "string") return;
					if (nextEdge[key] === oldText) nextEdge[key] = newText;
					else if (nextEdge[key].indexOf(oldText + ".") === 0) nextEdge[key] = newText + nextEdge[key].slice(oldText.length);
				});
				["to", "target"].forEach(function(key) {
					if (nextEdge[key] === oldText) nextEdge[key] = newText;
				});
				return nextEdge;
			});
			Object.keys(spec.nodes || {}).forEach(function(key) {
				const node = spec.nodes[key];
				if (!node || typeof node !== "object") return;
				if (node.default === oldText) node.default = newText;
				if (node.catch === oldText) node.catch = newText;
			});
			return spec;
		}
		function upsertSpecEdge(spec, source, target) {
			const next = cloneSpec(spec);
			next.edges = asArray(next.edges);
			if (!next.edges.some(function(edge) {
				return (edge.from || edge.source) === source && (edge.to || edge.target) === target;
			}) && source && target) next.edges.push({
				from: source,
				to: target
			});
			return next;
		}
		function nodeIdsForSpec(spec) {
			return nodeList(spec).map(function(node) {
				return String(node.id || node.name || "");
			}).filter(Boolean);
		}
		function uniqueWorkflowId(spec, requested) {
			const existing = Object.create(null);
			nodeIdsForSpec(spec).forEach(function(id) {
				existing[id] = true;
			});
			asArray(spec && spec.triggers).forEach(function(trigger) {
				const id = trigger && (trigger.id || trigger.name);
				if (id) existing[String(id)] = true;
			});
			const base = workflowIdFromText(requested || "cell").replace(/_+/g, "_") || "cell";
			let candidate = base;
			let index = 2;
			while (existing[candidate]) {
				candidate = (base + "_" + index).slice(0, 64);
				index += 1;
			}
			return candidate;
		}
		function newWorkflowSpec(name) {
			const title = String(name || "Untitled Workflow").trim() || "Untitled Workflow";
			return {
				id: workflowIdFromText(title),
				name: title,
				version: 1,
				enabled: true,
				triggers: [{
					id: "manual",
					type: "manual"
				}],
				nodes: {},
				edges: []
			};
		}
		function defaultNodeForType(type, nodeId) {
			const safeType = NODE_KIND_LIST.indexOf(type) === -1 || type === "trigger" ? "pass" : type;
			const node = {
				type: safeType,
				title: String(nodeId || safeType || "cell").replace(/[_-]+/g, " ").trim() || safeType
			};
			if (safeType === "agent_task") {
				node.profile = "default";
				node.prompt = "Return JSON only matching the result contract.";
				node.result_contract = {
					summary: "string",
					status: "string"
				};
			} else if (safeType === "wait") node.seconds = 60;
			else if (safeType === "fail") node.output = "Workflow failed.";
			else if (safeType === "switch") node.cases = [];
			return node;
		}
		function addSpecNodeAfter(spec, nodeId, type, afterSource) {
			const next = cloneSpec(spec || newWorkflowSpec("Workflow Draft"));
			next.nodes = next.nodes || {};
			next.edges = asArray(next.edges);
			const id = uniqueWorkflowId(next, nodeId || type || "cell");
			const clean = defaultNodeForType(type || "pass", id);
			if (Array.isArray(next.nodes)) next.nodes.push(Object.assign({ id }, clean));
			else next.nodes[id] = clean;
			const source = String(afterSource || "").trim();
			if (source) return upsertSpecEdge(next, source, id);
			return next;
		}
		function addSpecTrigger(spec, triggerId, triggerType, scheduleText) {
			const next = cloneSpec(spec || newWorkflowSpec("Workflow Draft"));
			const type = triggerType === "schedule" || triggerType === "webhook" ? triggerType : "manual";
			const id = uniqueWorkflowId(next, triggerId || type || "trigger");
			const trigger = defaultTriggerForType(type, id);
			if (type === "schedule") trigger.schedule = String(scheduleText || trigger.schedule || "0 9 * * *").trim() || "0 9 * * *";
			next.triggers = asArray(next.triggers).filter(function(existing) {
				return String(existing && (existing.id || existing.name) || "") !== id;
			});
			next.triggers.push(trigger);
			return next;
		}
		function addSwitchCaseToSpec(spec, nodeId, caseName, path, equalsValue) {
			const next = cloneSpec(spec || {});
			const id = String(nodeId || "");
			const name = workflowIdFromText(caseName || "case");
			const comparePath = String(path || "$.input.status").trim() || "$.input.status";
			if (!id || !next.nodes || !next.nodes[id]) return next;
			const node = Object.assign({}, next.nodes[id], { type: "switch" });
			const caseSpec = {
				name,
				when: {
					op: "eq",
					left: { path: comparePath },
					right: String(equalsValue || name)
				}
			};
			node.cases = asArray(node.cases).filter(function(item) {
				return item && item.name !== name;
			}).concat([caseSpec]);
			next.nodes[id] = node;
			return next;
		}
		function removeSpecNode(spec, nodeId) {
			const next = cloneSpec(spec || {});
			const id = String(nodeId || "");
			if (!id) return next;
			if (Array.isArray(next.nodes)) next.nodes = next.nodes.filter(function(node) {
				return String(node && (node.id || node.name) || "") !== id;
			});
			else if (next.nodes && typeof next.nodes === "object") delete next.nodes[id];
			next.edges = asArray(next.edges).filter(function(edge) {
				const sourceBase = String(edge && (edge.from || edge.source) || "").split(".", 1)[0];
				const target = String(edge && (edge.to || edge.target) || "");
				return sourceBase !== id && target !== id;
			});
			Object.keys(next.nodes || {}).forEach(function(key) {
				const node = next.nodes[key];
				if (!node || typeof node !== "object") return;
				if (node.default === id) delete node.default;
				if (node.catch === id) delete node.catch;
			});
			return next;
		}
		function removeSpecTrigger(spec, triggerId) {
			const next = cloneSpec(spec || {});
			const id = String(triggerId || "");
			next.triggers = asArray(next.triggers).filter(function(trigger) {
				return String(trigger && (trigger.id || trigger.name) || "") !== id;
			});
			return next;
		}
		function providerValue(node) {
			return node && (node.provider || node.provider_override) ? String(node.provider || node.provider_override) : "";
		}
		function modelValue(node) {
			return node && (node.model || node.model_override) ? String(node.model || node.model_override) : "";
		}
		function WorkflowsPage() {
			const useState = React.useState;
			const useEffect = React.useEffect;
			const useRef = React.useRef;
			const flowInstanceRef = useRef(null);
			const canvasRef = useRef(null);
			const membershipKeyRef = useRef("");
			const stateNodePaletteOpen = useState(false);
			const nodePaletteOpen = stateNodePaletteOpen[0];
			const setNodePaletteOpen = stateNodePaletteOpen[1];
			const stateNodePaletteGroup = useState("");
			const nodePaletteGroup = stateNodePaletteGroup[0];
			const setNodePaletteGroup = stateNodePaletteGroup[1];
			const stateDefinitions = useState([]);
			const definitions = stateDefinitions[0];
			const setDefinitions = stateDefinitions[1];
			const stateExecutions = useState([]);
			const executions = stateExecutions[0];
			const setExecutions = stateExecutions[1];
			const stateWorkflowStatus = useState(null);
			const workflowStatus = stateWorkflowStatus[0];
			const setWorkflowStatus = stateWorkflowStatus[1];
			const stateWorkflowCapabilities = useState(null);
			const workflowCapabilities = stateWorkflowCapabilities[0];
			const setWorkflowCapabilities = stateWorkflowCapabilities[1];
			const stateSelectedDefinition = useState(null);
			const selectedDefinition = stateSelectedDefinition[0];
			const setSelectedDefinition = stateSelectedDefinition[1];
			const stateSelectedExecution = useState(null);
			const selectedExecution = stateSelectedExecution[0];
			const setSelectedExecution = stateSelectedExecution[1];
			const stateSelectedNode = useState(null);
			const selectedNode = stateSelectedNode[0];
			const setSelectedNode = stateSelectedNode[1];
			const stateNodeJson = useState("");
			const nodeJson = stateNodeJson[0];
			const setNodeJson = stateNodeJson[1];
			const statePromptText = useState("");
			const promptText = statePromptText[0];
			const setPromptText = statePromptText[1];
			const stateResultContractText = useState("{}");
			const resultContractText = stateResultContractText[0];
			const setResultContractText = stateResultContractText[1];
			const stateAgentProfile = useState("");
			const agentProfile = stateAgentProfile[0];
			const setAgentProfile = stateAgentProfile[1];
			const stateAgentProvider = useState("");
			const agentProvider = stateAgentProvider[0];
			const setAgentProvider = stateAgentProvider[1];
			const stateAgentModel = useState("");
			const agentModel = stateAgentModel[0];
			const setAgentModel = stateAgentModel[1];
			const stateAgentRoutingOptions = useState({
				profiles: [],
				providers: [],
				default_provider: "",
				default_model: ""
			});
			const agentRoutingOptions = stateAgentRoutingOptions[0];
			const setAgentRoutingOptions = stateAgentRoutingOptions[1];
			const stateAgentTitle = useState("");
			const agentTitle = stateAgentTitle[0];
			const setAgentTitle = stateAgentTitle[1];
			const stateCellId = useState("");
			const cellId = stateCellId[0];
			const setCellId = stateCellId[1];
			const stateCellType = useState("pass");
			const cellType = stateCellType[0];
			const setCellType = stateCellType[1];
			const stateTriggerInputRows = useState([]);
			const triggerInputRows = stateTriggerInputRows[0];
			const setTriggerInputRows = stateTriggerInputRows[1];
			const stateTriggerInputName = useState("");
			const triggerInputName = stateTriggerInputName[0];
			const setTriggerInputName = stateTriggerInputName[1];
			const stateTriggerInputKind = useState("text");
			const triggerInputKind = stateTriggerInputKind[0];
			const setTriggerInputKind = stateTriggerInputKind[1];
			const stateTriggerInputRequired = useState(true);
			const triggerInputRequired = stateTriggerInputRequired[0];
			const setTriggerInputRequired = stateTriggerInputRequired[1];
			const stateTriggerInputDefault = useState("");
			const triggerInputDefault = stateTriggerInputDefault[0];
			const setTriggerInputDefault = stateTriggerInputDefault[1];
			const stateTriggerInputMinLength = useState("");
			const triggerInputMinLength = stateTriggerInputMinLength[0];
			const setTriggerInputMinLength = stateTriggerInputMinLength[1];
			const stateTriggerInputMaxLength = useState("");
			const triggerInputMaxLength = stateTriggerInputMaxLength[0];
			const setTriggerInputMaxLength = stateTriggerInputMaxLength[1];
			const stateTriggerIntakeMode = useState("single");
			const triggerIntakeMode = stateTriggerIntakeMode[0];
			const setTriggerIntakeMode = stateTriggerIntakeMode[1];
			const stateTriggerDedupeKey = useState("");
			const triggerDedupeKey = stateTriggerDedupeKey[0];
			const setTriggerDedupeKey = stateTriggerDedupeKey[1];
			const stateTriggerReadyPath = useState("");
			const triggerReadyPath = stateTriggerReadyPath[0];
			const setTriggerReadyPath = stateTriggerReadyPath[1];
			const stateTriggerSchedule = useState("");
			const triggerSchedule = stateTriggerSchedule[0];
			const setTriggerSchedule = stateTriggerSchedule[1];
			const stateCellOutputText = useState("");
			const cellOutputText = stateCellOutputText[0];
			const setCellOutputText = stateCellOutputText[1];
			const stateSendMessagePlatform = useState("auto");
			const sendMessagePlatform = stateSendMessagePlatform[0];
			const setSendMessagePlatform = stateSendMessagePlatform[1];
			const stateSendMessageTarget = useState("");
			const sendMessageTarget = stateSendMessageTarget[0];
			const setSendMessageTarget = stateSendMessageTarget[1];
			const stateSendMessageText = useState("");
			const sendMessageText = stateSendMessageText[0];
			const setSendMessageText = stateSendMessageText[1];
			const stateSubworkflowRef = useState("");
			const subworkflowRef = stateSubworkflowRef[0];
			const setSubworkflowRef = stateSubworkflowRef[1];
			const stateSubworkflowInputMappingText = useState("{}");
			const subworkflowInputMappingText = stateSubworkflowInputMappingText[0];
			const setSubworkflowInputMappingText = stateSubworkflowInputMappingText[1];
			const stateCellSeconds = useState("60");
			const cellSeconds = stateCellSeconds[0];
			const setCellSeconds = stateCellSeconds[1];
			const stateSwitchDefault = useState("");
			const switchDefault = stateSwitchDefault[0];
			const setSwitchDefault = stateSwitchDefault[1];
			const stateSwitchCases = useState([]);
			const switchCases = stateSwitchCases[0];
			const setSwitchCases = stateSwitchCases[1];
			const stateSwitchCaseName = useState("");
			const switchCaseName = stateSwitchCaseName[0];
			const setSwitchCaseName = stateSwitchCaseName[1];
			const stateSwitchCasePath = useState("$.input.status");
			const switchCasePath = stateSwitchCasePath[0];
			const setSwitchCasePath = stateSwitchCasePath[1];
			const stateSwitchCaseEquals = useState("");
			const switchCaseEquals = stateSwitchCaseEquals[0];
			const setSwitchCaseEquals = stateSwitchCaseEquals[1];
			const stateNewWorkflowName = useState("");
			const newWorkflowName = stateNewWorkflowName[0];
			const setNewWorkflowName = stateNewWorkflowName[1];
			const stateNewTriggerSchedule = useState("0 9 * * *");
			const newTriggerSchedule = stateNewTriggerSchedule[0];
			stateNewTriggerSchedule[1];
			const stateAdvancedJsonOpen = useState(false);
			const advancedJsonOpen = stateAdvancedJsonOpen[0];
			const setAdvancedJsonOpen = stateAdvancedJsonOpen[1];
			const stateIsDragOver = useState(false);
			const isDragOver = stateIsDragOver[0];
			const setIsDragOver = stateIsDragOver[1];
			const stateNodePositions = useState({});
			const nodePositions = stateNodePositions[0];
			const setNodePositions = stateNodePositions[1];
			const stateContextMenu = useState({
				x: 0,
				y: 0,
				visible: false
			});
			const contextMenu = stateContextMenu[0];
			const setContextMenu = stateContextMenu[1];
			const statePromptAssistantOpen = useState(false);
			const promptAssistantOpen = statePromptAssistantOpen[0];
			const setPromptAssistantOpen = statePromptAssistantOpen[1];
			const statePromptAssistantAdvanced = useState(false);
			const promptAssistantAdvanced = statePromptAssistantAdvanced[0];
			const setPromptAssistantAdvanced = statePromptAssistantAdvanced[1];
			const statePromptAssistantGoal = useState("");
			const promptAssistantGoal = statePromptAssistantGoal[0];
			const setPromptAssistantGoal = statePromptAssistantGoal[1];
			const statePromptAssistantObjective = useState("");
			const promptAssistantObjective = statePromptAssistantObjective[0];
			const setPromptAssistantObjective = statePromptAssistantObjective[1];
			const statePromptAssistantContext = useState("${ input }\n${ node.previous.output }");
			const promptAssistantContext = statePromptAssistantContext[0];
			const setPromptAssistantContext = statePromptAssistantContext[1];
			const statePromptAssistantOutput = useState("{\"summary\":\"string\",\"status\":\"string\"}");
			const promptAssistantOutput = statePromptAssistantOutput[0];
			const setPromptAssistantOutput = statePromptAssistantOutput[1];
			const statePromptAssistantConstraints = useState("Return JSON only");
			const promptAssistantConstraints = statePromptAssistantConstraints[0];
			const setPromptAssistantConstraints = statePromptAssistantConstraints[1];
			const stateNodeMessage = useState("");
			const nodeMessage = stateNodeMessage[0];
			const setNodeMessage = stateNodeMessage[1];
			const stateFlowNodes = useState([]);
			const flowNodes = stateFlowNodes[0];
			const setFlowNodes = stateFlowNodes[1];
			const stateFlowEdges = useState([]);
			const flowEdges = stateFlowEdges[0];
			const setFlowEdges = stateFlowEdges[1];
			const stateEditorText = useState(EXAMPLE_DEFINITION);
			const editorText = stateEditorText[0];
			const setEditorText = stateEditorText[1];
			const stateDraftSpec = useState(null);
			const draftSpec = stateDraftSpec[0];
			const setDraftSpec = stateDraftSpec[1];
			const stateGoalText = useState("");
			const goalText = stateGoalText[0];
			const setGoalText = stateGoalText[1];
			const stateDraftResult = useState(null);
			const draftResult = stateDraftResult[0];
			const setDraftResult = stateDraftResult[1];
			const stateDrafting = useState(false);
			const drafting = stateDrafting[0];
			const setDrafting = stateDrafting[1];
			const stateRefineText = useState("");
			const refineText = stateRefineText[0];
			const setRefineText = stateRefineText[1];
			const stateRefining = useState(false);
			const refining = stateRefining[0];
			const setRefining = stateRefining[1];
			const stateSavedDraft = useState(null);
			const savedDraft = stateSavedDraft[0];
			const setSavedDraft = stateSavedDraft[1];
			const stateCandidateSource = useState(null);
			const candidateSource = stateCandidateSource[0];
			const setCandidateSource = stateCandidateSource[1];
			const stateUndoStack = useState([]);
			stateUndoStack[0];
			const setUndoStack = stateUndoStack[1];
			const draftApi = buildApiHelpers(api);
			const stateShowAdvancedYaml = useState(false);
			const showAdvancedYaml = stateShowAdvancedYaml[0];
			const setShowAdvancedYaml = stateShowAdvancedYaml[1];
			const stateWorkspaceMode = useState(modeForLocation(currentLocationShape()));
			const workspaceMode = stateWorkspaceMode[0];
			const setWorkspaceMode = stateWorkspaceMode[1];
			const stateDiagnosticsOpen = useState(false);
			const diagnosticsOpen = stateDiagnosticsOpen[0];
			const setDiagnosticsOpen = stateDiagnosticsOpen[1];
			const stateBottomCollapsed = useState(true);
			const bottomCollapsed = stateBottomCollapsed[0];
			const setBottomCollapsed = stateBottomCollapsed[1];
			const stateSidebarCollapsed = useState({});
			stateSidebarCollapsed[0];
			stateSidebarCollapsed[1];
			const stateRunWorkflowId = useState("");
			const runWorkflowId = stateRunWorkflowId[0];
			const setRunWorkflowId = stateRunWorkflowId[1];
			const stateRunInputText = useState("{}");
			const runInputText = stateRunInputText[0];
			const setRunInputText = stateRunInputText[1];
			const stateInputFieldValues = useState({});
			const inputFieldValues = stateInputFieldValues[0];
			const setInputFieldValues = stateInputFieldValues[1];
			const stateShowAdvancedInputJson = useState(false);
			const showAdvancedInputJson = stateShowAdvancedInputJson[0];
			const setShowAdvancedInputJson = stateShowAdvancedInputJson[1];
			const stateRunPanelOpen = useState(false);
			const runPanelOpen = stateRunPanelOpen[0];
			const setRunPanelOpen = stateRunPanelOpen[1];
			const runPanelOpenerRef = useRef(null);
			const runPanelRef = useRef(null);
			const [runFieldErrors, setRunFieldErrors] = useState({});
			const stateInputFeeds = useState([]);
			const inputFeeds = stateInputFeeds[0];
			const setInputFeeds = stateInputFeeds[1];
			const stateInputFeedItems = useState([]);
			const inputFeedItems = stateInputFeedItems[0];
			const setInputFeedItems = stateInputFeedItems[1];
			const stateSelectedFeedId = useState("");
			const selectedFeedId = stateSelectedFeedId[0];
			const setSelectedFeedId = stateSelectedFeedId[1];
			const stateFeedInputValues = useState({});
			const feedInputValues = stateFeedInputValues[0];
			const setFeedInputValues = stateFeedInputValues[1];
			const stateFeedInputText = useState("{}");
			const feedInputText = stateFeedInputText[0];
			const setFeedInputText = stateFeedInputText[1];
			const stateShowAdvancedFeedInputJson = useState(false);
			const showAdvancedFeedInputJson = stateShowAdvancedFeedInputJson[0];
			const setShowAdvancedFeedInputJson = stateShowAdvancedFeedInputJson[1];
			const stateFeedBusy = useState(false);
			const feedBusy = stateFeedBusy[0];
			const setFeedBusy = stateFeedBusy[1];
			const stateEvents = useState([]);
			const events = stateEvents[0];
			const setEvents = stateEvents[1];
			const stateNodeRuns = useState([]);
			const nodeRuns = stateNodeRuns[0];
			const setNodeRuns = stateNodeRuns[1];
			const stateStatus = useState("");
			const status = stateStatus[0];
			const setStatus = stateStatus[1];
			const stateError = useState("");
			const error = stateError[0];
			const setError = stateError[1];
			const stateLoading = useState(false);
			const loading = stateLoading[0];
			const setLoading = stateLoading[1];
			const stateValidating = useState(false);
			const validating = stateValidating[0];
			const setValidating = stateValidating[1];
			const stateDeploying = useState(false);
			const deploying = stateDeploying[0];
			const setDeploying = stateDeploying[1];
			const stateDeleting = useState(false);
			const deleting = stateDeleting[0];
			const setDeleting = stateDeleting[1];
			const stateRunning = useState(false);
			const running = stateRunning[0];
			const setRunning = stateRunning[1];
			const stateTicking = useState(false);
			const ticking = stateTicking[0];
			const setTicking = stateTicking[1];
			const initialExecutionId = initialExecutionIdFromLocation();
			useEffect(function() {
				if (!nodePaletteOpen || typeof document === "undefined") return void 0;
				function closeOnEscape(event) {
					if (event.key === "Escape") closeNodePalette();
				}
				document.addEventListener("keydown", closeOnEscape);
				return function() {
					document.removeEventListener("keydown", closeOnEscape);
				};
			}, [nodePaletteOpen]);
			function fail(err) {
				setError(errorMessage(err));
			}
			function clearBanners() {
				setError("");
				setStatus("");
			}
			function updateEditorText(text) {
				setEditorText(text);
				if (!parseJsonObject(text)) setDraftSpec(null);
			}
			function activeSpec() {
				return parseJsonObject(editorText) || draftSpec || null;
			}
			function openNodePalette(groupName = "") {
				setNodePaletteGroup(groupName);
				setNodePaletteOpen(true);
			}
			function closeNodePalette() {
				setNodePaletteOpen(false);
				setNodePaletteGroup("");
			}
			function checklistSpec() {
				return parseJsonObject(editorText) || draftSpec || null;
			}
			function workflowIdForSpec(spec) {
				return spec && String(spec.workflow_id || spec.id || "");
			}
			function versionForSpec(spec) {
				return spec && spec.version !== void 0 && spec.version !== null ? spec.version : null;
			}
			function versionQuery(version) {
				const value = version === void 0 || version === null ? "" : String(version);
				return value ? "?version=" + encodeURIComponent(value) : "";
			}
			function workflowIdForDefinition(definition) {
				return definition && String(definition.workflow_id || definition.id || "");
			}
			function versionForDefinition(definition) {
				return definition && definition.version !== void 0 && definition.version !== null ? String(definition.version) : "";
			}
			function definitionSelectionKey(definition) {
				const id = workflowIdForDefinition(definition);
				return id ? id + ":" + versionForDefinition(definition) : "";
			}
			function runInputSpec() {
				const selectedId = workflowIdForDefinition(selectedDefinition);
				if (selectedDefinition && selectedId === String(runWorkflowId || "")) return selectedDefinition.spec || null;
				const spec = activeSpec();
				if (spec && workflowIdForSpec(spec) === String(runWorkflowId || "")) return spec;
				return null;
			}
			function selectedInputTrigger(spec) {
				return asArray(spec && spec.triggers).find(function(trigger) {
					return trigger && String(trigger.type || trigger.trigger_type || "") === "manual" && trigger.intake && trigger.intake.mode === "continuous";
				}) || null;
			}
			function loadInputFeedItems(feedId) {
				if (!feedId) {
					setInputFeedItems([]);
					return Promise.resolve([]);
				}
				return api("/input-feeds/" + encodeURIComponent(feedId) + "/items").then(function(res) {
					const rows = asArray(res.items);
					setInputFeedItems(rows);
					return rows;
				}).catch(function() {
					setInputFeedItems([]);
					return [];
				});
			}
			function loadInputFeeds(workflowId) {
				const id = workflowId || workflowIdForDefinition(selectedDefinition);
				if (!id) {
					setInputFeeds([]);
					setSelectedFeedId("");
					setInputFeedItems([]);
					return Promise.resolve([]);
				}
				return api("/input-feeds?workflow_id=" + encodeURIComponent(id)).then(function(res) {
					const rows = asArray(res.feeds);
					const nextFeedId = rows.some(function(feed) {
						return feed.feed_id === selectedFeedId;
					}) ? selectedFeedId : rows[0] && rows[0].feed_id ? rows[0].feed_id : "";
					setInputFeeds(rows);
					setSelectedFeedId(nextFeedId);
					return nextFeedId ? loadInputFeedItems(nextFeedId).then(function() {
						return rows;
					}) : rows;
				}).catch(function() {
					setInputFeeds([]);
					setSelectedFeedId("");
					setInputFeedItems([]);
					return [];
				});
			}
			function openContinuousFeed() {
				const definition = selectedDefinition || {};
				const workflowId = workflowIdForDefinition(definition);
				const trigger = selectedInputTrigger(definition.spec);
				if (!workflowId) {
					setError("Deploy or select a workflow before opening an input feed.");
					return;
				}
				if (!trigger) {
					setError("This workflow has no manual trigger with intake.mode: continuous.");
					return;
				}
				setFeedBusy(true);
				setError("");
				api("/definitions/" + encodeURIComponent(workflowId) + "/input-feeds" + versionQuery(definition.version), {
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({ trigger_id: trigger && (trigger.id || trigger.name) || null })
				}).then(function(res) {
					const feed = res.feed || {};
					setSelectedFeedId(feed.feed_id || "");
					setStatus("Opened continuous input feed " + safeString(feed.feed_id));
					return loadInputFeeds(workflowId);
				}).catch(fail).finally(function() {
					setFeedBusy(false);
				});
			}
			function setSelectedFeedStatus(nextStatus) {
				const feedId = selectedFeedId || inputFeeds[0] && inputFeeds[0].feed_id;
				if (!feedId) return;
				setFeedBusy(true);
				api("/input-feeds/" + encodeURIComponent(feedId) + "/status", {
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({ status: nextStatus })
				}).then(function() {
					setStatus("Set input feed " + safeString(feedId) + " to " + safeString(nextStatus));
					return loadInputFeeds();
				}).catch(fail).finally(function() {
					setFeedBusy(false);
				});
			}
			function manualTick() {
				setTicking(true);
				setError("");
				api("/tick", {
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({ limit: 1 })
				}).then(function(res) {
					var parts = [];
					if (res.schedules_admitted) parts.push(res.schedules_admitted + " schedule(s)");
					if (res.feed_items_admitted) parts.push(res.feed_items_admitted + " feed item(s)");
					if (res.executions_advanced) parts.push(res.executions_advanced + " execution(s)");
					var detail = parts.length ? " (" + parts.join(", ") + ")" : "";
					setStatus("Manual tick processed " + safeString(res.processed || 0) + " workflow(s)" + detail + ".");
					return Promise.all([loadExecutions(), loadInputFeeds()]);
				}).catch(fail).finally(function() {
					setTicking(false);
				});
			}
			function updateInputFeedItem(item) {
				const itemId = item && item.item_id;
				const itemStatus = safeString(item && item.status);
				const feedId = selectedFeedId || inputFeeds[0] && inputFeeds[0].feed_id;
				if (!itemId || ["needs_input", "queued"].indexOf(itemStatus) === -1) return;
				const text = window.prompt("Update input item JSON", jsonBlock(item && item.input || {}));
				if (text === null) return;
				let input = {};
				try {
					input = JSON.parse(text || "{}");
				} catch (err) {
					fail(err);
					return;
				}
				setFeedBusy(true);
				setError("");
				api("/input-items/" + encodeURIComponent(itemId), {
					method: "PATCH",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({ input })
				}).then(function(res) {
					const updated = res.item || {};
					setStatus("Updated input item " + safeString(updated.item_id || itemId) + " (" + safeString(updated.status) + ").");
					return Promise.all([loadInputFeeds(), loadInputFeedItems(feedId)]);
				}).catch(fail).finally(function() {
					setFeedBusy(false);
				});
			}
			function addItemToFeed(event) {
				if (event) event.preventDefault();
				const feedId = selectedFeedId || inputFeeds[0] && inputFeeds[0].feed_id;
				if (!feedId) {
					setError("Open a continuous input feed before adding items.");
					return;
				}
				let input = {};
				try {
					const spec = runInputSpec() || selectedDefinition && selectedDefinition.spec;
					input = showAdvancedFeedInputJson ? JSON.parse(feedInputText || "{}") : inputObjectForFields(inputFieldsForSpec(spec, selectedInputTrigger(spec)), feedInputValues);
				} catch (err) {
					fail(err);
					return;
				}
				setFeedBusy(true);
				setError("");
				api("/input-feeds/" + encodeURIComponent(feedId) + "/items", {
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({ input })
				}).then(function(res) {
					const item = res.item || {};
					setStatus("Added input item " + safeString(item.item_id) + " (" + safeString(item.status) + ").");
					setFeedInputValues({});
					setFeedInputText("{}");
					return loadInputFeeds();
				}).catch(fail).finally(function() {
					setFeedBusy(false);
				});
			}
			function selectNodeForInspector(item) {
				let node = item;
				if (item && item.specKind && item.spec && typeof item.spec === "object") node = item.specKind === "trigger" ? Object.assign({}, item.spec, {
					id: item.id,
					specKind: "trigger",
					type: "trigger",
					trigger_type: item.triggerType
				}) : Object.assign({}, item.spec, {
					id: item.id,
					specKind: "node",
					type: item.rendererType
				});
				setSelectedNode(node);
				setNodeJson(jsonBlock(node));
				setNodeMessage("");
				setAdvancedJsonOpen(false);
				setPromptAssistantOpen(false);
				const rawPrompt = node ? node.prompt : void 0;
				const assistantOutput = node && node.result_contract ? jsonBlock(node.result_contract) : "{\"summary\":\"string\",\"status\":\"string\"}";
				setCellId(node && (node.id || node.name) ? String(node.id || node.name) : "");
				setCellType(node && node.specKind === "trigger" ? String(node.trigger_type || node.type || "manual") : String(node && node.type || "pass"));
				setTriggerInputRows(node && node.specKind === "trigger" ? inputRowsFromTrigger(node) : []);
				setTriggerInputName("");
				setTriggerInputKind("text");
				setTriggerInputRequired(true);
				setTriggerInputDefault("");
				setTriggerInputMinLength("");
				setTriggerInputMaxLength("");
				setTriggerIntakeMode(node && node.specKind === "trigger" && node.intake && node.intake.mode === "continuous" ? "continuous" : "single");
				setTriggerDedupeKey(node && node.specKind === "trigger" && node.intake && node.intake.dedupe_key ? String(node.intake.dedupe_key) : "");
				setTriggerReadyPath(node && node.specKind === "trigger" ? readyPathFromTrigger(node) : "");
				setTriggerSchedule(node && (node.schedule || node.cron || node.expr) ? String(node.schedule || node.cron || node.expr) : "");
				setCellOutputText(node && node.output !== void 0 && node.output !== null ? typeof node.output === "string" ? node.output : jsonBlock(node.output) : "");
				setSendMessagePlatform(node && node.platform ? String(node.platform) : "auto");
				setSendMessageTarget(node && node.target ? String(node.target) : "");
				setSendMessageText(node && node.message_text !== void 0 && node.message_text !== null ? String(node.message_text) : "");
				setSubworkflowRef(node && node.workflow_ref ? String(node.workflow_ref) : "");
				setSubworkflowInputMappingText(jsonBlock(node && node.input_mapping || {}));
				setCellSeconds(node && node.seconds !== void 0 && node.seconds !== null ? String(node.seconds) : "60");
				setSwitchDefault(node && node.default ? String(node.default) : "");
				setSwitchCases(asArray(node && node.cases));
				setSwitchCaseName("");
				setSwitchCaseEquals("");
				setAgentProfile(node && node.profile ? String(node.profile) : "");
				setAgentProvider(providerValue(node));
				setAgentModel(modelValue(node));
				setAgentTitle(node && node.title ? String(node.title) : "");
				setPromptText(rawPrompt === null || rawPrompt === void 0 ? "" : typeof rawPrompt === "string" ? rawPrompt : jsonBlock(rawPrompt));
				setResultContractText(jsonBlock(node && node.result_contract || {}));
				setPromptAssistantObjective(node && node.title ? String(node.title) : "");
				setPromptAssistantContext("${ input }\n${ node.previous.output }");
				setPromptAssistantOutput(assistantOutput);
				setPromptAssistantConstraints("Return JSON only");
			}
			function loadDefinition(workflowId, version) {
				if (!workflowId) {
					setSelectedDefinition(null);
					setDraftSpec(null);
					setSelectedNode(null);
					return Promise.resolve(null);
				}
				const previousSelectionKey = definitionSelectionKey(selectedDefinition);
				return api("/definitions/" + encodeURIComponent(workflowId) + versionQuery(version)).then(function(res) {
					const definition = res.definition || null;
					const nextSelectionKey = definitionSelectionKey(definition);
					const nextSpec = definition && definition.spec ? definition.spec : null;
					setSelectedDefinition(definition);
					const dirty = isDraftDirty({
						savedDraft,
						workingDraft: draftSpec
					});
					setSavedDraft(nextSpec);
					if (!dirty) setDraftSpec(nextSpec);
					if (nextSelectionKey !== previousSelectionKey) {
						setInputFieldValues({});
						setShowAdvancedInputJson(false);
						setRunInputText("{}");
					}
					setDraftResult(null);
					setCandidateSource(null);
					setRefineText("");
					if (!dirty && nextSpec) updateEditorText(specToEditorText(nextSpec));
					setSelectedNode(null);
					if (definition) {
						setRunWorkflowId(definition.workflow_id || definition.id || workflowId);
						loadInputFeeds(definition.workflow_id || definition.id || workflowId);
					} else loadInputFeeds("");
					return definition;
				});
			}
			function loadEvents(executionId) {
				if (!executionId) {
					setEvents([]);
					return Promise.resolve([]);
				}
				return api("/executions/" + encodeURIComponent(executionId) + "/events").then(function(res) {
					const rows = asArray(res.events);
					setEvents(rows);
					return rows;
				});
			}
			function loadNodeRuns(executionId) {
				if (!executionId) {
					setNodeRuns([]);
					return Promise.resolve([]);
				}
				return api("/executions/" + encodeURIComponent(executionId) + "/node-runs").then(function(res) {
					const rows = asArray(res.node_runs);
					setNodeRuns(rows);
					return rows;
				}).catch(function() {
					setNodeRuns([]);
					return [];
				});
			}
			function loadExecution(executionId) {
				if (!executionId) {
					setSelectedExecution(null);
					setEvents([]);
					setNodeRuns([]);
					return Promise.resolve(null);
				}
				return api(detailUrl(executionId)).then(function(res) {
					const execution = res.execution || null;
					setSelectedExecution(execution);
					setEvents(asArray(res.events));
					setNodeRuns(asArray(res.node_runs));
					return execution;
				}).catch(function() {
					return api("/executions/" + encodeURIComponent(executionId)).then(function(res) {
						const execution = res.execution || null;
						setSelectedExecution(execution);
						return Promise.all([loadEvents(executionId), loadNodeRuns(executionId)]).then(function() {
							return execution;
						});
					});
				});
			}
			function loadDefinitions(preferId, preferVersion) {
				return SDK.fetchJSON(DEFINITIONS_API).then(function(res) {
					const rows = asArray(res.definitions);
					const currentId = workflowIdForDefinition(selectedDefinition);
					const currentVersion = selectedDefinition && selectedDefinition.version;
					const first = rows[0] || null;
					function hasDefinition(id) {
						return !!id && rows.some(function(definition) {
							return workflowIdForDefinition(definition) === String(id);
						});
					}
					const preferredId = hasDefinition(preferId) ? preferId : "";
					const selectedId = hasDefinition(currentId) ? currentId : "";
					const runId = hasDefinition(runWorkflowId) ? runWorkflowId : "";
					const nextId = preferredId || selectedId || runId || workflowIdForDefinition(first) || "";
					let nextVersion = preferredId ? preferVersion : selectedId ? currentVersion : void 0;
					if ((nextVersion === void 0 || nextVersion === null || nextVersion === "") && nextId) {
						const matches = rows.filter(function(definition) {
							return workflowIdForDefinition(definition) === String(nextId);
						});
						const match = matches[matches.length - 1];
						if (match) nextVersion = match.version;
					}
					setDefinitions(rows);
					if (nextId) return loadDefinition(nextId, nextVersion);
					setRunWorkflowId("");
					setSelectedDefinition(null);
					setDraftSpec(null);
					setSelectedNode(null);
					return null;
				});
			}
			function loadExecutions(preferId, opts) {
				var filterOpts = opts && typeof opts === "object" ? opts : {};
				var workflowId = (runWorkflowId || "").trim();
				if (workflowId && !filterOpts.workflowId) filterOpts.workflowId = workflowId;
				return api(historyListPath(filterOpts)).then(function(res) {
					const rows = asArray(res.executions);
					const currentId = selectedExecution && selectedExecution.execution_id;
					function hasExecution(id) {
						return !!id && rows.some(function(execution) {
							return String(execution.execution_id || execution.id || "") === String(id);
						});
					}
					const nextId = (hasExecution(preferId) ? preferId : "") || (hasExecution(currentId) ? currentId : "") || rows[0] && rows[0].execution_id || "";
					setExecutions(rows);
					if (nextId) return loadExecution(nextId);
					setSelectedExecution(null);
					setEvents([]);
					setNodeRuns([]);
					return null;
				});
			}
			function loadWorkflowStatus() {
				return api("/status").then(setWorkflowStatus).catch(function() {
					setWorkflowStatus(null);
				});
			}
			function loadWorkflowCapabilities() {
				return api("/capabilities").then(setWorkflowCapabilities).catch(function() {
					setWorkflowCapabilities(null);
				});
			}
			function loadAgentRoutingOptions() {
				return api("/agent-routing-options").then(function(res) {
					setAgentRoutingOptions(res || {
						profiles: [],
						providers: [],
						default_provider: "",
						default_model: ""
					});
				}).catch(function() {
					setAgentRoutingOptions({
						profiles: [],
						providers: [],
						default_provider: "",
						default_model: ""
					});
				});
			}
			function refresh(preferExecutionId) {
				setLoading(true);
				setError("");
				loadWorkflowStatus();
				loadWorkflowCapabilities();
				loadAgentRoutingOptions();
				return Promise.all([loadDefinitions(), loadExecutions(preferExecutionId)]).catch(fail).finally(function() {
					setLoading(false);
				});
			}
			useEffect(function() {
				refresh(initialExecutionId);
			}, []);
			useEffect(function() {
				function handlePopState() {
					const next = modeForLocation(currentLocationShape());
					setWorkspaceMode(WORKSPACE_MODES.indexOf(next) === -1 ? "build" : next);
				}
				if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
					window.addEventListener("popstate", handlePopState);
					return function() {
						window.removeEventListener("popstate", handlePopState);
					};
				}
			}, []);
			useEffect(function() {
				const spec = activeSpec();
				const statuses = statusByNode(events);
				setFlowNodes(spec ? buildFlowNodes(spec, statuses, selectedNode, selectNodeForInspector, nodePositions) : []);
				setFlowEdges(spec ? buildFlowEdges(spec) : []);
			}, [
				draftSpec,
				editorText,
				events,
				selectedNode,
				nodePositions
			]);
			useEffect(function() {
				const key = flowNodes.map(function(item) {
					return item.data.specKind + ":" + item.id;
				}).join("|");
				if (!key || key === membershipKeyRef.current) return void 0;
				const frame = requestAnimationFrame(function() {
					const instance = flowInstanceRef.current;
					if (!instance || typeof instance.fitView !== "function") return;
					instance.fitView();
					membershipKeyRef.current = key;
				});
				return function() {
					cancelAnimationFrame(frame);
				};
			}, [flowNodes]);
			useEffect(function() {
				if (!error && !status) return void 0;
				const timer = setTimeout(clearBanners, error ? 12e3 : 6e3);
				return function() {
					clearTimeout(timer);
				};
			}, [error, status]);
			useEffect(function() {
				const executionId = selectedExecution && selectedExecution.execution_id;
				const executionStatus = selectedExecution && selectedExecution.status;
				if (!executionId || executionStatus !== "queued" && executionStatus !== "waiting") return void 0;
				const timer = setInterval(function() {
					loadExecution(executionId).catch(function() {});
					api("/executions").then(function(res) {
						setExecutions(asArray(res.executions));
					}).catch(function() {});
				}, 5e3);
				return function() {
					clearInterval(timer);
				};
			}, [selectedExecution && selectedExecution.execution_id, selectedExecution && selectedExecution.status]);
			useEffect(function() {
				const feedId = selectedFeedId || inputFeeds[0] && inputFeeds[0].feed_id;
				if (!feedId) return void 0;
				if (!shouldPollFeed(inputFeedItems)) return void 0;
				var cancelled = false;
				function poll() {
					if (cancelled) return;
					loadInputFeedItems(feedId).then(function() {
						if (cancelled) return;
						setTimeout(poll, 2e3);
					}).catch(function() {
						if (cancelled) return;
						setTimeout(poll, 2e3);
					});
				}
				var timer = setTimeout(poll, 2e3);
				return function() {
					cancelled = true;
					clearTimeout(timer);
				};
			}, [
				selectedFeedId,
				inputFeeds[0] && inputFeeds[0].feed_id,
				inputFeedItems.length,
				inputFeedItems.map(function(i) {
					return i.status;
				}).join(",")
			]);
			useEffect(function() {
				if (!runPanelOpen) return void 0;
				runPanelOpenerRef.current = document.activeElement;
				var panel = runPanelRef.current;
				if (panel) {
					var first = panel.querySelector("button, [href], input, select, textarea, [tabindex]:not([tabindex=\"-1\"])");
					if (first) first.focus();
				}
				function onKeyDown(event) {
					if (event.key === "Escape") {
						setRunPanelOpen(false);
						return;
					}
					if (event.key !== "Tab" || !panel) return;
					var focusable = panel.querySelectorAll("button, [href], input, select, textarea, [tabindex]:not([tabindex=\"-1\"])");
					if (!focusable.length) return;
					var firstEl = focusable[0];
					var lastEl = focusable[focusable.length - 1];
					if (event.shiftKey && document.activeElement === firstEl) {
						event.preventDefault();
						lastEl.focus();
					} else if (!event.shiftKey && document.activeElement === lastEl) {
						event.preventDefault();
						firstEl.focus();
					}
				}
				document.addEventListener("keydown", onKeyDown);
				return function() {
					document.removeEventListener("keydown", onKeyDown);
					if (runPanelOpenerRef.current && runPanelOpenerRef.current.focus) runPanelOpenerRef.current.focus();
				};
			}, [runPanelOpen]);
			function validateDefinition() {
				setValidating(true);
				setError("");
				api("/definitions/validate", {
					method: "POST",
					headers: { "Content-Type": "text/plain" },
					body: editorText
				}).then(function(res) {
					const definition = res.definition || {};
					setStatus("Validated " + safeString(definition.workflow_id || definition.id));
					setDraftSpec(definition.spec || null);
					if (definition.spec) updateEditorText(specToEditorText(definition.spec));
				}).catch(fail).finally(function() {
					setValidating(false);
				});
			}
			function deployDefinition() {
				setDeploying(true);
				setError("");
				api("/definitions/deploy", {
					method: "POST",
					headers: { "Content-Type": "text/plain" },
					body: editorText
				}).then(function(res) {
					const definition = res.definition || {};
					const id = definition.workflow_id || definition.id || "";
					const version = definition.version;
					setDraftSpec(definition.spec || null);
					setStatus("Deployed " + safeString(id) + ". Use Run to provide start input and launch an execution.");
					return loadDefinitions(id, version);
				}).catch(fail).finally(function() {
					setDeploying(false);
				});
			}
			function deleteWorkflow() {
				const workflowId = workflowIdForDefinition(selectedDefinition);
				if (!workflowId) return;
				const name = selectedDefinition && selectedDefinition.name ? selectedDefinition.name : workflowId;
				if (!confirm("Delete workflow \"" + safeString(name) + "\"? This deletes its versions, schedules, and execution history.")) return;
				setDeleting(true);
				setError("");
				api("/definitions/" + encodeURIComponent(workflowId), { method: "DELETE" }).then(function() {
					setSelectedDefinition(null);
					setDraftSpec(null);
					setDraftResult(null);
					setSelectedNode(null);
					setRunWorkflowId("");
					setSelectedExecution(null);
					setEvents([]);
					setNodeRuns([]);
					setNodePositions({});
					updateEditorText(specToEditorText(newWorkflowSpec("Untitled Workflow")));
					setStatus("Deleted workflow " + safeString(workflowId));
					return Promise.all([loadDefinitions("__deleted_workflow__"), loadExecutions("__deleted_execution__")]);
				}).catch(fail).finally(function() {
					setDeleting(false);
				});
			}
			function selectedRunVersion(workflowId) {
				const requested = String(workflowId || "");
				const selectedId = workflowIdForDefinition(selectedDefinition);
				if (selectedDefinition && selectedId === requested && selectedDefinition.version !== void 0 && selectedDefinition.version !== null) return selectedDefinition.version;
				const spec = activeSpec();
				if (spec && workflowIdForSpec(spec) === requested && versionForSpec(spec) !== null) return versionForSpec(spec);
				return null;
			}
			function runWorkflow(event) {
				event.preventDefault();
				const workflowId = (runWorkflowId || "").trim();
				if (!workflowId) {
					setError("Choose a workflow before running it.");
					return;
				}
				let input = {};
				try {
					if (showAdvancedInputJson) input = JSON.parse(runInputText || "{}");
					else input = inputObjectForFields(inputFieldsForSpec(runInputSpec()), inputFieldValues);
				} catch (err) {
					fail(err);
					return;
				}
				setRunning(true);
				setError("");
				setRunFieldErrors({});
				const runVersion = selectedRunVersion(workflowId);
				api("/definitions/" + encodeURIComponent(workflowId) + "/run" + versionQuery(runVersion), {
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({ input })
				}).then(function(res) {
					const executionId = (res.execution || {}).execution_id;
					setRunPanelOpen(false);
					setRunFieldErrors({});
					setStatus("Started execution " + safeString(executionId));
					return loadExecutions(executionId);
				}).catch(function(err) {
					const fe = fieldErrors(err);
					if (Object.keys(fe).length) {
						setRunFieldErrors(fe);
						setError(formatApiError$1(err));
					} else fail(err);
				}).finally(function() {
					setRunning(false);
				});
			}
			function cancelSelectedExecution() {
				const execution = selectedExecution || {};
				const executionId = execution.execution_id || execution.id || "";
				const executionStatus = safeString(execution.status);
				if (!executionId) {
					setStatus("Select an execution before cancelling it.");
					return;
				}
				if (!canCancel(executionStatus)) {
					setStatus("Cannot cancel terminal execution " + safeString(executionId));
					return;
				}
				setError("");
				api("/executions/" + encodeURIComponent(executionId) + "/cancel", { method: "POST" }).then(function(res) {
					setSelectedExecution(res.execution || execution);
					setStatus(res.cancelled ? "Cancelled execution " + safeString(executionId) : "Cannot cancel terminal execution " + safeString(executionId));
					return loadExecutions(executionId);
				}).catch(fail);
			}
			function rerunSelectedExecution() {
				const execution = selectedExecution;
				if (!execution) {
					setStatus("Select an execution before rerunning it.");
					return;
				}
				const executionId = execution.execution_id || execution.id || "";
				if (!executionId) return;
				setError("");
				var input = execution.input && typeof execution.input === "object" ? execution.input : {};
				var safeInput = {};
				Object.keys(input).forEach(function(key) {
					var val = input[key];
					if (val !== "[REDACTED]" && val !== void 0) safeInput[key] = val;
				});
				var body = buildRerunBody(safeInput);
				api("/executions/" + encodeURIComponent(executionId) + "/rerun", {
					method: "POST",
					body
				}).then(function(res) {
					var newExec = res.execution;
					setStatus("Rerun started: " + safeString(newExec.execution_id));
					loadExecutions(newExec.execution_id);
					loadExecution(newExec.execution_id);
				}).catch(fail);
			}
			function draftFromGoal(event) {
				event.preventDefault();
				const goal = (goalText || "").trim();
				setStatus("");
				setDraftResult(null);
				if (!goal) {
					setError("Describe what you want the workflow to automate.");
					return;
				}
				setDrafting(true);
				setError("");
				api("/definitions/draft", {
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({ goal })
				}).then(function(res) {
					setDraftResult(res.draft || res);
					setCandidateSource("generate");
					setStatus("AI drafted a workflow. Review and Accept or Reject.");
				}).catch(fail).finally(function() {
					setDrafting(false);
				});
			}
			function refineWorkflow(event) {
				if (event) event.preventDefault();
				const instruction = (refineText || "").trim();
				const spec = activeSpec();
				setStatus("");
				setDraftResult(null);
				if (!instruction || !spec) {
					setError("Select or draft a workflow, then describe the refinement.");
					return;
				}
				setRefining(true);
				setError("");
				api("/definitions/refine", {
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({
						spec,
						instruction
					})
				}).then(function(res) {
					const draft = res && res.draft || res || {};
					if (!draft.spec) throw new Error("Refine response did not include a workflow spec.");
					setDraftResult(draft);
					setCandidateSource("refine");
					setStatus("Refinement proposed. Review changes and Accept or Reject.");
				}).catch(fail).finally(function() {
					setRefining(false);
				});
			}
			function acceptDraftCandidate() {
				const draft = draftResult;
				if (!draft || !draft.spec) return;
				setUndoStack(function(prev) {
					return prev.concat([draftSpec]).slice(-20);
				});
				setDraftSpec(draft.spec);
				setSelectedDefinition(null);
				setSelectedNode(null);
				setNodeJson("");
				setNodeMessage("");
				setInputFieldValues({});
				setShowAdvancedInputJson(false);
				setRunInputText("{}");
				updateEditorText(specToEditorText(draft.spec));
				setCandidateSource(null);
				setDraftResult(null);
				const workflowId = draft.spec.id || draft.spec.workflow_id || "";
				if (workflowId) draftApi.putDraft(workflowId, {
					spec: draft.spec,
					base_version: null
				}).then(function(res) {
					setSavedDraft(draft.spec);
					setStatus("Draft accepted and saved.");
				}).catch(fail);
				else {
					setSavedDraft(draft.spec);
					setStatus("Draft accepted.");
				}
			}
			function rejectDraftCandidate() {
				setDraftResult(null);
				setCandidateSource(null);
				setStatus("Draft rejected. Working draft unchanged.");
			}
			function importDefinitionFile(event) {
				const file = event.target.files && event.target.files[0];
				if (!file) return;
				const reader = new FileReader();
				reader.onload = function() {
					updateEditorText(String(reader.result || ""));
					setStatus("Import YAML loaded " + safeString(file.name));
					setDraftResult(null);
					setInputFieldValues({});
					setShowAdvancedInputJson(false);
					setRunInputText("{}");
					setSelectedDefinition(null);
					setNodeJson("");
					setNodeMessage("");
					setSelectedNode(null);
				};
				reader.onerror = function() {
					setError("Could not import workflow file.");
				};
				reader.readAsText(file);
				event.target.value = "";
			}
			function exportYAML() {
				const spec = activeSpec();
				const base = spec && (spec.id || spec.workflow_id || spec.name) || runWorkflowId || "workflow";
				const blob = new Blob([editorText], { type: "text/yaml;charset=utf-8" });
				const url = URL.createObjectURL(blob);
				const anchor = document.createElement("a");
				anchor.href = url;
				anchor.download = classSafe(base) + ".yaml";
				anchor.click();
				setTimeout(function() {
					URL.revokeObjectURL(url);
				}, 0);
				setStatus("Export YAML downloaded " + anchor.download);
			}
			function copyYAML() {
				if (!navigator.clipboard) {
					setStatus("Clipboard API unavailable; use Export YAML instead.");
					return;
				}
				navigator.clipboard.writeText(editorText).then(function() {
					setStatus("Export YAML copied to clipboard.");
				}).catch(fail);
			}
			function useJsonDraft() {
				const spec = parseJsonObject(editorText) || draftSpec;
				if (!spec) {
					setNodeMessage("Validate the YAML draft before converting it to JSON; no stale workflow was used.");
					return;
				}
				updateEditorText(JSON.stringify(spec, null, 2));
				setNodeMessage("Converted current workflow draft to JSON; node edits can now be applied.");
			}
			function applyNodeJson() {
				if (!selectedNode) return;
				let nextNode;
				try {
					nextNode = JSON.parse(nodeJson);
				} catch (err) {
					setNodeMessage("Invalid node JSON: " + (err && err.message ? err.message : String(err)));
					return;
				}
				const spec = parseJsonObject(editorText);
				if (!spec) {
					setNodeMessage("Current definition text is YAML. Convert to JSON before applying node JSON; YAML was not changed.");
					return;
				}
				const clean = cleanedNodeForSpec(nextNode);
				const nextId = clean.id || selectedNode.id;
				if (selectedNode.specKind === "trigger") spec.triggers = asArray(spec.triggers).map(function(trigger) {
					return (trigger.id || trigger.name) === selectedNode.id ? clean : trigger;
				});
				else if (Array.isArray(spec.nodes)) spec.nodes = spec.nodes.map(function(node) {
					return (node.id || node.name) === selectedNode.id ? clean : node;
				});
				else {
					spec.nodes = spec.nodes || {};
					if (nextId !== selectedNode.id) delete spec.nodes[selectedNode.id];
					const nodeValue = Object.assign({}, clean);
					delete nodeValue.id;
					spec.nodes[nextId] = nodeValue;
				}
				updateEditorText(JSON.stringify(spec, null, 2));
				setSelectedDefinition(Object.assign({}, selectedDefinition || {}, { spec }));
				setSelectedNode(Object.assign({}, nextNode, {
					id: nextId,
					specKind: selectedNode.specKind
				}));
				setNodeMessage("Applied node JSON to editor draft.");
			}
			function addTriggerInputFieldFromUi() {
				const name = workflowIdFromText(String(triggerInputName || "").trim()).replace(/-/g, "_");
				if (!name) {
					setNodeMessage("Enter an input field name before adding it.");
					return;
				}
				const row = {
					name,
					kind: SCALAR_INPUT_KINDS.indexOf(triggerInputKind) !== -1 ? triggerInputKind : "text",
					required: !!triggerInputRequired,
					defaultValue: triggerInputDefault,
					minLength: triggerInputMinLength,
					maxLength: triggerInputMaxLength
				};
				setTriggerInputRows(asArray(triggerInputRows).filter(function(existing) {
					return existing.name !== name;
				}).concat([row]));
				setTriggerInputName("");
				setTriggerInputDefault("");
				setTriggerInputMinLength("");
				setTriggerInputMaxLength("");
				setNodeMessage("Added input field " + safeString(name) + ". Apply to update the workflow draft.");
			}
			function removeTriggerInputField(name) {
				setTriggerInputRows(asArray(triggerInputRows).filter(function(row) {
					return row && row.name !== name;
				}));
			}
			function applyAgentCellForm() {
				if (!selectedNode) return;
				if (cellType !== "agent_task") {
					applyBasicCellForm();
					return;
				}
				const spec = activeSpec();
				if (!spec) {
					setNodeMessage("Validate the YAML draft before applying cell edits; no stale workflow was used.");
					return;
				}
				const nextId = cellId.trim() || selectedNode.id;
				const nextNode = Object.assign({}, selectedNode, {
					id: nextId,
					type: "agent_task",
					profile: agentProfile.trim(),
					title: agentTitle.trim() || nextId,
					prompt: promptText
				});
				const contractText = resultContractText.trim();
				if (contractText) {
					const contract = parseJsonObject(contractText);
					if (!contract) {
						setNodeMessage("Result contract JSON must be a JSON object.");
						return;
					}
					nextNode.result_contract = contract;
				} else delete nextNode.result_contract;
				const providerText = agentProvider.trim();
				const modelText = agentModel.trim();
				delete nextNode.provider_override;
				delete nextNode.model_override;
				if (providerText) nextNode.provider = providerText;
				else delete nextNode.provider;
				if (modelText) nextNode.model = modelText;
				else delete nextNode.model;
				const nextSpec = upsertSpecNode(spec, selectedNode.id, cleanedNodeForSpec(nextNode));
				updateEditorText(specToEditorText(nextSpec));
				setDraftSpec(nextSpec);
				setSelectedDefinition(Object.assign({}, selectedDefinition || {}, { spec: nextSpec }));
				setSelectedNode(Object.assign({}, nextNode, { id: nextId }));
				setNodeJson(jsonBlock(Object.assign({}, nextNode, { id: nextId })));
				setNodeMessage("Applied agent cell prompt to workflow draft.");
			}
			function applyBasicCellForm() {
				if (!selectedNode) return;
				const spec = activeSpec();
				if (!spec) {
					setNodeMessage("Validate or select a workflow before applying cell edits; no stale workflow was used.");
					return;
				}
				const nextId = cellId.trim() || selectedNode.id;
				const nextType = cellType.trim() || (selectedNode.specKind === "trigger" ? "manual" : "pass");
				const nextNode = Object.assign({}, selectedNode, {
					id: nextId,
					type: nextType
				});
				const titleText = agentTitle.trim();
				const promptValue = promptText.trim();
				const outputValue = cellOutputText.trim();
				if (titleText) nextNode.title = titleText;
				else delete nextNode.title;
				if (promptValue && selectedNode.specKind !== "trigger") nextNode.prompt = promptText;
				else delete nextNode.prompt;
				if (selectedNode.specKind !== "trigger") {
					if (nextType === "wait") nextNode.seconds = Math.max(0, parseInt(cellSeconds || "0", 10) || 0);
					else delete nextNode.seconds;
					if (nextType === "pass" || nextType === "fail") if (outputValue) nextNode.output = cellOutputText;
					else delete nextNode.output;
					else delete nextNode.output;
					if (nextType === "switch") {
						nextNode.cases = asArray(switchCases);
						if (switchDefault.trim()) nextNode.default = switchDefault.trim();
						else delete nextNode.default;
					} else {
						delete nextNode.cases;
						delete nextNode.default;
					}
					if (nextType === "send_message") {
						const platform = [
							"auto",
							"discord",
							"telegram",
							"slack"
						].indexOf(sendMessagePlatform) !== -1 ? sendMessagePlatform : "auto";
						if (sendMessageTarget.trim()) nextNode.target = sendMessageTarget.trim();
						else delete nextNode.target;
						if (sendMessageText.trim()) nextNode.message_text = sendMessageText;
						else delete nextNode.message_text;
						nextNode.platform = platform;
					} else {
						delete nextNode.target;
						delete nextNode.message_text;
						delete nextNode.platform;
					}
					if (nextType === "subworkflow") {
						let inputMapping;
						try {
							inputMapping = parseJsonObject(subworkflowInputMappingText || "{}");
						} catch (_) {
							inputMapping = null;
						}
						if (!inputMapping) {
							setNodeMessage("Input mapping JSON must be a JSON object.");
							return;
						}
						if (subworkflowRef.trim()) nextNode.workflow_ref = subworkflowRef.trim();
						else delete nextNode.workflow_ref;
						nextNode.input_mapping = inputMapping;
					} else {
						delete nextNode.workflow_ref;
						delete nextNode.input_mapping;
					}
				}
				delete nextNode.provider_override;
				delete nextNode.model_override;
				if (nextType === "agent_task") {
					const providerText = agentProvider.trim();
					const modelText = agentModel.trim();
					nextNode.profile = agentProfile.trim() || nextNode.profile || "default";
					if (!promptValue) nextNode.prompt = "Return JSON only matching the result contract.";
					if (!nextNode.result_contract || !Object.keys(nextNode.result_contract).length) nextNode.result_contract = {
						summary: "string",
						status: "string"
					};
					if (providerText) nextNode.provider = providerText;
					else delete nextNode.provider;
					if (modelText) nextNode.model = modelText;
					else delete nextNode.model;
				} else {
					delete nextNode.provider;
					delete nextNode.model;
				}
				if (selectedNode.specKind === "trigger") {
					const nextSpec = cloneSpec(spec);
					nextSpec.triggers = asArray(nextSpec.triggers).map(function(trigger) {
						if ((trigger.id || trigger.name) !== selectedNode.id) return trigger;
						const clean = cleanedNodeForSpec(Object.assign({}, trigger, {
							id: nextId,
							type: nextType
						}));
						if (titleText) clean.title = titleText;
						else delete clean.title;
						const schema = inputSchemaFromRows(triggerInputRows);
						if (Object.keys(schema).length) clean.input_schema = schema;
						else delete clean.input_schema;
						clean.intake = triggerIntakeFromForm(triggerIntakeMode, triggerDedupeKey, triggerReadyPath);
						if (nextType === "schedule") clean.schedule = triggerSchedule.trim() || trigger.schedule || trigger.cron || "0 9 * * *";
						else {
							delete clean.schedule;
							delete clean.cron;
							delete clean.expr;
						}
						return clean;
					});
					updateEditorText(specToEditorText(nextSpec));
					setDraftSpec(nextSpec);
					setSelectedDefinition(Object.assign({}, selectedDefinition || {}, { spec: nextSpec }));
					setSelectedNode(Object.assign({}, nextNode, {
						id: nextId,
						specKind: "trigger",
						trigger_type: nextType
					}));
					setNodeJson(jsonBlock(Object.assign({}, nextNode, {
						id: nextId,
						trigger_type: nextType
					})));
					setNodeMessage("Applied cell changes to workflow draft.");
					return;
				}
				const nextSpec = upsertSpecNode(spec, selectedNode.id, cleanedNodeForSpec(nextNode));
				updateEditorText(specToEditorText(nextSpec));
				setDraftSpec(nextSpec);
				setSelectedDefinition(Object.assign({}, selectedDefinition || {}, { spec: nextSpec }));
				setSelectedNode(nextNode);
				setNodeJson(jsonBlock(nextNode));
				setNodeMessage("Applied cell changes to workflow draft.");
			}
			function setActiveDraftSpec(nextSpec, message) {
				updateEditorText(specToEditorText(nextSpec));
				setDraftSpec(nextSpec);
				setSelectedDefinition(Object.assign({}, selectedDefinition || {}, { spec: nextSpec }));
				if (message) setStatus(message);
			}
			function startBlankWorkflow() {
				setActiveDraftSpec(newWorkflowSpec(newWorkflowName || goalText || "Untitled Workflow"), "Started blank workflow draft. No JSON/YAML required.");
				setSelectedNode(null);
				setNodeJson("");
				setNodeMessage("");
			}
			function addTriggerOfType(type, position) {
				const safeType = type === "schedule" || type === "webhook" ? type : "manual";
				const nextSpec = addSpecTrigger(activeSpec() || newWorkflowSpec(newWorkflowName || goalText || "Workflow Draft"), safeType, safeType, newTriggerSchedule);
				const trigger = asArray(nextSpec.triggers).slice(-1)[0];
				const triggerId = trigger && (trigger.id || trigger.name);
				if (triggerId && position) setNodePositions(function(previous) {
					return Object.assign({}, previous, { [triggerId]: position });
				});
				setActiveDraftSpec(nextSpec, "Added " + safeString(safeType) + " trigger.");
				if (trigger) selectNodeForInspector(Object.assign({}, trigger, {
					id: triggerId,
					specKind: "trigger",
					trigger_type: trigger.type
				}));
			}
			function addSwitchCaseFromUi() {
				if (!selectedNode) return;
				const spec = activeSpec();
				if (!spec) {
					setNodeMessage("Start or validate a workflow before adding switch cases.");
					return;
				}
				const nextSpec = addSwitchCaseToSpec(spec, selectedNode.id, switchCaseName || switchCaseEquals || "case", switchCasePath, switchCaseEquals || switchCaseName || "case");
				const nextNode = findSpecNode(nextSpec, selectedNode.id);
				setSwitchCases(asArray(nextNode && nextNode.cases));
				setActiveDraftSpec(nextSpec, "Added switch case. Connect cells from " + safeString(selectedNode.id) + "." + workflowIdFromText(switchCaseName || switchCaseEquals || "case") + " to route that branch.");
				if (nextNode) setSelectedNode(nextNode);
				setSwitchCaseName("");
				setSwitchCaseEquals("");
			}
			function addWorkflowCellAtPosition(type, position) {
				const safeType = type || "pass";
				const nextSpec = addSpecNodeAfter(activeSpec() || newWorkflowSpec(newWorkflowName || goalText || "Workflow Draft"), safeType, safeType, selectedNode && selectedNode.specKind !== "trigger" ? selectedNode.id : "");
				const id = Object.keys(nextSpec.nodes || {}).slice(-1)[0];
				if (id && position) setNodePositions(function(previous) {
					return Object.assign({}, previous, { [id]: position });
				});
				setActiveDraftSpec(nextSpec, "Added " + safeString(safeType) + " cell. Configure it in the inspector.");
				const node = findSpecNode(nextSpec, id);
				if (node) selectNodeForInspector(node);
			}
			function flowPositionFromDropEvent(event) {
				const clientPosition = {
					x: event.clientX,
					y: event.clientY
				};
				const instance = flowInstanceRef.current;
				if (instance && typeof instance.screenToFlowPosition === "function") return instance.screenToFlowPosition(clientPosition);
				return clientPosition;
			}
			function paletteNodePosition() {
				const instance = flowInstanceRef.current;
				const canvas = canvasRef.current;
				const rect = canvas && typeof canvas.getBoundingClientRect === "function" ? canvas.getBoundingClientRect() : null;
				if (instance && rect && typeof instance.screenToFlowPosition === "function") return snapFlowPosition(instance.screenToFlowPosition({
					x: rect.left + rect.width / 2,
					y: rect.top + rect.height / 2
				}));
				return snapFlowPosition({
					x: 280,
					y: 180
				});
			}
			function addNodeFromPalette(type) {
				const position = paletteNodePosition();
				if (type === "manual" || type === "schedule" || type === "webhook") addTriggerOfType(type, position);
				else addWorkflowCellAtPosition(type, position);
				closeNodePalette();
			}
			function deleteSelectedCell() {
				if (!selectedNode) return;
				const spec = activeSpec();
				if (!spec) {
					setNodeMessage("Start or validate a workflow before deleting a cell.");
					return;
				}
				const id = selectedNode.id || selectedNode.name;
				setActiveDraftSpec(selectedNode.specKind === "trigger" ? removeSpecTrigger(spec, id) : removeSpecNode(spec, id), "Deleted selected cell from workflow draft.");
				setSelectedNode(null);
				setNodeJson("");
				setNodeMessage("");
			}
			function draftPromptWithAssistant() {
				const outputText = promptAssistantOutput.trim();
				const expectedOutput = outputText ? parseJsonObject(outputText) : {};
				if (outputText && !expectedOutput) {
					setNodeMessage("Expected output contract JSON must be a JSON object.");
					return;
				}
				api("/prompt-assistant/draft", {
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({
						workflow_goal: promptAssistantGoal,
						node_id: selectedNode && selectedNode.id,
						profile: agentProfile,
						provider: agentProvider,
						model: agentModel,
						cell_objective: promptAssistantObjective,
						available_context: promptAssistantContext.split(/\n+/).map(function(line) {
							return line.trim();
						}).filter(Boolean),
						expected_output: expectedOutput,
						constraints: promptAssistantConstraints.split(/\n+/).map(function(line) {
							return line.trim();
						}).filter(Boolean)
					})
				}).then(function(res) {
					setPromptText(res.prompt_text || "");
					setResultContractText(jsonBlock(res.result_contract || expectedOutput));
					setNodeMessage("Prompt assistant drafted a cell prompt. Review it, then Apply cell prompt.");
				}).catch(fail);
			}
			function renderValidationChecklist() {
				const spec = checklistSpec();
				if (!spec) return h(Card, { className: "hermes-workflows-panel hermes-workflows-validation-checklist" }, h("h2", null, "Validation checklist"), h("p", { className: "hermes-workflows-muted" }, "Select or validate a workflow to see the checklist."));
				const implementedTriggers = implementedTriggerTypesFromCapabilities(workflowCapabilities);
				const implementedNodes = implementedNodeTypesFromCapabilities(workflowCapabilities);
				const items = validationChecklist(spec, workflowCapabilities);
				return h(Card, { className: "hermes-workflows-panel hermes-workflows-validation-checklist" }, h("h2", null, "Validation checklist"), h("p", { className: "hermes-workflows-muted" }, "Checks implemented dashboard/dispatcher readiness, not every declared WorkflowSpec primitive."), h("ul", { className: "hermes-workflows-checklist" }, items.map(function(item) {
					return h("li", {
						key: item.label,
						className: "hermes-workflows-checklist-item " + (item.ok ? "is-ok" : "is-fail")
					}, h("span", {
						className: "hermes-workflows-checklist-mark",
						"aria-hidden": "true"
					}, item.ok ? "✓" : "!"), h("span", null, item.label));
				})), h("p", { className: "hermes-workflows-muted" }, "Implemented triggers today: " + implementedTriggers.join(", ") + "."), h("p", { className: "hermes-workflows-muted" }, "Implemented node types today: " + implementedNodes.join(", ") + "."));
			}
			function renderAdvancedYaml() {
				if (!showAdvancedYaml) return null;
				return h(Card, { className: "hermes-workflows-panel" }, h("h2", null, "Advanced YAML"), h("textarea", {
					className: "hermes-workflows-editor",
					"aria-label": "Workflow definition YAML",
					value: editorText,
					onChange: function(event) {
						setDraftResult(null);
						setInputFieldValues({});
						setShowAdvancedInputJson(false);
						setRunInputText("{}");
						updateEditorText(event.target.value);
					}
				}), h("div", { className: "hermes-workflows-row" }, h("button", {
					type: "button",
					disabled: validating,
					onClick: validateDefinition
				}, validating ? "Validating…" : "Validate"), h("button", {
					type: "button",
					disabled: deploying,
					onClick: deployDefinition,
					className: "hermes-workflows-primary"
				}, deploying ? "Deploying…" : "Deploy"), h("label", { className: "hermes-workflows-file-button" }, "Import YAML", h("input", {
					type: "file",
					accept: ".yaml,.yml,.json,application/yaml,application/x-yaml,application/json",
					onChange: importDefinitionFile
				})), h("button", {
					type: "button",
					onClick: exportYAML
				}, "Export YAML"), h("button", {
					type: "button",
					onClick: copyYAML
				}, "Copy YAML")));
			}
			function renderExecutionStallWarning() {
				if (!selectedExecution) return null;
				const executionStatus = safeString(selectedExecution.status);
				if (executionStatus !== "queued" && executionStatus !== "waiting") return null;
				const dispatcher = workflowStatus && workflowStatus.dispatcher ? workflowStatus.dispatcher : null;
				if (!dispatcher || dispatcher.dispatch_in_gateway === true) return null;
				const warning = dispatcher.warning || "Set workflow.dispatch_in_gateway: true to let the gateway advance runs; fallback: hermes workflow tick.";
				return h("div", { className: "hermes-workflows-panel hermes-workflows-dispatcher-readiness is-warning" }, h("strong", null, "This execution will not advance automatically."), h("p", null, safeString(warning)));
			}
			function renderExecutionActions() {
				if (!selectedExecution) return null;
				const canCancel$1 = canCancel(safeString(selectedExecution.status));
				return h("div", { className: "hermes-workflows-row" }, h("button", {
					type: "button",
					onClick: cancelSelectedExecution,
					disabled: !canCancel$1
				}, canCancel$1 ? "Cancel Execution" : "Cannot cancel terminal execution"), h("button", {
					type: "button",
					onClick: rerunSelectedExecution
				}, "Rerun"));
			}
			function renderTimeline() {
				return h("div", { className: "hermes-workflows-timeline" }, h("p", { className: "hermes-workflows-privacy-note" }, PRIVACY_NOTE), renderExecutionStallWarning(), selectedExecution ? h("div", { className: "hermes-workflows-event" }, h("div", { className: "hermes-workflows-item-title" }, h("strong", null, safeString(selectedExecution.execution_id)), h("span", { className: "hermes-workflows-badge" }, safeString(selectedExecution.status))), h("div", { className: "hermes-workflows-meta" }, safeString(selectedExecution.workflow_id) + " · created " + safeString(selectedExecution.created_at)), h("pre", { className: "hermes-workflows-pre" }, jsonBlock(selectedExecution.input)), renderExecutionActions()) : h("p", { className: "hermes-workflows-muted" }, "Select an execution to inspect it."), renderNodeRuns(), events.length ? events.map(function(row) {
					return h("div", {
						key: row.id,
						className: "hermes-workflows-event"
					}, h("div", { className: "hermes-workflows-item-title" }, h("span", { className: "hermes-workflows-event-kind" }, safeString(row.kind)), h("span", { className: "hermes-workflows-meta" }, safeString(row.created_at))), row.node_run_id ? h("div", { className: "hermes-workflows-meta" }, "node run " + row.node_run_id) : null, h("pre", { className: "hermes-workflows-pre" }, jsonBlock(row.payload)));
				}) : h("p", { className: "hermes-workflows-muted" }, "No events recorded for this execution."));
			}
			function renderNodeRunPreview(label, value) {
				if (!hasPreviewValue(value)) return null;
				return h("div", { className: "hermes-workflows-node-run-preview" }, h("div", { className: "hermes-workflows-meta" }, label), h("pre", { className: "hermes-workflows-pre" }, previewJson(value)));
			}
			function renderNodeRuns() {
				if (!selectedExecution) return null;
				if (!nodeRuns.length) return h("p", { className: "hermes-workflows-muted" }, "No node runs recorded for this execution yet.");
				const groups = [];
				const byNode = {};
				nodeRuns.forEach(function(row, index) {
					const nodeId = safeString(row && row.node_id);
					if (!byNode[nodeId]) {
						byNode[nodeId] = [];
						groups.push({
							nodeId,
							rows: byNode[nodeId]
						});
					}
					byNode[nodeId].push({
						row: row || {},
						index
					});
				});
				return h("div", { className: "hermes-workflows-node-runs" }, h("h3", null, "Node runs"), groups.map(function(group) {
					return h("div", {
						key: group.nodeId,
						className: "hermes-workflows-node-run-group"
					}, group.rows.map(function(item) {
						const row = item.row;
						const status = safeString(row.status);
						const workerStatus = row.kanban_task_id && row.status === "waiting" ? "waiting on agent" : status;
						const payload = row.payload && typeof row.payload === "object" ? row.payload : {};
						const runProvider = row.provider || row.provider_override || payload.provider || payload.provider_override || "";
						const runModel = row.model || row.model_override || payload.model || payload.model_override || "";
						return h("div", {
							key: String(row.id || row.event_id || item.index),
							className: "hermes-workflows-node-run-card"
						}, h("div", { className: "hermes-workflows-item-title" }, h("strong", null, safeString(row.node_id)), h("span", { className: "hermes-workflows-badge" }, status)), row.kanban_task_id ? h("div", { className: "hermes-workflows-node-run-worker" }, h("div", null, "Linked worker task: " + safeString(row.kanban_task_id)), h("div", { className: "hermes-workflows-meta" }, "Worker status: " + workerStatus)) : null, runProvider || runModel ? h("div", { className: "hermes-workflows-node-run-routing" }, h("div", { className: "hermes-workflows-meta" }, "Provider / model: " + safeString(runProvider || "profile default") + " / " + safeString(runModel || "profile default"))) : null, renderNodeRunPreview("Output", row.output), renderNodeRunPreview("Error", row.error));
					}));
				}));
			}
			function renderCellList(spec) {
				const nodes = graphItems(spec);
				return h("section", {
					className: "hermes-workflows-cell-list",
					"aria-label": "Workflow cell list"
				}, h("h3", null, "Workflow cell list"), nodes.length ? nodes.map(function(item) {
					const id = item.id || item.spec.name || "node";
					const triggerType = item.specKind === "trigger" ? item.triggerType : item.rendererType;
					return h("button", {
						key: id,
						type: "button",
						className: "hermes-workflows-cell-list-item",
						"aria-label": "Edit cell " + safeString(id),
						onClick: function() {
							selectNodeForInspector(item);
						}
					}, h("span", null, safeString(id)), h("span", null, safeString(triggerType || "unknown")), h("span", null, triggerType === "agent_task" ? safeString(providerValue(item.spec) || "profile provider") : "—"), h("span", null, "Edit cell"));
				}) : h("p", { className: "hermes-workflows-muted" }, "No workflow cells available."));
			}
			function renderSimpleGraph(spec) {
				const items = graphItems(spec);
				const edges = edgeList(spec);
				return h("div", { className: "hermes-workflows-graph-fallback" }, renderCellList(spec), items.length ? h("div", { className: "hermes-workflows-node-grid" }, items.map(function(item) {
					const id = item.id || item.spec.name || "node";
					const triggerType = item.specKind === "trigger" ? item.triggerType : item.rendererType;
					return h("div", {
						key: id,
						className: "hermes-workflows-node-card",
						onClick: function() {
							selectNodeForInspector(item);
						}
					}, h("h3", null, safeString(id)), h("div", { className: "hermes-workflows-node-type" }, safeString(triggerType)), h("pre", { className: "hermes-workflows-pre" }, jsonBlock(item.spec)));
				})) : h("p", { className: "hermes-workflows-muted" }, "No nodes to render."), h("div", { className: "hermes-workflows-stack" }, h("h3", null, "Edges"), edges.length ? edges.map(function(edge) {
					const text = safeString(edge.from) + " → " + safeString(edge.to) + (edge.label ? " · " + edge.label : "");
					return h("div", {
						key: edge.id,
						className: "hermes-workflows-edge-card"
					}, text);
				}) : h("p", { className: "hermes-workflows-muted" }, "No edges defined.")));
			}
			function renderReactFlowGraph(spec) {
				function renderCanvasOnboarding(className) {
					return h("div", { className }, h("div", { className: "hermes-workflows-canvas-onboarding-card" }, renderWorkflowOnboarding({
						createElement: h,
						activeSpec: null,
						goalText,
						setGoalText,
						newWorkflowName,
						setNewWorkflowName,
						draftFromGoal,
						drafting,
						startBlankWorkflow,
						refineWorkflow,
						refining,
						refineText,
						setRefineText,
						draftResult,
						candidateSource,
						acceptDraftCandidate,
						rejectDraftCandidate
					})));
				}
				if (!ReactFlow || !ReactFlowProvider) return spec ? renderSimpleGraph(spec) : renderCanvasOnboarding("hermes-workflows-canvas-onboarding hermes-workflows-simple-canvas-onboarding");
				return h("div", { className: "hermes-workflows-flow-surface" }, h("div", {
					ref: canvasRef,
					className: "hermes-workflows-canvas" + (isDragOver ? " hermes-workflows-canvas-drop-target" : ""),
					onDragOver: function(event) {
						event.preventDefault();
						if (!isDragOver) setIsDragOver(true);
					},
					onDragLeave: function() {
						setIsDragOver(false);
					},
					onDrop: function(event) {
						event.preventDefault();
						setIsDragOver(false);
						const type = event.dataTransfer && event.dataTransfer.getData("text/plain") || window.__HERMES_DRAG_NODE_TYPE || "";
						const dropPosition = snapFlowPosition(flowPositionFromDropEvent(event));
						if (type === "manual" || type === "schedule" || type === "webhook") addTriggerOfType(type, dropPosition);
						else if (type) addWorkflowCellAtPosition(type, dropPosition);
						delete window.__HERMES_DRAG_NODE_TYPE;
					}
				}, h(ReactFlowProvider, null, h(ReactFlow, {
					nodes: spec ? flowNodes : [],
					edges: spec ? flowEdges : [],
					nodeTypes: NODE_TYPES,
					fitView: false,
					nodesDraggable: true,
					nodesConnectable: true,
					onInit: function(instance) {
						flowInstanceRef.current = instance;
					},
					onNodeClick: function(_, node) {
						if (node && node.data && node.data.node) selectNodeForInspector(node.data.node);
					},
					onNodesChange: applyNodeChanges ? function(changes) {
						setFlowNodes(applyNodeChanges(changes, flowNodes));
						changes.forEach(function(change) {
							if (change.type === "position" && change.position && change.dragging === false) setNodePositions(function(prev) {
								var next = Object.assign({}, prev);
								next[change.id] = change.position;
								return next;
							});
						});
					} : void 0,
					onEdgesChange: applyEdgeChanges ? function(changes) {
						setFlowEdges(applyEdgeChanges(changes, flowEdges));
					} : void 0,
					onConnect: addEdge ? function(connection) {
						const spec = activeSpec();
						if (isTriggerSource(spec, connection.source)) {
							setStatus("Triggers start workflows automatically; connect cells to other cells, not triggers.");
							return;
						}
						setFlowEdges(addEdge(Object.assign({
							label: "draft",
							markerEnd: { type: MarkerType.ArrowClosed }
						}, connection), flowEdges));
						if (spec && connection.source && connection.target) {
							const nextSpec = upsertSpecEdge(spec, connection.sourceHandle ? connection.source + "." + connection.sourceHandle : connection.source, connection.target);
							updateEditorText(specToEditorText(nextSpec));
							setDraftSpec(nextSpec);
							setStatus("Connection added to workflow draft.");
						} else setStatus("Draft connection added visually; validate/select a workflow to persist it.");
					} : void 0,
					onEdgeDoubleClick: function(event, edge) {
						if (!edge) return;
						event.preventDefault();
						event.stopPropagation();
						const spec = activeSpec();
						const current = edge && edge.label || "";
						const nextLabel = typeof window === "undefined" ? current : window.prompt("Edit edge label (used for switch case names):", current);
						if (nextLabel === null || nextLabel === current) return;
						if (!spec) return;
						const edges = asArray(spec.edges).map(function(entry) {
							if (entry === edge) return Object.assign({}, entry, { label: nextLabel });
							return entry && edge && entry.id === edge.id ? Object.assign({}, entry, { label: nextLabel }) : entry;
						});
						const updated = edges.find(function(entry) {
							return entry && entry.id === edge.id;
						});
						const nextSpec = Object.assign({}, spec, { edges });
						updateEditorText(specToEditorText(nextSpec));
						setDraftSpec(nextSpec);
						setFlowEdges(flowEdges.map(function(e) {
							return e && e.id === edge.id ? Object.assign({}, e, { label: nextLabel }) : e;
						}));
						setStatus(updated && updated.label !== current ? "Edge label updated." : "Edge label cleared.");
					},
					onNodeContextMenu: function(event, node) {
						event.preventDefault();
						if (node && node.data && node.data.node) {
							selectNodeForInspector(node.data.node);
							setContextMenu({
								x: event.clientX,
								y: event.clientY,
								visible: true
							});
						}
					}
				}, Controls ? h(Controls, null) : null)), !spec ? renderCanvasOnboarding("hermes-workflows-canvas-onboarding") : null, h(NodePaletteOverlay, {
					createElement: h,
					React,
					isOpen: nodePaletteOpen,
					nodePaletteGroup,
					categories: NODE_CATEGORIES,
					closeNodePalette,
					onSelectNode: addNodeFromPalette
				}), contextMenu.visible ? h(React.Fragment, null, h("div", {
					className: "hermes-workflows-context-menu-overlay",
					style: {
						position: "fixed",
						inset: 0,
						zIndex: 999
					},
					onClick: function() {
						setContextMenu({
							x: 0,
							y: 0,
							visible: false
						});
					}
				}), h("div", {
					className: "hermes-workflows-context-menu",
					style: {
						left: contextMenu.x + "px",
						top: contextMenu.y + "px",
						zIndex: 1e3
					},
					onClick: function() {
						setContextMenu({
							x: 0,
							y: 0,
							visible: false
						});
					}
				}, h("button", {
					type: "button",
					onClick: function() {
						deleteSelectedCell();
						setContextMenu({
							x: 0,
							y: 0,
							visible: false
						});
					}
				}, "Delete cell"))) : null));
			}
			function renderWorkspaceTabsBar() {
				workflowIdFromLocation() || selectedDefinition && workflowIdForDefinition(selectedDefinition);
				const runDisabled = !selectedDefinition || !persistedRunCapable();
				return h("div", {
					role: "tablist",
					"aria-label": "Workflow workspace modes",
					className: "hermes-workflows-workspace-tabs"
				}, WORKSPACE_MODES.map(function(mode) {
					return h("button", {
						key: mode,
						type: "button",
						role: "tab",
						"data-workspace-mode": mode,
						"aria-selected": workspaceMode === mode ? "true" : "false",
						"aria-controls": "hermes-workflows-mode-" + mode,
						disabled: mode === "run" && runDisabled,
						...mode === "run" && runDisabled ? {
							"aria-disabled": "true",
							title: "Run is disabled until the workflow is published"
						} : {},
						className: "hermes-workflows-workspace-tab" + (workspaceMode === mode ? " is-active" : ""),
						onClick: function() {
							if (workspaceMode === mode) return;
							setWorkspaceMode(mode);
							pushMode(mode, mode === "run" ? { feed: selectedFeedId } : mode === "history" ? { execution: selectedExecution && selectedExecution.execution_id } : {});
						}
					}, mode === "build" ? "Build" : mode === "run" ? "Run" : "History");
				}));
			}
			function persistedRunCapable() {
				return !!(selectedDefinition && workflowIdForDefinition(selectedDefinition) && versionForDefinition(selectedDefinition));
			}
			function renderBuildMode() {
				return h("section", {
					id: "hermes-workflows-mode-build",
					role: "tabpanel",
					"aria-label": "Build workflow",
					className: "hermes-workflows-build-mode"
				}, h("div", { className: "hermes-workflows-canvas-area" }, h("div", { className: "hermes-workflows-canvas-main" }, h("div", { className: "hermes-workflows-canvas-wrap" }, renderReactFlowGraph(activeSpec())))));
			}
			function renderRunMode() {
				return h("section", {
					id: "hermes-workflows-mode-run",
					role: "tabpanel",
					"aria-label": "Run workflow",
					className: "hermes-workflows-run-mode"
				}, renderInputFeedPanel({
					createElement: h,
					React,
					selectedDefinition,
					workflowIdForDefinition,
					selectedInputTrigger,
					inputFieldsForSpec,
					inputFeeds,
					selectedFeedId,
					setSelectedFeedId,
					openContinuousFeed,
					setSelectedFeedStatus,
					feedBusy,
					loadInputFeedItems,
					inputFeedItems,
					updateInputFeedItem,
					addItemToFeed,
					feedInputValues,
					setFeedInputValues,
					showAdvancedFeedInputJson,
					setShowAdvancedFeedInputJson,
					feedInputText,
					setFeedInputText,
					feedActions
				}), renderDiagnosticsPanel({
					createElement: h,
					React,
					diagnosticsOpen,
					setDiagnosticsOpen,
					renderExecutionStallWarning,
					ticking,
					manualTick,
					loading,
					refresh
				}));
			}
			function renderHistoryMode$1() {
				return renderHistoryMode({
					createElement: h,
					React,
					executions,
					selectedExecution,
					loadExecution,
					refresh,
					loading,
					pushMode,
					renderTimeline,
					fail
				});
			}
			function renderActiveMode() {
				if (workspaceMode === "run") return renderRunMode();
				if (workspaceMode === "history") return renderHistoryMode$1();
				return renderBuildMode();
			}
			var spec = activeSpec();
			var topBarProps = {
				createElement: h,
				React,
				activeSpec: spec,
				selectedDefinition,
				workflowIdForDefinition,
				versionForDefinition,
				renderWorkspaceTabs: renderWorkspaceTabsBar,
				validateDefinition,
				validating,
				deployDefinition,
				deploying,
				deleteWorkflow,
				deleting,
				openRunPanel: function() {
					setRunWorkflowId(workflowIdForDefinition(selectedDefinition));
					setRunPanelOpen(true);
				},
				openNodePalette,
				running,
				refresh: function() {
					refresh();
				},
				loading,
				showAdvancedYaml,
				setShowAdvancedYaml,
				persistedRunCapable
			};
			var inspectorProps = {
				createElement: h,
				React,
				selectedNode,
				cellId,
				setCellId,
				cellType,
				setCellType,
				activeSpec,
				agentProfile,
				setAgentProfile,
				agentProvider,
				setAgentProvider,
				agentModel,
				setAgentModel,
				agentTitle,
				setAgentTitle,
				promptText,
				setPromptText,
				resultContractText,
				setResultContractText,
				agentRoutingOptions,
				promptAssistantOpen,
				setPromptAssistantOpen,
				promptAssistantGoal,
				setPromptAssistantGoal,
				promptAssistantObjective,
				setPromptAssistantObjective,
				promptAssistantContext,
				setPromptAssistantContext,
				promptAssistantOutput,
				setPromptAssistantOutput,
				promptAssistantConstraints,
				setPromptAssistantConstraints,
				promptAssistantAdvanced,
				setPromptAssistantAdvanced,
				draftPromptWithAssistant,
				triggerInputRows,
				setTriggerInputRows,
				triggerInputName,
				setTriggerInputName,
				triggerInputKind,
				setTriggerInputKind,
				triggerInputRequired,
				setTriggerInputRequired,
				triggerInputDefault,
				setTriggerInputDefault,
				triggerInputMinLength,
				setTriggerInputMinLength,
				triggerInputMaxLength,
				setTriggerInputMaxLength,
				triggerIntakeMode,
				setTriggerIntakeMode,
				triggerDedupeKey,
				setTriggerDedupeKey,
				triggerReadyPath,
				setTriggerReadyPath,
				triggerSchedule,
				setTriggerSchedule,
				addTriggerInputFieldFromUi,
				removeTriggerInputField,
				switchDefault,
				setSwitchDefault,
				switchCases,
				setSwitchCases,
				switchCaseName,
				setSwitchCaseName,
				switchCasePath,
				setSwitchCasePath,
				switchCaseEquals,
				setSwitchCaseEquals,
				addSwitchCaseFromUi,
				cellSeconds,
				setCellSeconds,
				cellOutputText,
				setCellOutputText,
				sendMessagePlatform,
				setSendMessagePlatform,
				sendMessageText,
				setSendMessageText,
				applyAgentCellForm,
				applyBasicCellForm,
				deleteSelectedCell,
				advancedJsonOpen,
				setAdvancedJsonOpen,
				nodeJson,
				setNodeJson,
				applyNodeJson,
				useJsonDraft,
				nodeMessage,
				setNodeMessage,
				setDraftSpec,
				setSelectedDefinition,
				draftSpec,
				selectedDefinition
			};
			return h("div", { className: "hermes-workflows" }, h("div", { className: "hermes-workflows-app" }, renderTopBar(topBarProps), error || status ? h("div", { className: "hermes-workflows-status-row" }, error ? h("div", {
				className: "hermes-workflows-banner is-error",
				role: "alert"
			}, h("span", null, error), h("button", {
				type: "button",
				className: "hermes-workflows-banner-close",
				"aria-label": "Dismiss alert",
				onClick: clearBanners
			}, "×")) : null, status ? h("div", {
				className: "hermes-workflows-banner",
				role: "status",
				"aria-live": "polite"
			}, h("span", null, status), h("button", {
				type: "button",
				className: "hermes-workflows-banner-close",
				"aria-label": "Dismiss alert",
				onClick: clearBanners
			}, "×")) : null) : null, h("div", { className: "hermes-workflows-body" + (workspaceMode === "build" && selectedNode ? " has-inspector" : "") }, h("div", { className: "hermes-workflows-palette-zone" }, h(WorkflowRail, {
				variant: "rail",
				createElement: h,
				React,
				activeSpec: spec,
				hideWorkflowForm: !spec,
				goalText,
				setGoalText,
				newWorkflowName,
				setNewWorkflowName,
				draftFromGoal,
				drafting,
				startBlankWorkflow,
				refineWorkflow,
				refining,
				refineText,
				setRefineText,
				draftResult,
				candidateSource,
				acceptDraftCandidate,
				rejectDraftCandidate,
				definitions,
				selectedDefinition,
				loadDefinition,
				executions,
				loadExecution,
				libraryGroups: libraryGroups(NODE_CATEGORIES),
				openNodePalette
			})), h("div", { className: "hermes-workflows-canvas-zone" }, renderActiveMode()), workspaceMode === "build" && selectedNode ? h("div", { className: "hermes-workflows-inspector-zone" }, renderInspector(inspectorProps)) : null, h("div", { className: "hermes-workflows-bottom-zone" }, workspaceMode === "build" ? renderBottomPanel({
				createElement: h,
				React,
				bottomCollapsed,
				setBottomCollapsed,
				activeSpec: spec,
				workflowCapabilities,
				renderValidationChecklist
			}) : null)), renderRunStartPanel({
				createElement: h,
				React,
				runPanelOpen,
				setRunPanelOpen,
				runPanelRef,
				runWorkflowId,
				definitions,
				inputFieldsForSpec,
				runInputSpec,
				inputFieldValues,
				setInputFieldValues,
				showAdvancedInputJson,
				setShowAdvancedInputJson,
				runInputText,
				setRunInputText,
				running,
				runFieldErrors,
				runWorkflow,
				selectedRunVersion,
				loadDefinition,
				fail
			}), showAdvancedYaml ? renderAdvancedYaml() : null));
		}
		REG.register("workflows", WorkflowsPage);
	})();
	//#endregion
})();
