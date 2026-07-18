import { describe, expect, it } from "vitest";
import { feedActions, shouldPollFeed, fieldErrors } from "./run.js";

describe("feedActions", () => {
  it("open → pause + close", () => {
    expect(feedActions("open")).toEqual(["pause", "close"]);
  });
  it("paused → resume + close", () => {
    expect(feedActions("paused")).toEqual(["resume", "close"]);
  });
  it("closed → start-new", () => {
    expect(feedActions("closed")).toEqual(["start-new"]);
  });
  it("unknown → empty", () => {
    expect(feedActions("bogus")).toEqual([]);
  });
});

describe("shouldPollFeed", () => {
  it("polls when any item is queued", () => {
    expect(shouldPollFeed([{ status: "queued" }])).toBe(true);
  });
  it("polls when any item is running", () => {
    expect(shouldPollFeed([{ status: "running" }])).toBe(true);
  });
  it("polls when any item needs_input", () => {
    expect(shouldPollFeed([{ status: "needs_input" }])).toBe(true);
  });
  it("stops when all items are terminal", () => {
    expect(shouldPollFeed([{ status: "succeeded" }, { status: "failed" }])).toBe(false);
  });
  it("stops on empty list", () => {
    expect(shouldPollFeed([])).toBe(false);
  });
});

describe("fieldErrors", () => {
  it("extracts field_errors from error envelope", () => {
    const err = { detail: { field_errors: { brief: "required" } } };
    expect(fieldErrors(err)).toEqual({ brief: "required" });
  });
  it("returns {} when no field_errors", () => {
    expect(fieldErrors({ detail: { message: "bad" } })).toEqual({});
  });
  it("returns {} for non-object errors", () => {
    expect(fieldErrors("string")).toEqual({});
    expect(fieldErrors(null)).toEqual({});
  });
});
