declare module "fs-ext" {
  export function flock(
    fileDescriptor: number,
    operation: "ex" | "exnb" | "sh" | "shnb" | "un",
    callback: (error?: NodeJS.ErrnoException | null) => void,
  ): void;
}
