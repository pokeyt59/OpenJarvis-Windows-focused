/**
 * Global Docker resources panel — lists every image managed by an
 * OpenJarvis installer and shows whether Docker itself is available.
 *
 * Different from the per-installer Storage panel: images are a global
 * resource that may be shared (e.g. SearXNG's image is reused if we
 * ever add a second SearXNG-backed installer). Living outside any one
 * installer keeps the "where did all my disk go?" question answerable
 * with one fetch.
 *
 * No write actions yet — we just surface what's there. A future
 * "Prune unused" button would call ``docker image prune`` via a new
 * backend route; intentionally deferred until we have a real second
 * installer to test the shared-image case.
 */

import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, Box, CheckCircle2, Loader2, RefreshCw } from 'lucide-react';

import {
  formatBytes,
  getDockerResources,
  type DockerImageInfo,
  type DockerResources,
} from '../../lib/installers-api';

export function DockerResourcesCard() {
  const [data, setData] = useState<DockerResources | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      setData(await getDockerResources());
    } catch (err: any) {
      setError(err.message || 'Failed to fetch Docker resources');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  return (
    <div className="hud-panel" style={{ overflow: 'hidden' }}>
      <div
        style={{
          padding: '14px 18px',
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          borderBottom: '1px solid var(--color-border)',
        }}
      >
        <Box size={18} style={{ color: 'var(--color-accent-purple)' }} />
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 14, fontWeight: 600 }}>Docker images</div>
          <div style={{ fontSize: 11, color: 'var(--color-text-tertiary)', marginTop: 2 }}>
            Container images managed by OpenJarvis installers
          </div>
        </div>
        <button
          type="button"
          onClick={() => void load()}
          disabled={loading}
          title="Refresh"
          style={{
            padding: 6,
            background: 'transparent',
            border: '1px solid var(--color-border)',
            color: 'var(--color-text-secondary)',
            borderRadius: 4,
            cursor: loading ? 'not-allowed' : 'pointer',
          }}
        >
          <RefreshCw size={12} />
        </button>
      </div>

      <div style={{ padding: 14 }}>
        {loading && !data && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: 'var(--color-text-tertiary)' }}>
            <Loader2 size={14} className="animate-spin" />
            Querying Docker…
          </div>
        )}

        {error && (
          <div style={{ fontSize: 12, color: 'var(--color-error)' }}>{error}</div>
        )}

        {data && !data.available && (
          <div
            style={{
              padding: 10,
              background: 'color-mix(in srgb, var(--color-warning) 10%, transparent)',
              border: '1px solid var(--color-warning)',
              borderRadius: 6,
              fontSize: 11,
              color: 'var(--color-warning)',
              display: 'flex',
              alignItems: 'center',
              gap: 6,
            }}
          >
            <AlertTriangle size={13} />
            {data.note || 'Docker is not available on this machine.'}
          </div>
        )}

        {data && data.available && data.images.length === 0 && (
          <div style={{ fontSize: 12, color: 'var(--color-text-tertiary)' }}>
            No managed Docker images yet. Installing a Docker-backed tool will pull its image here.
          </div>
        )}

        {data && data.available && data.images.length > 0 && (
          <ImageList images={data.images} />
        )}
      </div>
    </div>
  );
}

function ImageList({ images }: { images: DockerImageInfo[] }) {
  const totalBytes = images.reduce((acc, im) => acc + (im.image_exists ? im.size_bytes : 0), 0);

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 8 }}>
        <div style={{ fontSize: 11, color: 'var(--color-text-secondary)' }}>
          {images.length} image{images.length === 1 ? '' : 's'} tracked
        </div>
        <div style={{ fontSize: 11, color: 'var(--color-text-secondary)' }}>
          On disk: {formatBytes(totalBytes)}
        </div>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {images.map((im) => (
          <div
            key={im.image_ref}
            style={{
              padding: 10,
              background: 'var(--color-bg)',
              border: '1px solid var(--color-border)',
              borderRadius: 4,
              display: 'flex',
              alignItems: 'center',
              gap: 12,
            }}
          >
            <div style={{ flex: 1, minWidth: 0 }}>
              <div
                style={{ fontSize: 12, fontFamily: 'monospace', color: 'var(--color-text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                title={im.image_ref}
              >
                {im.image_ref}
              </div>
              <div style={{ fontSize: 10.5, color: 'var(--color-text-tertiary)', marginTop: 2 }}>
                Used by: {im.installer_ids.join(', ') || '—'}
              </div>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 2 }}>
              <div style={{ fontSize: 11, color: 'var(--color-text-secondary)' }}>
                {im.image_exists ? formatBytes(im.size_bytes) : 'not pulled'}
              </div>
              <div
                style={{
                  fontSize: 10,
                  display: 'flex',
                  alignItems: 'center',
                  gap: 4,
                  color: im.in_use ? 'var(--color-success)' : 'var(--color-text-tertiary)',
                }}
                title={im.in_use ? 'A container is currently running this image' : 'Idle'}
              >
                <CheckCircle2 size={10} />
                {im.in_use ? 'in use' : 'idle'}
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
