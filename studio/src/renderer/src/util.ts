export function pathLeaf(value: string): string { return value.replace(/[\\/]+$/, "").split(/[\\/]/).at(-1) || value; }
export function ignore(promise: Promise<unknown>): void { void promise.catch(() => undefined); }
/** The sandboxed renderer has no `process`; the user agent is the platform signal (Electron keeps "Macintosh" in it). */
export function isMacPlatform(userAgent: string = typeof navigator === "undefined" ? "" : navigator.userAgent): boolean { return /Mac/i.test(userAgent); }
