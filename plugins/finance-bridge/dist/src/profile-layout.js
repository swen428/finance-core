import { closeSync, fstatSync, lstatSync, realpathSync } from "node:fs";
import { basename, dirname, isAbsolute, join, resolve } from "node:path";
import { openDirectory, rejectAclGrants } from "./posix.js";
export function resolveProfileLayout(locator) {
    if (typeof locator !== "object" || locator === null ||
        typeof locator.profileId !== "string" ||
        !/^[a-z0-9][a-z0-9_-]{0,63}$/u.test(locator.profileId)) {
        throw new Error("Invalid trusted profile locator.");
    }
    const hasMac = locator.applicationSupportRoot !== undefined;
    const hasLinux = locator.linuxDataRoot !== undefined;
    if (hasMac === hasLinux)
        throw new Error("Exactly one trusted profile root is required.");
    const root = hasLinux ? locator.linuxDataRoot : locator.applicationSupportRoot;
    if (typeof root !== "string" || !isAbsolute(root) || resolve(root) !== root ||
        realpathSync(root) !== root || lstatSync(root).isSymbolicLink() ||
        basename(root) !== (hasLinux ? "finance-codex" : "Application Support")) {
        throw new Error("Trusted profile root must be explicit, canonical, and use its fixed layout.");
    }
    const components = hasLinux ? ["profiles", locator.profileId]
        : ["Finance-Codex", "profiles", locator.profileId];
    return Object.freeze({ root, profileRoot: join(root, ...components),
        components: Object.freeze(components), linux: hasLinux });
}
export function profileLocatorEnvironment(locator) {
    const layout = resolveProfileLayout(locator);
    return {
        [layout.linux ? "FINANCE_CUT_LINUX_DATA_ROOT" : "FINANCE_CUT_APPLICATION_SUPPORT"]: layout.root,
        FINANCE_RUNTIME_ROOT: join(layout.profileRoot, "runtime"),
    };
}
/** Check existing trusted ancestors without creating or admitting any profile. */
export function checkProfileAncestors(locator) {
    const layout = resolveProfileLayout(locator);
    if (layout.linux && process.platform !== "linux") {
        throw new Error("Linux profile admission requires Linux filesystem verification.");
    }
    if (typeof process.getuid !== "function")
        throw new Error("Profile requires POSIX owner identity.");
    const uid = BigInt(process.getuid());
    for (let path = layout.root;; path = dirname(path)) {
        const named = lstatSync(path, { bigint: true });
        if (!named.isDirectory() || named.isSymbolicLink() ||
            (named.uid !== 0n && named.uid !== uid) ||
            (((named.mode & 18n) !== 0n) && !(named.uid === 0n && (named.mode & 512n) !== 0n)) ||
            (layout.linux && path === layout.root &&
                (named.uid !== uid || (named.mode & 4095n) !== 448n))) {
            throw new Error("Unsafe trusted profile ancestor or Linux data root.");
        }
        const fd = openDirectory(path);
        try {
            const opened = fstatSync(fd, { bigint: true });
            if (opened.dev !== named.dev || opened.ino !== named.ino) {
                throw new Error("Trusted profile ancestor changed.");
            }
            rejectAclGrants(fd);
            try {
                lstatSync(join(path, ".git"));
                throw new Error("Repository path cannot be a profile.");
            }
            catch (error) {
                if (!(error instanceof Error && "code" in error && error.code === "ENOENT"))
                    throw error;
            }
        }
        finally {
            closeSync(fd);
        }
        if (dirname(path) === path)
            return layout;
    }
}
