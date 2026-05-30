/**
 * Client for the /v1/tools/* routes — user-mutable tool settings.
 *
 * Currently only the web_search backend selector. The Tools tab in
 * DataSourcesPage reads the effective config and PUTs an update when
 * the user picks a different backend.
 */

import { getBase } from './api';

/**
 * Effective web_search config + provenance.
 *
 * ``backend_source`` answers the UI's question "if I changed this
 * setting, would it actually take effect?" — when the source is
 * ``"env"`` the value comes from an environment variable that
 * supersedes anything the user can change in the UI, so we render the
 * card as read-only with an explainer.
 */
export interface WebSearchConfig {
  backend: string;
  backend_source: 'env' | 'sidecar' | 'config' | 'default';
  searxng_url: string;
  searxng_url_source: 'env' | 'sidecar' | 'config' | 'default';
  /** Raw sidecar JSON, or null if no override file exists. */
  sidecar: Record<string, unknown> | null;
  /** Recognised backend names — handy so the UI doesn't hard-code the list. */
  available_backends: string[];
}

export async function getWebSearchConfig(): Promise<WebSearchConfig> {
  const res = await fetch(`${getBase()}/v1/tools/web_search/config`);
  if (!res.ok) throw new Error(`Failed to fetch web_search config: ${res.status}`);
  return res.json();
}

export interface WebSearchConfigUpdate {
  /** Pass an empty string to clear the sidecar override for this field. */
  backend?: string;
  searxng_url?: string;
}

export async function putWebSearchConfig(
  update: WebSearchConfigUpdate,
): Promise<WebSearchConfig> {
  const res = await fetch(`${getBase()}/v1/tools/web_search/config`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(update),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(typeof body.detail === 'string' ? body.detail : `Update failed: ${res.status}`);
  }
  return res.json();
}

export async function deleteWebSearchConfig(): Promise<WebSearchConfig> {
  const res = await fetch(`${getBase()}/v1/tools/web_search/config`, {
    method: 'DELETE',
  });
  if (!res.ok) throw new Error(`Failed to clear web_search config: ${res.status}`);
  return res.json();
}
