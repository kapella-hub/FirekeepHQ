import { createHash, generateKeyPairSync, sign } from "node:crypto";
import { describe, expect, it } from "vitest";
import { verifyMinisign } from "../src/main/release-signing.js";

function signedFixture(data: Buffer, trustedComment = "timestamp:1787770800 version:0.4.0"): {
  readonly publicKey: string;
  readonly signature: string;
} {
  const { privateKey, publicKey } = generateKeyPairSync("ed25519");
  const publicDer = publicKey.export({ format: "der", type: "spki" });
  const rawPublicKey = publicDer.subarray(-32);
  const keyId = Buffer.from("0123456789abcdef", "hex");
  const fileSignature = sign(null, createHash("blake2b512").update(data).digest(), privateKey);
  const globalSignature = sign(null, Buffer.concat([fileSignature, Buffer.from(trustedComment)]), privateKey);
  return {
    publicKey: Buffer.concat([Buffer.from("Ed"), keyId, rawPublicKey]).toString("base64"),
    signature: [
      "untrusted comment: signature from test release",
      Buffer.concat([Buffer.from("ED"), keyId, fileSignature]).toString("base64"),
      `trusted comment: ${trustedComment}`,
      globalSignature.toString("base64"),
    ].join("\n"),
  };
}

describe("release signature verification", () => {
  it("verifies a fixture emitted by Firekeep's production Python signer", () => {
    const data = Buffer.from('{"version":"0.4.0"}\n');
    const publicKey = "RWQ4qttY/f5Pxhn6e3ZeILhHnxPCoNuBtbGV9QmJ5ZVsLBgDfZ0MaPvc";
    const signature = [
      "untrusted comment: signature from firekeep release signing",
      "RUQ4qttY/f5PxhAXnbe2T2dORqPUy8s/j4QPvk5em473F3Onk/SZNPytH4t5W5GnwKI+jvX3FHww7pGUgZqayvj6/Pxs4ZUmsAI=",
      "trusted comment: timestamp:1787770800 version:0.4.0",
      "DBcpVfZwQVVMJBGPxmVJdKLdJnQiR40NWzGPk+hW7nI7mCrkck9Pr52basOupUx0nNpvNoN8p6jJ+nBKpZAQDg==",
    ].join("\n");

    expect(verifyMinisign(data, signature, publicKey)).toBe("timestamp:1787770800 version:0.4.0");
  });

  it("verifies minisign's prehashed signature and trusted-comment binding", () => {
    const data = Buffer.from('{"version":"0.4.0"}\n');
    const fixture = signedFixture(data);

    expect(verifyMinisign(data, fixture.signature, fixture.publicKey)).toBe("timestamp:1787770800 version:0.4.0");
  });

  it("rejects changed manifest bytes and changed trusted comments", () => {
    const data = Buffer.from('{"version":"0.4.0"}\n');
    const fixture = signedFixture(data);

    expect(() => verifyMinisign(Buffer.from('{"version":"9.9.9"}\n'), fixture.signature, fixture.publicKey)).toThrow(/does not verify/i);
    expect(() => verifyMinisign(data, fixture.signature.replace("version:0.4.0", "version:9.9.9"), fixture.publicKey)).toThrow(/trusted comment/i);
  });
});
