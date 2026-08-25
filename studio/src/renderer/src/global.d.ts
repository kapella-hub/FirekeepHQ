import type { StudioBridge } from "../../shared/ipc";

declare global {
  interface SpeechRecognitionResultLike { readonly length: number; readonly isFinal: boolean; readonly [index: number]: { readonly transcript: string; readonly confidence: number } }
  interface SpeechRecognitionEventLike extends Event { readonly resultIndex: number; readonly results: { readonly length: number; readonly [index: number]: SpeechRecognitionResultLike } }
  interface SpeechRecognitionErrorEventLike extends Event { readonly error: string; readonly message?: string }
  interface SpeechRecognitionLike {
    continuous: boolean;
    interimResults: boolean;
    lang: string;
    onstart: (() => void) | null;
    onend: (() => void) | null;
    onresult: ((event: SpeechRecognitionEventLike) => void) | null;
    onerror: ((event: SpeechRecognitionErrorEventLike) => void) | null;
    start(): void;
    stop(): void;
    abort(): void;
  }
  interface Window {
    SpeechRecognition?: new () => SpeechRecognitionLike;
    webkitSpeechRecognition?: new () => SpeechRecognitionLike;
  }
  interface Window {
    firekeepStudio: StudioBridge;
  }
}

export {};
