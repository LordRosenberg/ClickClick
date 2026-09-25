import assert from "node:assert/strict";
import test from "node:test";
import { getTaskPage } from "../src/api/client.ts";

test("task pages request only the selected range and optional status", async () => {
  const original = globalThis.fetch;
  const requests: string[] = [];
  globalThis.fetch = (async (url) => {
    requests.push(String(url));
    return new Response(JSON.stringify({ items: [], total: 42, page: 2, page_size: 20 }));
  }) as typeof fetch;
  try {
    const page = await getTaskPage(2, 20, "failed");
    assert.equal(page.total, 42);
    assert.deepEqual(requests, ["/api/tasks/page?page=2&page_size=20&status=failed"]);
    await getTaskPage(1, 10);
    assert.equal(requests[1], "/api/tasks/page?page=1&page_size=10");
  } finally {
    globalThis.fetch = original;
  }
});
