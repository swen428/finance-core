/** Trusted local composition only. A locator never grants database authority. */
export interface ProfileRootLocator {
    readonly applicationSupportRoot?: string;
    readonly linuxDataRoot?: string;
    readonly profileId: string;
}
export interface ProfileLayout {
    readonly root: string;
    readonly profileRoot: string;
    readonly components: readonly string[];
    readonly linux: boolean;
}
export declare function resolveProfileLayout(locator: ProfileRootLocator): ProfileLayout;
export declare function profileLocatorEnvironment(locator: ProfileRootLocator): Record<string, string>;
/** Check existing trusted ancestors without creating or admitting any profile. */
export declare function checkProfileAncestors(locator: ProfileRootLocator): ProfileLayout;
