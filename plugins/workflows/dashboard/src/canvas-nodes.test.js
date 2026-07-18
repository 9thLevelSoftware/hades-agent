import { describe, expect, it } from "vitest";
import { makeWorkflowNode, NODE_COLORS, NODE_ICONS } from "./canvas-nodes.js";

const supportedTypes = Object.keys(NODE_COLORS);
const SDK = {
  React: { createElement() {} },
  ReactFlow: { Handle() {}, Position: { Left: "left", Right: "right" } },
};

describe("canvas node renderers", () => {
  it("defines a color for every supported node type", () => {
    supportedTypes.forEach((type) => expect(NODE_COLORS[type]).toMatch(/^#/));
  });

  it("defines icons for node types and trigger subtypes", () => {
    [...supportedTypes, "manual", "schedule", "webhook", "kanban_event"].forEach((type) => {
      expect(NODE_ICONS[type]).toBeTruthy();
    });
  });

  it("creates a renderer component for each supported node type", () => {
    supportedTypes.forEach((type) => expect(makeWorkflowNode(type, SDK)).toEqual(expect.any(Function)));
  });
});
