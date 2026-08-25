export interface MicrophonePermissionRequest {
  readonly permission: string;
  readonly attachedToStudioWindow: boolean;
  readonly isMainFrame: boolean;
  readonly mediaTypes?: readonly string[];
}

export interface MicrophonePermissionCheck {
  readonly permission: string;
  readonly attachedToStudioWindow: boolean;
  readonly isMainFrame: boolean;
  readonly mediaType?: string;
}

/** Allow only an audio-only request from Studio's main renderer frame. */
export function allowsMicrophoneRequest(request: MicrophonePermissionRequest): boolean {
  return request.permission === "media"
    && request.attachedToStudioWindow
    && request.isMainFrame
    && request.mediaTypes?.length === 1
    && request.mediaTypes[0] === "audio";
}

/** Mirror the request policy for Chromium's separate permission preflight. */
export function allowsMicrophoneCheck(request: MicrophonePermissionCheck): boolean {
  return request.permission === "media"
    && request.attachedToStudioWindow
    && request.isMainFrame
    && request.mediaType === "audio";
}
