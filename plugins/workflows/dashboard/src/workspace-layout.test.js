import { describe, expect, it } from "vitest";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const sourceDir = path.dirname(fileURLToPath(import.meta.url));
const appSource = fs.readFileSync(path.join(sourceDir, "app.js"), "utf8");
const paletteSource = fs.readFileSync(path.join(sourceDir, "palette.js"), "utf8");
const topbarSource = fs.readFileSync(path.join(sourceDir, "topbar.js"), "utf8");
const cssSource = fs.readFileSync(path.join(sourceDir, "style.css"), "utf8");

describe("workflow canvas workspace layout", () => {
  it("gives the Build panel and canvas a full-height grid-zone chain", () => {
    expect(cssSource).toContain(".hermes-workflows-canvas-zone > .hermes-workflows-build-mode");
    expect(cssSource).toContain("grid-row: 1;");
    expect(cssSource).toContain("minmax(0, 1fr)");
  });

  it("renders an intentional square grid through CSS rather than a competing flow background", () => {
    expect(cssSource).toContain("linear-gradient(to right");
    expect(cssSource).toContain("linear-gradient(to bottom");
  });

  it("fits only after the rendered flow-node membership is available", () => {
    expect(appSource).toContain("const key = flowNodes.map");
    expect(appSource).toContain("requestAnimationFrame");
    expect(appSource).toContain("membershipKeyRef.current = key;");
  });

  it("converts the palette drop point into flow coordinates before creating a node", () => {
    expect(appSource).toContain("screenToFlowPosition");
    expect(appSource).toContain("snapFlowPosition(flowPositionFromDropEvent(event))");
    expect(appSource).toContain("addWorkflowCellAtPosition(type, dropPosition)");
    expect(appSource).toContain("function addWorkflowCellAtPosition(type, position)");
  });

  it("wires the live workflow rail and manual node overlay", () => {
    const paletteZoneStart = appSource.indexOf("hermes-workflows-palette-zone");
    const paletteZoneEnd = appSource.indexOf("hermes-workflows-canvas-zone", paletteZoneStart);
    const livePaletteZone = appSource.slice(paletteZoneStart, paletteZoneEnd);
    expect(topbarSource).toContain('"Add Node"');
    expect(appSource).toContain("openNodePalette");
    expect(paletteSource).toContain("NodePaletteOverlay");
    expect(appSource).toContain("h(WorkflowRail");
    expect(livePaletteZone).not.toContain("renderPalette({");
  });

  it("keeps the React Flow canvas mounted while onboarding has no workflow", () => {
    expect(appSource).toContain("renderReactFlowGraph(activeSpec())");
    expect(appSource).not.toContain("No workflow loaded. Use the sidebar to draft a new workflow or select an existing one.");
  });

  it("uses the hook-free onboarding renderer in the canvas overlay", () => {
    expect(appSource).toContain("renderWorkflowOnboarding(");
    const canvasStart = appSource.indexOf("function renderReactFlowGraph");
    const canvasEnd = appSource.indexOf("function renderBuildMode");
    const canvasSource = appSource.slice(canvasStart, canvasEnd);
    expect(canvasSource).toContain("renderWorkflowOnboarding(");
    expect(canvasSource).not.toContain("renderPalette({");
    expect(canvasSource).toContain("renderCanvasOnboarding(\"hermes-workflows-canvas-onboarding hermes-workflows-simple-canvas-onboarding\")");
  });

  it("Node Configuration inspector exposes tablist, tabpanels, sticky header, and per-field wrappers", () => {
    expect(cssSource).toMatch(/\.hermes-workflows\s+\.hermes-workflows-inspector-sticky-header[\s\S]*?position:\s*sticky/);
    expect(cssSource).toContain(".hermes-workflows .hermes-workflows-tabs");
    expect(cssSource).toMatch(/\.hermes-workflows\s+\.hermes-workflows-tab[^\{]*\{/);
    expect(cssSource).toContain(".hermes-workflows-tab.is-active");
    expect(cssSource).toContain(".hermes-workflows-field");
    expect(cssSource).toContain(".hermes-workflows-field-row");
    expect(cssSource).toMatch(/\.hermes-workflows-field-row[\s\S]*?grid-template-columns/);
    expect(cssSource).toContain(".hermes-workflows-section-panel");
    expect(cssSource).toContain(".hermes-workflows-node-message");
  });
});
