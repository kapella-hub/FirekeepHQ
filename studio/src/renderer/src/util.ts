export function pathLeaf(value: string): string { return value.replace(/[\\/]+$/, "").split(/[\\/]/).at(-1) || value; }
export function ignore(promise: Promise<unknown>): void { void promise.catch(() => undefined); }
