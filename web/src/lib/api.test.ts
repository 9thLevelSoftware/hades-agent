import { afterEach, describe, expect, it, vi } from "vitest";

import { api, setManagementProfile } from "./api";

const SESSION_HEADER = "X-Hermes-Session-Token";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  setManagementProfile("");
});

function jsonFetchMock(body: unknown = { ok: true }) {
  return vi.fn<typeof fetch>(
    async () =>
      new Response(JSON.stringify(body), {
        headers: { "Content-Type": "application/json" },
        status: 200,
      }),
  );
}

describe("api.getModelOptions", () => {
  it("requests a live model refresh when asked", async () => {
    vi.stubGlobal("window", {});

    const fetchMock = jsonFetchMock({ providers: [] });
    vi.stubGlobal("fetch", fetchMock);

    await api.getModelOptions({ refresh: true });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/model/options?refresh=1&include_unconfigured=1",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("keeps explicit profile scoping when refreshing", async () => {
    vi.stubGlobal("window", {});

    const fetchMock = jsonFetchMock({ providers: [] });
    vi.stubGlobal("fetch", fetchMock);

    await api.getModelOptions({ profile: "default", refresh: true });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/model/options?profile=default&refresh=1&include_unconfigured=1",
      expect.objectContaining({ credentials: "include" }),
    );
  });
});

describe("api OAuth helpers", () => {
  it("starts OAuth login in gated mode without requiring an injected session token", async () => {
    vi.stubGlobal("window", { __HERMES_AUTH_REQUIRED__: true });
    const fetchMock = jsonFetchMock({
      flow: "device_code",
      session_id: "oauth-session",
    });
    vi.stubGlobal("fetch", fetchMock);

    await api.startOAuthLogin("openai-codex");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/providers/oauth/openai-codex/start",
      expect.objectContaining({
        body: "{}",
        credentials: "include",
        method: "POST",
      }),
    );
    const headers = fetchMock.mock.calls[0][1]?.headers as Headers;
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(headers.has(SESSION_HEADER)).toBe(false);
  });

  it("still sends the injected session token for OAuth login in loopback mode", async () => {
    vi.stubGlobal("window", { __HERMES_SESSION_TOKEN__: "loopback-token" });
    const fetchMock = jsonFetchMock({
      flow: "device_code",
      session_id: "oauth-session",
    });
    vi.stubGlobal("fetch", fetchMock);

    await api.startOAuthLogin("openai-codex");

    const headers = fetchMock.mock.calls[0][1]?.headers as Headers;
    expect(headers.get(SESSION_HEADER)).toBe("loopback-token");
  });

  it("runs provider auth mutations in gated mode via cookie auth", async () => {
    vi.stubGlobal("window", { __HERMES_AUTH_REQUIRED__: true });
    const fetchMock = jsonFetchMock({ ok: true });
    vi.stubGlobal("fetch", fetchMock);

    await api.disconnectOAuthProvider("anthropic");
    await api.submitOAuthCode("anthropic", "oauth-session", "code-123");
    await api.cancelOAuthSession("oauth-session");
    await api.revealEnvVar("OPENAI_API_KEY");

    for (const call of fetchMock.mock.calls) {
      const init = call[1] as RequestInit;
      expect(init.credentials).toBe("include");
      expect((init.headers as Headers).has(SESSION_HEADER)).toBe(false);
    }
  });
});

describe("Hades management API contract", () => {
  it("requests filtered autonomy rules within the active management profile", async () => {
    vi.stubGlobal("window", {});

    const fetchMock = jsonFetchMock({ rules: [] });
    vi.stubGlobal("fetch", fetchMock);
    setManagementProfile("alpha");

    await api.getAutonomyRules({ source: "user_assertion", effective: true });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url] = fetchMock.mock.calls[0];
    const requestUrl = new URL(String(url), "http://test");
    expect(requestUrl.pathname).toBe("/api/autonomy/rules");
    expect(requestUrl.searchParams.get("source")).toBe("user_assertion");
    expect(requestUrl.searchParams.get("effective")).toBe("true");
    expect(requestUrl.searchParams.get("profile")).toBe("alpha");
  });

  it("requests filtered receipts within the active management profile", async () => {
    vi.stubGlobal("window", {});

    const fetchMock = jsonFetchMock({ receipts: [] });
    vi.stubGlobal("fetch", fetchMock);
    setManagementProfile("alpha");

    await api.getReceipts({ status: "verified", limit: 10 });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url] = fetchMock.mock.calls[0];
    const requestUrl = new URL(String(url), "http://test");
    expect(requestUrl.pathname).toBe("/api/receipts");
    expect(requestUrl.searchParams.get("status")).toBe("verified");
    expect(requestUrl.searchParams.get("limit")).toBe("10");
    expect(requestUrl.searchParams.get("profile")).toBe("alpha");
  });

  it("does not let a preview caller override the active management profile", async () => {
    vi.stubGlobal("window", {});

    const fetchMock = jsonFetchMock({});
    vi.stubGlobal("fetch", fetchMock);
    setManagementProfile("alpha");

    const change = {
      set_rules: [{ rule_id: "rule-new" }],
      remove_rule_ids: ["rule-old"],
      profile: "beta",
    };
    await api.previewAutonomyChange(change);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    const requestUrl = new URL(String(url), "http://test");
    expect(requestUrl.pathname).toBe("/api/autonomy/preview");
    expect(requestUrl.searchParams.get("profile")).toBe("alpha");
    expect(init?.method).toBe("POST");
    expect((init?.headers as Headers).get("Content-Type")).toBe(
      "application/json",
    );
    const requestBody = JSON.parse(String(init?.body));
    expect(requestBody.profile).toBe("alpha");
    expect(requestBody.set_rules).toEqual(change.set_rules);
    expect(requestBody.remove_rule_ids).toEqual(change.remove_rule_ids);
  });

  it("does not let an apply caller override the active management profile", async () => {
    vi.stubGlobal("window", {});

    const fetchMock = jsonFetchMock({});
    vi.stubGlobal("fetch", fetchMock);
    setManagementProfile("alpha");

    const change = {
      remove_rule_ids: ["rule-old"],
      expected_contract_hash: "contract-hash",
      profile: "beta",
    };
    await api.applyAutonomyPreview(change);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    const requestUrl = new URL(String(url), "http://test");
    expect(requestUrl.pathname).toBe("/api/autonomy/apply");
    expect(requestUrl.searchParams.get("profile")).toBe("alpha");
    expect(init?.method).toBe("POST");
    expect((init?.headers as Headers).get("Content-Type")).toBe(
      "application/json",
    );
    const requestBody = JSON.parse(String(init?.body));
    expect(requestBody.profile).toBe("alpha");
    expect(requestBody.remove_rule_ids).toEqual(change.remove_rule_ids);
    expect(requestBody.expected_contract_hash).toBe(
      change.expected_contract_hash,
    );
  });

  it("does not let an accept caller override the active management profile", async () => {
    vi.stubGlobal("window", {});

    const fetchMock = jsonFetchMock({});
    vi.stubGlobal("fetch", fetchMock);
    setManagementProfile("alpha");

    const body = {
      destination: "mandate" as const,
      expected_contract_hash: "contract-hash",
      profile: "beta",
    };
    await api.acceptAutonomySuggestion("suggestion-1", body);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    const requestUrl = new URL(String(url), "http://test");
    expect(requestUrl.pathname).toBe(
      "/api/autonomy/suggestions/suggestion-1/accept",
    );
    expect(requestUrl.searchParams.get("profile")).toBe("alpha");
    expect(init?.method).toBe("POST");
    expect((init?.headers as Headers).get("Content-Type")).toBe(
      "application/json",
    );
    const requestBody = JSON.parse(String(init?.body));
    expect(requestBody.profile).toBe("alpha");
    expect(requestBody.destination).toBe(body.destination);
    expect(requestBody.expected_contract_hash).toBe(
      body.expected_contract_hash,
    );
  });
});
