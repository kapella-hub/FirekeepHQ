import { createHash, generateKeyPairSync, sign } from "node:crypto";

export function createSignedFixture(data: Buffer, trustedComment: string): { readonly publicKey: string; readonly signature: string } {
  const { privateKey, publicKey } = generateKeyPairSync("ed25519");
  const rawPublicKey = publicKey.export({ format: "der", type: "spki" }).subarray(-32);
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
