/**
 * Web search backend selector + SearXNG installer launcher.
 *
 * Renders one "tool card" with two main responsibilities:
 *
 * 1. Let the user pick which backend the ``web_search`` tool uses —
 *    the cloud chain (Tavily → DuckDuckGo) or the local SearXNG
 *    container. Persistence is via the ``PUT /v1/tools/web_search/config``
 *    sidecar override; that route is keyed off ``WebSearchConfig`` in
 *    ``lib/tools-api.ts``.
 *
 * 2. When SearXNG is selected, show its install lifecycle: not-installed
 *    → "Install" button → SSE-streamed step progress → ready with a
 *    Storage panel + Uninstall action. The installer SSE is consumed
 *    via the async iterable in ``lib/installers-api.ts``.
 *
 * Why one component rather than per-screen pieces: the backend chooser
 * and the installer panel are tightly linked — picking SearXNG without
 * an install path is dead-end UX, and showing the installer outside
 * of "SearXNG chosen" is noise. Composing them here keeps the state
 * machine local.
 */

import { useEffect, useState, useCallback, useRef } from 'react';
import { Search, Cloud, Server, CheckCircle2, AlertTriangle, ExternalLink, Loader2, Play, RefreshCw } from 'lucide-react';

import {
  getWebSearchConfig,
  putWebSearchConfig,
  type WebSearchConfig,
} from '../../lib/tools-api';
import {
  getInstallerStatus,
  refreshInstallerStatus,
  streamInstallerRun,
  type InstallerStatus,
  type InstallerEvent,
} from '../../lib/installers-api';
import { InstallerStoragePanel } from './InstallerStoragePanel';

const SEARXNG_INSTALLER_ID = 'web_search.searxng';

// Helper: render the source provenance for the active backend. Surfaces
// "this is locked by env var" so the user knows clicking won't help.
type BackendSource = WebSearchConfig['backend_source'];

function SourceTag({ source }: { source: BackendSource }) {
  const labels: Record<BackendSource, string> = {
    env: 'Locked by env var',
    sidecar: 'Set in UI',
    config: 'Set in config.toml',
    default: 'Default',
  };
  const colors: Record<BackendSource, string> = {
    env: 'var(--color-warning)',
    sidecar: 'var(--color-success)',
    config: 'var(--color-text-secondary)',
    default: 'var(--color-text-tertiary)',
  };
  return (
    <span
      style={{
        fontSize: 10,
        padding: '1px 6px',
        borderRadius: 4,
        border: `1px solid ${colors[source]}`,
        color: colors[source],
        marginLeft: 8,
      }}
    >
      {labels[source]}
    </span>
  );
}

// Compact pill for the per-step status during install streaming.
function StepPill({ event }: { event: InstallerEvent }) {
  const isError = event.level === 'error' || event.error;
  const isWarn = event.level === 'warn';
  const color = isError
    ? 'var(--color-error)'
    : isWarn
      ? 'var(--color-warning)'
      : 'var(--color-text-secondary)';
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 11, color }}>
      <span style={{ minWidth: 40, color: 'var(--color-text-tertiary)' }}>
        {event.percent != null ? `${event.percent}%` : ''}
      </span>
      <span style={{ flex: 1, fontFamily: 'monospace' }}>{event.message || event.step_name}</span>
    </div>
  );
}

export function WebSearchToolCard() {
  const [cfg, setCfg] = useState<WebSearchConfig | null>(null);
  const [status, setStatus] = useState<InstallerStatus | null>(null);
  const [statusError, setStatusError] = useState('');
  const [cfgError, setCfgError] = useState('');

  // Install run state — a separate stream cancels the previous one.
  // ``installFailed`` is shaped so the backend can attach a help link
  // (e.g. Docker missing → "Install Docker Desktop" button).
  const [installing, setInstalling] = useState(false);
  const [installEvents, setInstallEvents] = useState<InstallerEvent[]>([]);
  const [installFailed, setInstallFailed] = useState<{
    message: string;
    link?: { label: string; url: string };
  } | null>(null);
  const cancelInstallRef = useRef<{ cancelled: boolean }>({ cancelled: false });

  // Loading config / saving backend choice
  const [busy, setBusy] = useState(false);

  const loadConfig = useCallback(async () => {
    try {
      const next = await getWebSearchConfig();
      setCfg(next);
      setCfgError('');
    } catch (err: any) {
      setCfgError(err.message || 'Failed to load config');
    }
  }, []);

  const loadStatus = useCallback(async (forceRefresh = false) => {
    try {
      const next = forceRefresh
        ? await refreshInstallerStatus(SEARXNG_INSTALLER_ID)
        : await getInstallerStatus(SEARXNG_INSTALLER_ID);
      setStatus(next);
      setStatusError('');
    } catch (err: any) {
      setStatusError(err.message || 'Failed to load installer status');
    }
  }, []);

  // Initial load + refresh on a 30s heartbeat. We only poll status when
  // SearXNG is the user's chosen backend — no point hammering the
  // status cache for a backend they're not using.
  useEffect(() => {
    loadConfig();
  }, [loadConfig]);

  const usingSearxng = !!(cfg && cfg.backend.split(',').includes('searxng'));

  useEffect(() => {
    if (!usingSearxng) return;
    loadStatus();
    const handle = setInterval(() => loadStatus(), 30000);
    return () => clearInterval(handle);
  }, [usingSearxng, loadStatus]);

  // ---------------------------------------------------------------
  // Backend choice — radio cards
  // ---------------------------------------------------------------

  const pickBackend = async (next: string) => {
    if (busy || !cfg) return;
    setBusy(true);
    setCfgError('');
    try {
      // Empty string clears the sidecar override → config.toml wins
      // again. We use it for "Default" so the user can reset.
      const updated = await putWebSearchConfig({ backend: next });
      setCfg(updated);
    } catch (err: any) {
      setCfgError(err.message || 'Failed to save');
    } finally {
      setBusy(false);
    }
  };

  // ---------------------------------------------------------------
  // SearXNG install — consumes the SSE stream
  // ---------------------------------------------------------------

  const startInstall = async () => {
    if (installing) return;
    setInstalling(true);
    setInstallEvents([]);
    setInstallFailed(null);
    cancelInstallRef.current = { cancelled: false };

    try {
      for await (const evt of streamInstallerRun(SEARXNG_INSTALLER_ID)) {
        if (cancelInstallRef.current.cancelled) break;
        setInstallEvents((prev) => [...prev, evt]);
        if (evt.error) {
          // Carry the optional link through so the panel can render
          // the "Install Docker Desktop" button.
          setInstallFailed({ message: evt.message, link: evt.link });
          break;
        }
        if (evt.done) {
          break;
        }
      }
    } catch (err: any) {
      setInstallFailed({ message: err.message || 'Install failed' });
    } finally {
      setInstalling(false);
      // Bust the status cache so the freshly-installed state shows up
      // immediately rather than after the next 30s tick.
      loadStatus(true);
    }
  };

  const cancelInstall = () => {
    cancelInstallRef.current = { cancelled: true };
    setInstalling(false);
  };

  // ---------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------

  if (!cfg) {
    return (
      <div className="hud-panel" style={{ padding: 16 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: 'var(--color-text-tertiary)' }}>
          <Loader2 size={14} className="animate-spin" />
          Loading web search config…
        </div>
        {cfgError && (
          <div style={{ marginTop: 8, fontSize: 12, color: 'var(--color-error)' }}>{cfgError}</div>
        )}
      </div>
    );
  }

  const envLocked = cfg.backend_source === 'env';
  const activeBackend = cfg.backend;
  // For the radio choice we treat anything containing "searxng" as
  // "Local SearXNG" and everything else as "Cloud chain". This keeps
  // the UI simple even when an advanced user types a custom chain.
  const choice: 'cloud' | 'searxng' = activeBackend.includes('searxng') ? 'searxng' : 'cloud';

  return (
    <div className="hud-panel" style={{ overflow: 'hidden' }}>
      {/* Card header */}
      <div
        style={{
          padding: '14px 18px',
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          borderBottom: '1px solid var(--color-border)',
        }}
      >
        <Search size={18} style={{ color: 'var(--color-accent-purple)' }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 14, fontWeight: 600, color: 'var(--color-text)' }}>
            Web Search
            <SourceTag source={cfg.backend_source} />
          </div>
          <div style={{ fontSize: 11, color: 'var(--color-text-tertiary)', marginTop: 2 }}>
            Active chain: <code style={{ color: 'var(--color-text-secondary)' }}>{activeBackend}</code>
          </div>
        </div>
      </div>

      {/* Env-lock warning */}
      {envLocked && (
        <div
          style={{
            padding: '8px 14px',
            background: 'color-mix(in srgb, var(--color-warning) 12%, transparent)',
            borderBottom: '1px solid var(--color-border)',
            fontSize: 11,
            color: 'var(--color-warning)',
            display: 'flex',
            alignItems: 'center',
            gap: 6,
          }}
        >
          <AlertTriangle size={13} />
          OPENJARVIS_WEB_SEARCH_BACKEND env var is set — UI changes won't take effect until it's unset.
        </div>
      )}

      {/* Backend choice */}
      <div style={{ padding: 14, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
        <BackendChoice
          icon={Cloud}
          title="Cloud chain"
          subtitle="Tavily → DuckDuckGo"
          description="Fast, no setup. Tavily key gets used when present; falls back to DuckDuckGo otherwise."
          active={choice === 'cloud'}
          disabled={busy || envLocked}
          onClick={() => pickBackend('auto')}
        />
        <BackendChoice
          icon={Server}
          title="Local SearXNG"
          subtitle="Docker container"
          description="Self-hosted meta-search. Private, no rate limits. Requires Docker; one-click install below."
          active={choice === 'searxng'}
          disabled={busy || envLocked}
          onClick={() => pickBackend('searxng')}
        />
      </div>

      {/* Config error */}
      {cfgError && (
        <div style={{ padding: '0 14px 10px', fontSize: 11, color: 'var(--color-error)' }}>
          {cfgError}
        </div>
      )}

      {/* SearXNG install panel — only when SearXNG is the chosen backend */}
      {choice === 'searxng' && (
        <div style={{ borderTop: '1px solid var(--color-border)' }}>
          <SearxngInstallPanel
            status={status}
            statusError={statusError}
            installing={installing}
            installEvents={installEvents}
            installFailed={installFailed}
            onStart={startInstall}
            onCancel={cancelInstall}
            onRefresh={() => loadStatus(true)}
          />
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------
// Backend radio "card"
// ---------------------------------------------------------------

function BackendChoice({
  icon: Icon,
  title,
  subtitle,
  description,
  active,
  disabled,
  onClick,
}: {
  icon: typeof Cloud;
  title: string;
  subtitle: string;
  description: string;
  active: boolean;
  disabled: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      style={{
        textAlign: 'left',
        padding: 12,
        background: active
          ? 'color-mix(in srgb, var(--color-accent-purple) 12%, var(--color-bg))'
          : 'var(--color-bg)',
        border: `1px solid ${active ? 'var(--color-accent-purple)' : 'var(--color-border)'}`,
        borderRadius: 6,
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.6 : 1,
        color: 'var(--color-text)',
        display: 'flex',
        gap: 10,
        alignItems: 'flex-start',
      }}
    >
      <Icon size={16} style={{ color: active ? 'var(--color-accent-purple)' : 'var(--color-text-secondary)', marginTop: 2, flexShrink: 0 }} />
      <div style={{ minWidth: 0, flex: 1 }}>
        <div style={{ fontWeight: 600, fontSize: 13 }}>
          {title}{' '}
          {active && (
            <CheckCircle2 size={12} style={{ verticalAlign: 'middle', color: 'var(--color-accent-purple)' }} />
          )}
        </div>
        <div style={{ fontSize: 11, color: 'var(--color-text-secondary)', marginTop: 1 }}>
          {subtitle}
        </div>
        <div style={{ fontSize: 11, color: 'var(--color-text-tertiary)', marginTop: 6, lineHeight: 1.45 }}>
          {description}
        </div>
      </div>
    </button>
  );
}

// ---------------------------------------------------------------
// SearXNG install lifecycle panel
// ---------------------------------------------------------------

function SearxngInstallPanel({
  status,
  statusError,
  installing,
  installEvents,
  installFailed,
  onStart,
  onCancel,
  onRefresh,
}: {
  status: InstallerStatus | null;
  statusError: string;
  installing: boolean;
  installEvents: InstallerEvent[];
  installFailed: { message: string; link?: { label: string; url: string } } | null;
  onStart: () => void;
  onCancel: () => void;
  onRefresh: () => void;
}) {
  // Build a step-keyed map so the live display shows the latest event
  // for each step, rather than a giant scrolling tail.
  const latestByStep = installEvents.reduce<Record<number, InstallerEvent>>((acc, evt) => {
    if (evt.error || evt.done) return acc;
    acc[evt.step_idx] = evt;
    return acc;
  }, {});

  const overall = status?.status ?? 'unknown';
  const isReady = overall === 'ready';
  const isPartial = overall === 'partial';
  const isBroken = overall === 'broken';
  const isMissing = overall === 'not_installed';

  return (
    <div style={{ padding: 14 }}>
      {/* Status header */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          marginBottom: 10,
        }}
      >
        <StatusIndicator state={overall} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--color-text)' }}>
            {status?.display_name ?? 'SearXNG'}
          </div>
          <div style={{ fontSize: 11, color: 'var(--color-text-tertiary)' }}>
            {statusError
              ? statusError
              : isReady
                ? 'Running — web_search will hit the local instance'
                : isPartial
                  ? 'Partially installed — some steps need attention'
                  : isBroken
                    ? 'Broken — last install failed or container is down'
                    : isMissing
                      ? 'Not installed — click Install to set up the Docker container'
                      : 'Checking…'}
          </div>
        </div>
        <button
          type="button"
          onClick={onRefresh}
          disabled={installing}
          title="Re-check status"
          style={{
            padding: 6,
            background: 'transparent',
            border: '1px solid var(--color-border)',
            color: 'var(--color-text-secondary)',
            borderRadius: 4,
            cursor: installing ? 'not-allowed' : 'pointer',
          }}
        >
          <RefreshCw size={12} />
        </button>
      </div>

      {/* Per-step indicator (from /status) */}
      {status?.steps && status.steps.length > 0 && !installing && (
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(140px, 1fr))',
            gap: 6,
            marginBottom: 12,
          }}
        >
          {status.steps.map((s) => (
            <div
              key={s.name}
              style={{
                fontSize: 10,
                padding: '4px 8px',
                background: 'var(--color-bg)',
                border: '1px solid var(--color-border)',
                borderRadius: 4,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: 6,
              }}
            >
              <span style={{ color: 'var(--color-text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {s.name}
              </span>
              <StepStatusDot state={s.status} />
            </div>
          ))}
        </div>
      )}

      {/* Live install progress */}
      {installing && (
        <div
          style={{
            padding: 10,
            background: 'var(--color-bg)',
            border: '1px solid var(--color-border)',
            borderRadius: 6,
            marginBottom: 10,
            display: 'flex',
            flexDirection: 'column',
            gap: 6,
          }}
        >
          <div style={{ fontSize: 11, color: 'var(--color-accent-purple)', fontWeight: 600, display: 'flex', alignItems: 'center', gap: 6 }}>
            <Loader2 size={12} className="animate-spin" />
            Installing SearXNG…
          </div>
          {Object.values(latestByStep).map((evt) => (
            <StepPill key={evt.step_idx} event={evt} />
          ))}
        </div>
      )}

      {installFailed && (
        <div
          style={{
            padding: 10,
            background: 'color-mix(in srgb, var(--color-error) 10%, transparent)',
            border: '1px solid var(--color-error)',
            borderRadius: 6,
            marginBottom: 10,
            fontSize: 11,
            color: 'var(--color-error)',
            display: 'flex',
            flexDirection: 'column',
            gap: 8,
          }}
        >
          <div>Install failed: {installFailed.message}</div>
          {installFailed.link && (
            // Styled like the connector setup-step buttons
            // (see OneDrive's "Open Azure App Registrations →") so
            // the recovery action lives next to the error message
            // instead of as a copy-pasted URL inside the message.
            <a
              href={installFailed.link.url}
              target="_blank"
              rel="noopener noreferrer"
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 6,
                alignSelf: 'flex-start',
                padding: '6px 10px',
                fontSize: 11,
                fontWeight: 500,
                color: 'var(--color-error)',
                background: 'var(--color-bg)',
                border: '1px solid var(--color-error)',
                borderRadius: 5,
                textDecoration: 'none',
              }}
            >
              {installFailed.link.label}
              <ExternalLink size={11} />
            </a>
          )}
        </div>
      )}

      {/* Action row */}
      <div style={{ display: 'flex', gap: 8 }}>
        {installing ? (
          <button
            type="button"
            onClick={onCancel}
            style={{
              padding: '6px 14px',
              background: 'var(--color-bg)',
              border: '1px solid var(--color-border)',
              borderRadius: 5,
              color: 'var(--color-text-secondary)',
              fontSize: 12,
              cursor: 'pointer',
            }}
          >
            Cancel
          </button>
        ) : (
          !isReady && (
            <button
              type="button"
              onClick={onStart}
              style={{
                padding: '6px 14px',
                background: 'var(--color-accent-purple)',
                color: 'var(--color-on-accent)',
                border: 'none',
                borderRadius: 5,
                fontSize: 12,
                fontWeight: 600,
                cursor: 'pointer',
                display: 'flex',
                alignItems: 'center',
                gap: 6,
              }}
            >
              <Play size={12} />
              {isMissing ? 'Install SearXNG' : 'Repair install'}
            </button>
          )
        )}
        {status && status.estimated_download_mb > 0 && !installing && !isReady && (
          <div style={{ alignSelf: 'center', fontSize: 10.5, color: 'var(--color-text-tertiary)' }}>
            ~{status.estimated_download_mb} MB download · ~{Math.round((status.estimated_total_seconds || 0) / 60)} min
          </div>
        )}
      </div>

      {/* Storage panel — shown only when installed */}
      {isReady && (
        <div style={{ marginTop: 16, paddingTop: 12, borderTop: '1px dashed var(--color-border)' }}>
          <InstallerStoragePanel
            installerId={SEARXNG_INSTALLER_ID}
            onAfterWipe={() => onRefresh()}
          />
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------
// Tiny status indicators
// ---------------------------------------------------------------

function StatusIndicator({ state }: { state: string }) {
  const colors: Record<string, string> = {
    ready: 'var(--color-success)',
    partial: 'var(--color-warning)',
    broken: 'var(--color-error)',
    not_installed: 'var(--color-text-tertiary)',
    unknown: 'var(--color-text-tertiary)',
  };
  return (
    <span
      style={{
        width: 8,
        height: 8,
        borderRadius: 999,
        background: colors[state] ?? 'var(--color-text-tertiary)',
        flexShrink: 0,
      }}
    />
  );
}

function StepStatusDot({ state }: { state: string }) {
  return <StatusIndicator state={state} />;
}

// Re-export so other tool cards can wrap the storage panel themselves
// without importing it directly.
export { InstallerStoragePanel };
