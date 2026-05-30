/**
 * Per-installer storage inventory + wipe UI.
 *
 * Backed by the installer router's ``GET /storage`` (report) and
 * ``POST /wipe`` (delete) endpoints. Items are grouped by wipeability
 * tier — EPHEMERAL and REPLACEABLE wipe with one click; IRRECOVERABLE
 * requires a confirm-phrase modal because the server enforces it.
 *
 * Design notes:
 * - We deliberately re-fetch storage after every wipe even though the
 *   wipe endpoint already invalidated the status cache: the storage
 *   report isn't on that cache and the on-disk numbers can shift in
 *   ways the server can't predict (free pages, OS journal flush, etc.).
 * - The confirm-phrase modal is fully client-side gated AND
 *   server-enforced. If the user bypasses the modal we still get a
 *   400 with the expected phrase echoed back — we use that response
 *   to populate the modal if the user somehow triggered the request
 *   without seeing the phrase (e.g. older client).
 */

import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, Database, FileText, FolderOpen, HardDrive, Loader2, Trash2 } from 'lucide-react';

import {
  formatBytes,
  getInstallerStorage,
  wipeInstaller,
  type StorageItem,
  type StorageReport,
} from '../../lib/installers-api';

// Tier labels — keep them human-readable. "Ephemeral" sounds technical
// but we picked it for the dataclass so the UI uses the same word.
const TIER_LABELS: Record<string, { title: string; help: string; tone: 'neutral' | 'warn' | 'danger' }> = {
  ephemeral: {
    title: 'Ephemeral',
    help: 'Caches, temp files. Safe to delete; auto-rebuilds on next run.',
    tone: 'neutral',
  },
  replaceable: {
    title: 'Replaceable',
    help: 'Settings + small state. Can be regenerated from defaults; you may lose tweaks.',
    tone: 'warn',
  },
  irrecoverable: {
    title: 'Irrecoverable',
    help: 'Indexed data and user content. Type the confirm phrase to delete.',
    tone: 'danger',
  },
};

const KIND_ICONS: Record<string, typeof Database> = {
  config: FileText,
  volume: HardDrive,
  cache: Database,
  model: FolderOpen,
};

export function InstallerStoragePanel({
  installerId,
  onAfterWipe,
}: {
  installerId: string;
  onAfterWipe?: () => void;
}) {
  const [report, setReport] = useState<StorageReport | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [wiping, setWiping] = useState<string | null>(null); // item_id of in-flight wipe, or 'BATCH'
  const [confirmModal, setConfirmModal] = useState<{
    items: StorageItem[];
    expected: string;
  } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const next = await getInstallerStorage(installerId);
      setReport(next);
    } catch (err: any) {
      setError(err.message || 'Failed to load storage');
    } finally {
      setLoading(false);
    }
  }, [installerId]);

  useEffect(() => { load(); }, [load]);

  /**
   * Execute a wipe of the given items. For IRRECOVERABLE items, the
   * server returns 400 with ``confirm_phrase_required`` until the
   * caller supplies the phrase. We surface that via the modal — the
   * modal then re-invokes this with ``phrase`` filled in.
   */
  const performWipe = async (items: StorageItem[], phrase: string) => {
    if (items.length === 0) return;
    const key = items.length === 1 ? items[0].item_id : 'BATCH';
    setWiping(key);
    try {
      await wipeInstaller(installerId, {
        item_ids: items.map((i) => i.item_id),
        confirm_phrase: phrase,
        // restart_after stays true (default) — wiping config shouldn't
        // leave the container down. For a hard uninstall the user would
        // wipe via a different code path.
      });
      setConfirmModal(null);
      await load();
      onAfterWipe?.();
    } catch (err: any) {
      // confirm_phrase_required → open the modal; everything else is
      // surfaced via the inline error pane.
      if ((err as any).confirm_phrase_required) {
        setConfirmModal({
          items,
          expected: (err as any).expected_phrase || `wipe ${installerId}`,
        });
      } else {
        setError(err.message || 'Wipe failed');
      }
    } finally {
      setWiping(null);
    }
  };

  const handleItemWipe = (item: StorageItem) => {
    if (item.wipeability === 'irrecoverable') {
      setConfirmModal({
        items: [item],
        expected: `wipe ${installerId}`,
      });
      return;
    }
    void performWipe([item], '');
  };

  if (loading) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: 'var(--color-text-tertiary)' }}>
        <Loader2 size={14} className="animate-spin" />
        Loading storage…
      </div>
    );
  }

  if (error || !report) {
    return (
      <div style={{ fontSize: 12, color: 'var(--color-error)' }}>
        {error || 'Storage report unavailable'}
      </div>
    );
  }

  // Group by wipeability so the danger tier renders distinctly.
  const grouped: Record<string, StorageItem[]> = { ephemeral: [], replaceable: [], irrecoverable: [] };
  for (const item of report.items) {
    (grouped[item.wipeability] ?? grouped.ephemeral).push(item);
  }

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', marginBottom: 10 }}>
        <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--color-text)' }}>
          Storage — total {formatBytes(report.total_bytes)}
        </div>
        <button
          type="button"
          onClick={() => void load()}
          style={{
            fontSize: 10,
            padding: '2px 8px',
            background: 'transparent',
            border: '1px solid var(--color-border)',
            color: 'var(--color-text-secondary)',
            borderRadius: 4,
            cursor: 'pointer',
          }}
        >
          Refresh
        </button>
      </div>

      {(['ephemeral', 'replaceable', 'irrecoverable'] as const).map((tier) => {
        const items = grouped[tier];
        if (!items || items.length === 0) return null;
        const tierInfo = TIER_LABELS[tier];
        return (
          <div key={tier} style={{ marginBottom: 12 }}>
            <div
              style={{
                fontSize: 10,
                textTransform: 'uppercase',
                letterSpacing: '0.08em',
                marginBottom: 4,
                color:
                  tierInfo.tone === 'danger'
                    ? 'var(--color-error)'
                    : tierInfo.tone === 'warn'
                      ? 'var(--color-warning)'
                      : 'var(--color-text-tertiary)',
              }}
            >
              {tierInfo.title} · {items.length} item{items.length === 1 ? '' : 's'}
            </div>
            <div style={{ fontSize: 10.5, color: 'var(--color-text-tertiary)', marginBottom: 6 }}>
              {tierInfo.help}
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              {items.map((item) => (
                <StorageItemRow
                  key={item.item_id}
                  item={item}
                  busy={wiping === item.item_id || wiping === 'BATCH'}
                  onWipe={() => handleItemWipe(item)}
                />
              ))}
            </div>
          </div>
        );
      })}

      {confirmModal && (
        <ConfirmPhraseModal
          items={confirmModal.items}
          expectedPhrase={confirmModal.expected}
          totalBytes={confirmModal.items.reduce((acc, it) => acc + it.size_bytes, 0)}
          busy={wiping !== null}
          onCancel={() => setConfirmModal(null)}
          onConfirm={(phrase) => void performWipe(confirmModal.items, phrase)}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Storage item row
// ---------------------------------------------------------------------------

function StorageItemRow({
  item,
  busy,
  onWipe,
}: {
  item: StorageItem;
  busy: boolean;
  onWipe: () => void;
}) {
  const Icon = KIND_ICONS[item.kind] ?? Database;
  const isDanger = item.wipeability === 'irrecoverable';
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        padding: '6px 8px',
        background: 'var(--color-bg)',
        border: '1px solid var(--color-border)',
        borderRadius: 4,
      }}
    >
      <Icon size={13} style={{ color: 'var(--color-text-tertiary)', flexShrink: 0 }} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 12, color: 'var(--color-text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {item.description}
        </div>
        {item.path && (
          <div
            style={{ fontSize: 10, fontFamily: 'monospace', color: 'var(--color-text-tertiary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
            title={item.path}
          >
            {item.path}
          </div>
        )}
      </div>
      <div style={{ fontSize: 11, color: 'var(--color-text-secondary)', whiteSpace: 'nowrap' }}>
        {formatBytes(item.size_bytes)}
      </div>
      <button
        type="button"
        disabled={busy}
        onClick={onWipe}
        style={{
          padding: '4px 8px',
          background: isDanger ? 'color-mix(in srgb, var(--color-error) 12%, var(--color-bg))' : 'var(--color-bg)',
          border: `1px solid ${isDanger ? 'var(--color-error)' : 'var(--color-border)'}`,
          color: isDanger ? 'var(--color-error)' : 'var(--color-text-secondary)',
          borderRadius: 4,
          fontSize: 11,
          cursor: busy ? 'not-allowed' : 'pointer',
          opacity: busy ? 0.5 : 1,
          display: 'flex',
          alignItems: 'center',
          gap: 4,
        }}
        title={isDanger ? 'Type the confirm phrase to delete' : 'Delete this item'}
      >
        <Trash2 size={11} />
        {busy ? '…' : 'Wipe'}
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Confirm-phrase modal (IRRECOVERABLE wipes)
// ---------------------------------------------------------------------------

function ConfirmPhraseModal({
  items,
  expectedPhrase,
  totalBytes,
  busy,
  onCancel,
  onConfirm,
}: {
  items: StorageItem[];
  expectedPhrase: string;
  totalBytes: number;
  busy: boolean;
  onCancel: () => void;
  onConfirm: (phrase: string) => void;
}) {
  const [typed, setTyped] = useState('');
  const matches = typed.trim().toLowerCase() === expectedPhrase.toLowerCase();

  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.55)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 200,
      }}
      onClick={onCancel}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 440,
          maxWidth: '92vw',
          background: 'var(--color-surface)',
          border: '1px solid var(--color-error)',
          borderRadius: 8,
          padding: 18,
          color: 'var(--color-text)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
          <AlertTriangle size={18} style={{ color: 'var(--color-error)' }} />
          <div style={{ fontWeight: 600, fontSize: 14 }}>Confirm irrecoverable wipe</div>
        </div>
        <div style={{ fontSize: 12, color: 'var(--color-text-secondary)', marginBottom: 10 }}>
          You're about to delete data that cannot be recovered:
        </div>
        <ul style={{ margin: '0 0 10px 16px', padding: 0, fontSize: 12, lineHeight: 1.6 }}>
          {items.map((it) => (
            <li key={it.item_id}>
              <strong>{it.description}</strong>{' '}
              <span style={{ color: 'var(--color-text-tertiary)' }}>
                ({formatBytes(it.size_bytes)})
              </span>
            </li>
          ))}
        </ul>
        <div style={{ fontSize: 11, color: 'var(--color-text-tertiary)', marginBottom: 10 }}>
          Total: {formatBytes(totalBytes)}. To proceed, type the phrase below exactly:
        </div>
        <div
          style={{
            padding: '6px 8px',
            background: 'var(--color-bg)',
            border: '1px solid var(--color-border)',
            borderRadius: 4,
            fontFamily: 'monospace',
            fontSize: 12,
            marginBottom: 8,
            color: 'var(--color-error)',
            userSelect: 'all',
          }}
        >
          {expectedPhrase}
        </div>
        <input
          type="text"
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          placeholder="Type the phrase"
          autoFocus
          style={{
            width: '100%',
            padding: '7px 10px',
            background: 'var(--color-bg)',
            border: `1px solid ${matches ? 'var(--color-error)' : 'var(--color-border)'}`,
            borderRadius: 4,
            color: 'var(--color-text)',
            fontSize: 12,
            fontFamily: 'monospace',
            marginBottom: 12,
            boxSizing: 'border-box',
          }}
        />
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            style={{
              padding: '6px 14px',
              background: 'var(--color-bg)',
              color: 'var(--color-text-secondary)',
              border: '1px solid var(--color-border)',
              borderRadius: 5,
              fontSize: 12,
              cursor: busy ? 'not-allowed' : 'pointer',
            }}
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={busy || !matches}
            onClick={() => onConfirm(typed.trim())}
            style={{
              padding: '6px 14px',
              background: matches ? 'var(--color-error)' : 'var(--color-bg)',
              color: matches ? 'var(--color-on-accent)' : 'var(--color-text-tertiary)',
              border: matches ? 'none' : '1px solid var(--color-border)',
              borderRadius: 5,
              fontSize: 12,
              fontWeight: 600,
              cursor: busy || !matches ? 'not-allowed' : 'pointer',
              opacity: busy ? 0.6 : 1,
            }}
          >
            {busy ? 'Wiping…' : 'Wipe'}
          </button>
        </div>
      </div>
    </div>
  );
}
