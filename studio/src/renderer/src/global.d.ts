import type { StudioBridge } from "../../shared/ipc";

declare global {
  interface Window {
    firekeepStudio: StudioBridge;
  }
}

export {};
