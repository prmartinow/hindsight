import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/hindsight-client", () => ({
  dataplaneBankUrl: (bank: string, path: string) => `http://dataplane/banks/${bank}${path}`,
  getDataplaneHeaders: () => ({}),
}));

import { DELETE, PATCH } from "@/app/api/banks/[bankId]/directives/[directiveId]/route";
import { ControlPlaneClient } from "@/lib/api";

describe("directive edit and delete preconditions", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("carries the reviewed revision from the browser through each proxy and reports a conflict", async () => {
    const bank = "demo";
    const id = "directive";
    const params = Promise.resolve({ bankId: bank, directiveId: id });
    const edits: Array<{ url: string; body?: string }> = [];
    let errorMessage = "";
    const dataplane = vi.fn().mockImplementation(async (url, init) => {
      edits.push({ url: String(url), body: init.body });
      return Response.json({ detail: "Stale revision" }, { status: 409 });
    });

    // Feed each browser request into the matching route; its downstream fetch supplies the conflict.
    vi.stubGlobal("fetch", vi.fn(async (url, init) => {
      if (String(url).startsWith("http://dataplane/")) return dataplane(url, init);
      const req = new Request(new URL(String(url), "http://ui"), init);
      const res = init.method === "PATCH" ? await PATCH(req, { params }) : await DELETE(req, { params });
      expect(res.status).toBe(409);
      errorMessage = (await res.json()).error;
      return Response.json({}); // The client request has traversed both layers.
    }));
    const client = new ControlPlaneClient();
    await client.updateDirective(bank, id, { content: "edit", expected_revision: 5 });
    await client.deleteDirective(bank, id, 5);
    expect(JSON.parse(edits[0].body!)).toEqual({ content: "edit", expected_revision: 5 });
    expect(edits[1].url).toBe("http://dataplane/banks/demo/directives/directive?expected_revision=5");
    expect(errorMessage).toContain("Reload");
  });

  it("accepts deletion from an unversioned backend without inventing a revision", async () => {
    const fetch = vi.fn().mockResolvedValue(Response.json({}));
    vi.stubGlobal("fetch", fetch);
    await new ControlPlaneClient().deleteDirective("demo", "id");
    expect(String(fetch.mock.calls[0][0])).not.toContain("expected_revision");
  });
});
