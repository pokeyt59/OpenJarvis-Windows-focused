/**
 * Client for the /v1/installers + /v1/docker routes.
 *
 * Three categories of call:
 *
 * - **Plain JSON** (list / status / storage / wipe) — vanilla fetch.
 * - **SSE stream** (/run) — uses ``streamInstallerRun`` which returns
 *   an async iterable of ``InstallerEvent`` objects. The server emits
 *   one ``data: <json>\n\n`` line per ``Progress`` event, then a final
 *   ``event: done`` (or ``event: error`` on failure). We parse SSE
 *   manually rather than using the browser's ``EventSource`` because
 *   EventSource only does GET — and POST is the correct verb here
 *   (the request mutates server state by starting an install).
 * - **Docker resources** — list-only, plain JSON.
 */

import { getBase } from './api';

// ---------------------------------------------------------------------------
// Types — match the backend response shapes
// ---------------------------------------------------------------------------

export interface InstallerStepStatus {
  name: string;
  /** "installed" | "not_installed" | "partial" | "broken" | "unknown" */
  status: string;
}

export interface InstallerStatus {
  installer_id: string;
  display_name: string;
  description: string;
  /** "ready" | "not_installed" | "partial" | "broken" | "unknown" */
  status: string;
  estimated_total_seconds: number;
  estimated_download_mb: number;
  steps: InstallerStepStatus[];
}

export interface InstallerEvent {
  step_idx: number;
  step_name: string;
  percent: number;
  message: string;
  /** "info" | "warn" | "error" — defaults to "info" */
  level?: string;
  /** Set on the final event the stream emits when run completes. */
  done?: boolean;
  /** Set when the run failed; ``message`` carries the error text. */
  error?: boolean;
  /**
   * Optional action link attached to an error — rendered as a clickable
   * button next to the message. Used by the Docker primitive: when
   * Docker Desktop is missing we attach
   * ``{label: "Install Docker Desktop", url: "https://www.docker.com/..."}``
   * so the user gets a single click to fix it.
   */
  link?: { label: string; url: string };
}

export interface StorageItem {
  item_id: string;
  /** "config" | "volume" | "cache" | "model" */
  kind: string;
  description: string;
  size_bytes: number;
  /** "ephemeral" | "replaceable" | "irrecoverable" */
  wipeability: string;
  path: string | null;
}

export interface StorageReport {
  installer_id: string;
  total_bytes: number;
  by_kind: Record<string, number>;
  items: StorageItem[];
}

export interface DockerImageInfo {
  image_ref: string;
  size_bytes: number;
  installer_ids: string[];
  in_use: boolean;
  image_exists: boolean;
}

export interface DockerResources {
  available: boolean;
  images: DockerImageInfo[];
  /** Present only when ``available`` is false; explains why. */
  note?: string;
}

// ---------------------------------------------------------------------------
// Plain JSON endpoints
// ---------------------------------------------------------------------------

export async function listInstallers(): Promise<InstallerStatus[]> {
  const res = await fetch(`${getBase()}/v1/installers`);
  if (!res.ok) throw new Error(`Failed to list installers: ${res.status}`);
  const data = await res.json();
  return data.installers || [];
}

export async function getInstallerStatus(id: string): Promise<InstallerStatus> {
  const res = await fetch(
    `${getBase()}/v1/installers/${encodeURIComponent(id)}/status`,
  );
  if (!res.ok) throw new Error(`Failed to get installer ${id}: ${res.status}`);
  return res.json();
}

export async function refreshInstallerStatus(id: string): Promise<InstallerStatus> {
  const res = await fetch(
    `${getBase()}/v1/installers/${encodeURIComponent(id)}/status/refresh`,
    { method: 'POST' },
  );
  if (!res.ok) throw new Error(`Failed to refresh installer ${id}: ${res.status}`);
  return res.json();
}

export async function getInstallerStorage(id: string): Promise<StorageReport> {
  const res = await fetch(
    `${getBase()}/v1/installers/${encodeURIComponent(id)}/storage`,
  );
  if (!res.ok) {
    throw new Error(`Failed to get storage for ${id}: ${res.status}`);
  }
  return res.json();
}

export interface WipeRequest {
  item_ids: string[];
  confirm_phrase?: string;
  force?: boolean;
  restart_after?: boolean;
}

export async function wipeInstaller(
  id: string,
  req: WipeRequest,
): Promise<{ ok: boolean; events: Array<{ message: string }> }> {
  const res = await fetch(
    `${getBase()}/v1/installers/${encodeURIComponent(id)}/wipe`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(req),
    },
  );
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    const detail = body.detail;
    // The 400 from confirm_phrase_required has a structured ``detail``
    // object — surface it so the caller can prompt for the phrase.
    if (typeof detail === 'object' && detail?.error === 'confirm_phrase_required') {
      const err = new Error(detail.message) as Error & {
        confirm_phrase_required?: true;
        expected_phrase?: string;
      };
      err.confirm_phrase_required = true;
      err.expected_phrase = detail.expected;
      throw err;
    }
    throw new Error(typeof detail === 'string' ? detail : `Wipe failed: ${res.status}`);
  }
  return res.json();
}

export async function getDockerResources(): Promise<DockerResources> {
  const res = await fetch(`${getBase()}/v1/docker/resources`);
  if (!res.ok) throw new Error(`Failed to list docker resources: ${res.status}`);
  return res.json();
}

// ---------------------------------------------------------------------------
// SSE stream — /run
// ---------------------------------------------------------------------------

/**
 * Start an installer run and yield each Progress event as it arrives.
 *
 * Why a manual SSE parser:
 * - The browser's ``EventSource`` is GET-only, but ``/run`` is POST
 *   (it mutates server state — starts an install). So we fetch with a
 *   POST + ReadableStream and parse the SSE wire format inline.
 * - The stream may emit two non-data event types: ``event: done`` for
 *   clean completion and ``event: error`` on failure. We yield a final
 *   event with ``done: true`` or ``error: true`` so the consumer
 *   doesn't have to know about SSE framing.
 *
 * Cancellation: when the consumer breaks out of the loop, the
 * underlying stream is released and the server-side generator
 * eventually unwinds.
 */
export async function* streamInstallerRun(
  id: string,
): AsyncIterableIterator<InstallerEvent> {
  const res = await fetch(
    `${getBase()}/v1/installers/${encodeURIComponent(id)}/run`,
    {
      method: 'POST',
      headers: { Accept: 'text/event-stream' },
    },
  );
  if (!res.ok || !res.body) {
    throw new Error(`Failed to start installer ${id}: ${res.status}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  // SSE messages are separated by a blank line. We buffer chunks until
  // we see one, then parse the buffered text into an event.
  let buffer = '';
  let currentEvent = 'message';

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // Split on the SSE message boundary (blank line). Last fragment
      // may be incomplete — keep it in the buffer for next iteration.
      const parts = buffer.split(/\r?\n\r?\n/);
      buffer = parts.pop() ?? '';

      for (const part of parts) {
        const lines = part.split(/\r?\n/);
        let dataLine: string | null = null;
        let eventLine: string | null = null;
        for (const line of lines) {
          if (line.startsWith('data:')) {
            dataLine = line.slice(5).trimStart();
          } else if (line.startsWith('event:')) {
            eventLine = line.slice(6).trim();
          }
        }
        if (eventLine) currentEvent = eventLine;
        if (dataLine === null) continue;

        let payload: any = {};
        try {
          payload = JSON.parse(dataLine);
        } catch {
          // Malformed JSON shouldn't kill the stream — yield a
          // best-effort event with the raw text in ``message``.
          payload = { message: dataLine };
        }

        if (currentEvent === 'error') {
          // Forward ``link`` if the backend attached one — e.g. the
          // Docker primitive sends {label: "Install Docker Desktop",
          // url: "..."} so the card can render a clickable button.
          yield {
            step_idx: -1,
            step_name: '',
            percent: 0,
            message: payload.error || 'unknown error',
            error: true,
            link: payload.link,
          };
          return;
        }
        if (currentEvent === 'done') {
          yield {
            step_idx: -1,
            step_name: '',
            percent: 100,
            message: '',
            done: true,
          };
          return;
        }
        yield {
          step_idx: payload.step_idx ?? -1,
          step_name: payload.step_name ?? '',
          percent: payload.percent ?? 0,
          message: payload.message ?? '',
          level: payload.level,
        };
        currentEvent = 'message';
      }
    }
  } finally {
    try {
      reader.releaseLock();
    } catch {
      // The reader may already be closed; nothing to do.
    }
  }
}

// ---------------------------------------------------------------------------
// Tiny helper: human-readable byte size
// ---------------------------------------------------------------------------

/**
 * Render a byte count as "1.2 GB" etc. — used by Storage panels.
 * Lives here (rather than a generic utils module) so the installer UI
 * doesn't depend on anything else.
 */
export function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return '—';
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v < 10 ? v.toFixed(1) : v.toFixed(0)} ${units[i]}`;
}
