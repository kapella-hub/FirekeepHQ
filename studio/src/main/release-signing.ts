import { createHash, createPublicKey, timingSafeEqual, verify } from "node:crypto";

export const PINNED_FIREKEEP_RELEASE_KEY = "RWRhSg0k0YNtfVG2DYqWZCyZaY9XRylvhxNdX3k0dseC0xoSSxnvrdh/";

const PUBLIC_KEY_PREFIX = Buffer.from("302a300506032b6570032100", "hex");

function decodeBase64(value: string, label: string): Buffer {
  const normalized = value.trim();
  if (!/^[A-Za-z0-9+/]+={0,2}$/.test(normalized) || normalized.length % 4 === 1) {
    throw new Error(`${label} is not valid base64`);
  }
  const decoded = Buffer.from(normalized, "base64");
  if (decoded.toString("base64").replace(/=+$/, "") !== normalized.replace(/=+$/, "")) {
    throw new Error(`${label} is not valid base64`);
  }
  return decoded;
}

function publicKeyBlob(publicKeyText: string): Buffer {
  const line = publicKeyText.split(/\r?\n/).map((item) => item.trim()).find((item) => item && !item.toLowerCase().startsWith("untrusted comment:"));
  if (!line) throw new Error("release public key is empty");
  const blob = decodeBase64(line, "release public key");
  if (blob.length !== 42 || blob.subarray(0, 2).toString("ascii") !== "Ed") {
    throw new Error("release public key is not a minisign Ed25519 key");
  }
  return blob;
}

export function verifyMinisign(data: Uint8Array, signatureText: string, publicKeyText = PINNED_FIREKEEP_RELEASE_KEY): string {
  const publicBlob = publicKeyBlob(publicKeyText);
  const lines = signatureText.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
  if (lines.length !== 4 || !lines[0]?.toLowerCase().startsWith("untrusted comment:") || !lines[2]?.toLowerCase().startsWith("trusted comment:")) {
    throw new Error("release signature is not a four-line minisign signature");
  }
  const signatureBlob = decodeBase64(lines[1] ?? "", "release signature");
  const globalSignature = decodeBase64(lines[3] ?? "", "release global signature");
  if (signatureBlob.length !== 74 || globalSignature.length !== 64) throw new Error("release signature has an invalid length");
  const algorithm = signatureBlob.subarray(0, 2).toString("ascii");
  if (algorithm !== "ED" && algorithm !== "Ed") throw new Error(`release signature uses unsupported algorithm ${algorithm}`);
  if (!timingSafeEqual(signatureBlob.subarray(2, 10), publicBlob.subarray(2, 10))) throw new Error("release signature key does not match Studio's pinned key");

  const publicKey = createPublicKey({
    key: Buffer.concat([PUBLIC_KEY_PREFIX, publicBlob.subarray(10)]),
    format: "der",
    type: "spki",
  });
  const fileSignature = signatureBlob.subarray(10);
  const payload = algorithm === "ED" ? createHash("blake2b512").update(data).digest() : Buffer.from(data);
  if (!verify(null, payload, publicKey, fileSignature)) throw new Error("release signature does not verify against Studio's pinned key");

  const trustedComment = lines[2].slice("trusted comment:".length).trimStart();
  if (!verify(null, Buffer.concat([fileSignature, Buffer.from(trustedComment)]), publicKey, globalSignature)) {
    throw new Error("release signature's trusted comment does not verify");
  }
  return trustedComment;
}
