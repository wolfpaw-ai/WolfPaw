// Workspace browser. v1 lists files + provides per-row download links.
// Upload UI is deferred — backend supports it (POST /workspace/upload-url
// → PUT bytes → POST /workspace/files) but the React form lands in a
// follow-up.

import { useEffect, useState } from "react";
import { ApiError, workspace } from "../api/client";
import type { WorkspaceFile } from "../api/types";
import { formatDateTime } from "../lib/format";

export function FilesPage() {
  const [items, setItems] = useState<WorkspaceFile[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    workspace.list().then(setItems).catch((e) =>
      setError(e instanceof ApiError ? String(e.detail) : String(e)),
    );
  }, []);

  async function onDownload(id: string) {
    try {
      const { url } = await workspace.downloadUrl(id);
      // Open the pre-signed URL in a new tab — the backend serves the
      // bytes directly (LocalStorage) or 302's to S3 in hosted.
      window.open(url, "_blank", "noopener");
    } catch (e) {
      alert(
        e instanceof ApiError
          ? `Couldn't get download URL: ${e.detail}`
          : `Couldn't get download URL: ${e}`,
      );
    }
  }

  if (error) return <main className="page"><p className="error">{error}</p></main>;
  if (items === null) return <main className="page"><p>Loading…</p></main>;

  return (
    <main className="page">
      <h1>Workspace</h1>
      {items.length === 0 && (
        <p>
          No files yet. Agents write deliverables here when they produce
          spreadsheets, PDFs, etc. — upload UI ships in a follow-up.
        </p>
      )}
      <table className="data">
        <thead>
          <tr>
            <th>Name</th>
            <th>Source</th>
            <th>Size</th>
            <th>v</th>
            <th>Created</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {items.map((f) => (
            <tr key={f.id}>
              <td>{f.filename}</td>
              <td>{f.source}</td>
              <td>{f.size_bytes.toLocaleString()} B</td>
              <td>{f.version}</td>
              <td>{formatDateTime(f.created_at)}</td>
              <td>
                <button onClick={() => onDownload(f.id)}>Download</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </main>
  );
}
