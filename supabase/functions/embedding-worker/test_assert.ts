// Tiny stdlib-only assertion shim so the worker-core tests run fully OFFLINE under
// `deno test --no-config` with ZERO remote imports (jsr:@std/assert is not vendored
// in this repo and Deno is not assumed installed on the acceptance host). Test-only.

export function assertEquals<T>(actual: T, expected: T, msg?: string): void {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) {
    throw new Error(msg ?? `assertEquals failed:\n  actual:   ${a}\n  expected: ${e}`);
  }
}

export function assertTrue(v: unknown, msg?: string): void {
  if (!v) throw new Error(msg ?? `expected truthy, got: ${JSON.stringify(v)}`);
}

export function assertRejects(
  fn: () => Promise<unknown>,
  expected?: unknown,
): Promise<void> {
  return Promise.resolve()
    .then(() => fn())
    .then(() => {
      throw new Error(
        typeof expected === "string"
          ? expected
          : "assertRejects: expected rejection, got resolution",
      );
    }, (err: unknown) => {
      if (typeof expected === "function" && !(err instanceof (expected as Function))) {
        throw new Error("assertRejects: rejection type did not match");
      }
    });
}
