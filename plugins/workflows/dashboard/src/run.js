// Run-mode pure helpers — no side effects, no DOM.

const ACTIONS_BY_STATUS = {
  open: ["pause", "close"],
  paused: ["resume", "close"],
  closed: ["start-new"],
};

const ACTIVE_STATUSES = new Set(["queued", "running", "needs_input"]);

export function feedActions(status) {
  return ACTIONS_BY_STATUS[status] || [];
}

export function shouldPollFeed(items) {
  if (!Array.isArray(items) || !items.length) return false;
  return items.some(function (item) { return ACTIVE_STATUSES.has(item.status); });
}

export function fieldErrors(error) {
  if (error && typeof error === "object" && !Array.isArray(error)) {
    const detail = error.detail;
    if (detail && typeof detail === "object" && !Array.isArray(detail) && detail.field_errors && typeof detail.field_errors === "object") {
      return detail.field_errors;
    }
  }
  return {};
}
