import { safeStorage } from "electron";
import { EncryptedFileSecretStore, type SecretCodec } from "../core/settings-store.js";

const electronCodec: SecretCodec = {
  available: () => safeStorage.isEncryptionAvailable(),
  encrypt: (value) => safeStorage.encryptString(value),
  decrypt: (value) => safeStorage.decryptString(Buffer.from(value)),
};

export class ElectronSecretStore extends EncryptedFileSecretStore {
  constructor(path: string, onWarning?: (message: string, error: unknown) => void) {
    super(path, electronCodec, onWarning);
  }
}
