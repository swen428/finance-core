import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

export async function temporaryDirectory(): Promise<AsyncDisposable & { path: string }> {
  const path = await mkdtemp(join(tmpdir(), "finance-bridge-test-"));
  return {
    path,
    async [Symbol.asyncDispose]() {
      await rm(path, { recursive: true, force: true });
    },
  };
}
