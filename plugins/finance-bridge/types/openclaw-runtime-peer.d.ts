// Runtime-only import supplied by the exact OpenClaw peer host. All plugin API
// types compile against the independently named official openclaw-sdk alias so
// the development package cannot shadow this peer at runtime.
declare module "openclaw/plugin-sdk/media-runtime" {
  export function getMediaDir(): string;
}
