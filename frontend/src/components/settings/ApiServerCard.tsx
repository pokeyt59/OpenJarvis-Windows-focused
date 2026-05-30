/**
 * API server status card — shows whether the Python backend is up, what
 * port it's bound to, and gives Start/Stop/Restart/Hard-restart controls.
 *
 * Why this exists: before this panel, the only way to bounce the API was
 * to quit and re-open the whole Tauri app (1–2 min wait on the next boot
 * because `uv sync` would run again). Now "Restart" can do a 3-second
 * re-spawn of just the Python child if a previous boot succeeded, and
 * "Hard restart" is reserved for cases where you actually need the full
 * Ollama + model + uv-sync prelude (e.g. after `git pull` changed deps).
 *
 * Polls get_jarvis_status every 2s. The poll is cheap — the Rust side
 * gates its /health probe with an 800ms timeout — but only runs while
 * the Settings page is mounted.
 */

import { useCallback, useEffect, useState } from 'react';
import { Loader2, Play, RefreshCw, RotateCcw, Square } from 'lucide-react';

import {
  getJarvisStatus,
  hardRestartBackend,
  restartJarvis,
  startBackend,
  stopJarvis,
  type JarvisStatus,
} from '../../lib/api';

type Action = 'start' | 'stop' | 'restart' | 'hardRestart';

export function ApiServerCard() {
  const [status, setStatus] = useState<JarvisStatus | null>(null);
  const [acting, setActing] = useState<Action | null>(null);
  const [actionError, setActionError] = useState('');

  const poll = useCallback(async () => {
    const s = await getJarvisStatus();
    if (s) setStatus(s);
  }, []);

  useEffect(() => {
    void poll();
    const id = setInterval(() => void poll(), 2000);
    return () => clearInterval(id);
  }, [poll]);

  const run = async (kind: Action, fn: () => Promise<void>) => {
    setActing(kind);
    setActionError('');
    try {
      await fn();
      // Re-poll quickly so the pill flips without waiting for the next tick.
      setTimeout(() => void poll(), 300);
    } catch (e: any) {
      setActionError(e?.message || String(e));
    } finally {
      setActing(null);
    }
  };

  const isRunning = !!status?.running;
  const isHealthy = !!status?.healthy;
  const canFastRestart = !!status?.can_fast_restart;
  const phase = status?.phase || '';

  // Pill state derivation — order matters: "Starting" wins over "Stopped"
  // when we hold a child handle but /health hasn't responded yet.
  let pillLabel = 'Loading…';
  let pillColor = 'var(--color-text-tertiary)';
  if (status) {
    if (isRunning && isHealthy) {
      pillLabel = 'Running';
      pillColor = 'var(--color-success)';
    } else if (isRunning && phase !== 'ready') {
      pillLabel = 'Starting';
      pillColor = 'var(--color-warning)';
    } else if (isRunning && !isHealthy) {
      pillLabel = 'Unhealthy';
      pillColor = 'var(--color-warning)';
    } else if (status.error) {
      pillLabel = 'Failed';
      pillColor = 'var(--color-error)';
    } else {
      pillLabel = 'Stopped';
      pillColor = 'var(--color-text-tertiary)';
    }
  }

  // "Start" prefers fast restart when we have last_boot data — same effect
  // as Restart but presented as Start because there's no live process.
  const startFn = canFastRestart ? restartJarvis : startBackend;

  return (
    <div
      className="rounded-xl p-5"
      style={{
        background: 'var(--color-surface)',
        border: '1px solid var(--color-border)',
      }}
    >
      <div className="flex items-center justify-between mb-4">
        <div>
          <h3 className="text-sm font-semibold" style={{ color: 'var(--color-text)' }}>
            API server
          </h3>
          <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-tertiary)' }}>
            The Python backend that the UI talks to. Bounce it without restarting the app.
          </p>
        </div>
        <Pill label={pillLabel} color={pillColor} />
      </div>

      <div
        className="flex items-center justify-between py-2.5"
        style={{ borderTop: '1px solid var(--color-border-subtle)' }}
      >
        <div className="text-sm" style={{ color: 'var(--color-text)' }}>
          Address
        </div>
        <div
          className="text-xs"
          style={{
            color: 'var(--color-text-tertiary)',
            fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
          }}
        >
          http://127.0.0.1:{status?.port ?? 8000}
        </div>
      </div>

      <div
        className="flex items-start justify-between py-2.5"
        style={{ borderTop: '1px solid var(--color-border-subtle)' }}
      >
        <div className="text-sm" style={{ color: 'var(--color-text)' }}>
          Last status
        </div>
        <div
          className="text-xs text-right"
          style={{ color: 'var(--color-text-tertiary)', maxWidth: 280 }}
        >
          {status?.detail || status?.phase || '—'}
        </div>
      </div>

      {(actionError || status?.error) && (
        <div
          className="text-xs px-3 py-2 rounded mt-3"
          style={{
            background: 'color-mix(in srgb, var(--color-error) 8%, transparent)',
            color: 'var(--color-error)',
            border: '1px solid color-mix(in srgb, var(--color-error) 25%, transparent)',
            wordBreak: 'break-word',
            overflowWrap: 'anywhere',
          }}
        >
          {actionError || status?.error}
        </div>
      )}

      <div className="flex flex-wrap gap-2 mt-4">
        {isRunning ? (
          <Btn
            variant="danger"
            disabled={!!acting}
            onClick={() => run('stop', stopJarvis)}
            icon={
              acting === 'stop' ? (
                <Loader2 size={12} className="animate-spin" />
              ) : (
                <Square size={12} />
              )
            }
            label={acting === 'stop' ? 'Stopping…' : 'Stop'}
          />
        ) : (
          <Btn
            variant="primary"
            disabled={!!acting}
            onClick={() => run('start', startFn)}
            icon={
              acting === 'start' ? (
                <Loader2 size={12} className="animate-spin" />
              ) : (
                <Play size={12} />
              )
            }
            label={acting === 'start' ? 'Starting…' : 'Start'}
          />
        )}
        <Btn
          variant="secondary"
          disabled={!!acting || !canFastRestart || !isRunning}
          onClick={() => run('restart', restartJarvis)}
          icon={
            acting === 'restart' ? (
              <Loader2 size={12} className="animate-spin" />
            ) : (
              <RotateCcw size={12} />
            )
          }
          label={acting === 'restart' ? 'Restarting…' : 'Restart'}
          title={
            !canFastRestart
              ? 'Available after the first successful boot of this session'
              : !isRunning
                ? 'Server is stopped — use Start'
                : 'Quick re-spawn of the Python server (skips uv sync)'
          }
        />
        <Btn
          variant="secondary"
          disabled={!!acting}
          onClick={() => run('hardRestart', hardRestartBackend)}
          icon={
            acting === 'hardRestart' ? (
              <Loader2 size={12} className="animate-spin" />
            ) : (
              <RefreshCw size={12} />
            )
          }
          label={acting === 'hardRestart' ? 'Restarting…' : 'Hard restart'}
          title="Full setup sequence — uv sync, model check, server spawn (1–2 min)"
        />
      </div>

      <p className="text-[10.5px] mt-3" style={{ color: 'var(--color-text-tertiary)' }}>
        Restart re-spawns the Python server only (~3s). Hard restart re-runs
        the full boot sequence — needed after a <code style={{ fontSize: '0.95em' }}>git pull</code> that
        changed dependencies.
      </p>
    </div>
  );
}

function Pill({ label, color }: { label: string; color: string }) {
  return (
    <span
      className="flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-full shrink-0"
      style={{
        background: `color-mix(in srgb, ${color} 12%, transparent)`,
        color,
        border: `1px solid color-mix(in srgb, ${color} 25%, transparent)`,
      }}
    >
      <span
        style={{
          width: 6,
          height: 6,
          borderRadius: '50%',
          background: color,
          display: 'inline-block',
        }}
      />
      {label}
    </span>
  );
}

function Btn({
  variant,
  disabled,
  onClick,
  icon,
  label,
  title,
}: {
  variant: 'primary' | 'danger' | 'secondary';
  disabled: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
  title?: string;
}) {
  const variantStyle: React.CSSProperties =
    variant === 'primary'
      ? {
          background: 'var(--color-accent)',
          color: 'white',
          border: '1px solid var(--color-accent)',
        }
      : variant === 'danger'
        ? {
            background: 'transparent',
            color: 'var(--color-error)',
            border: '1px solid color-mix(in srgb, var(--color-error) 40%, transparent)',
          }
        : {
            background: 'var(--color-bg)',
            color: 'var(--color-text-secondary)',
            border: '1px solid var(--color-border)',
          };
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      title={title}
      className="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-colors"
      style={{
        ...variantStyle,
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.5 : 1,
      }}
    >
      {icon}
      {label}
    </button>
  );
}
