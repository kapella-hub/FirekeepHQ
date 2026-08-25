import { contextBridge, ipcRenderer } from "electron";
import {
  STUDIO_EVENT_CHANNEL,
  STUDIO_INVOKE_CHANNEL,
  type StudioAction,
  type StudioBridge,
  type StudioPushEvent,
} from "../shared/ipc.js";

const bridge: StudioBridge = {
  invoke: (action: StudioAction) => ipcRenderer.invoke(STUDIO_INVOKE_CHANNEL, action),
  subscribe: (listener) => {
    const wrapped = (_event: Electron.IpcRendererEvent, value: StudioPushEvent): void => {
      listener(value);
    };
    ipcRenderer.on(STUDIO_EVENT_CHANNEL, wrapped);
    return () => ipcRenderer.removeListener(STUDIO_EVENT_CHANNEL, wrapped);
  },
};

contextBridge.exposeInMainWorld("firekeepStudio", bridge);
