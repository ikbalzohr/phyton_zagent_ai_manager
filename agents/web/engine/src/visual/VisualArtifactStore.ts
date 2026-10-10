import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { DATA_ROOT } from "../config/paths.js";

export type VisualArtifactSource = "web" | "arcmap" | "video" | "file";

export interface VisualArtifactManifest {
  schemaVersion: 1;
  id: string;
  source: VisualArtifactSource;
  kind: string;
  label: string | null;
  capturedAt: string;
  artifactPath: string;
  originalPath: string;
  mimeType: string;
  bytes: number;
  sha256: string;
  provenance: {
    deviceId: string | null;
    deviceName: string | null;
    sessionId: string | null;
    requestId: string | null;
    traceId: string | null;
  };
  metadata: Record<string, unknown>;
}

function rootFromEnvironment(): string {
  const configured = process.env.ZAGENT_VISUAL_ROOT?.trim();
  if (configured) return path.resolve(configured);
  return path.resolve(DATA_ROOT, "..", "visual");
}

function sanitize(value: string): string {
  return value.replace(/[^a-zA-Z0-9._-]+/g, "_").replace(/^_+|_+$/g, "");
}

function mimeFor(extension: string): string {
  switch (extension.toLowerCase()) {
    case ".png": return "image/png";
    case ".jpg":
    case ".jpeg": return "image/jpeg";
    case ".webp": return "image/webp";
    case ".mp4": return "video/mp4";
    case ".webm": return "video/webm";
    case ".gif": return "image/gif";
    default: return "application/octet-stream";
  }
}

async function atomicJson(file: string, value: unknown): Promise<void> {
  await fs.mkdir(path.dirname(file), { recursive: true });
  const tmp = file + "." + process.pid + ".tmp";
  await fs.writeFile(tmp, JSON.stringify(value, null, 2), "utf8");
  await fs.rename(tmp, file);
}

async function sha256(file: string): Promise<string> {
  const hash = crypto.createHash("sha256");
  hash.update(await fs.readFile(file));
  return hash.digest("hex");
}

export class VisualArtifactStore {
  readonly root: string;
  readonly artifactsDir: string;
  readonly manifestsDir: string;
  readonly stagingDir: string;
  readonly latestFile: string;

  constructor(root = rootFromEnvironment()) {
    this.root = path.resolve(root);
    this.artifactsDir = path.join(this.root, "artifacts");
    this.manifestsDir = path.join(this.root, "manifests");
    this.stagingDir = path.join(this.root, "staging");
    this.latestFile = path.join(this.root, "latest.json");
  }

  async ensure(): Promise<void> {
    await Promise.all([
      fs.mkdir(this.artifactsDir, { recursive: true }),
      fs.mkdir(this.manifestsDir, { recursive: true }),
      fs.mkdir(this.stagingDir, { recursive: true }),
    ]);
  }

  stagingPath(name: string, extension = ".png"): string {
    const safe = sanitize(name) || "visual";
    const ext = extension.startsWith(".") ? extension : "." + extension;
    return path.join(this.stagingDir, safe + "_" + Date.now() + ext);
  }

  async publish(input: {
    path: string;
    source: VisualArtifactSource;
    kind: string;
    label?: string | null;
    sessionId?: string | null;
    requestId?: string | null;
    traceId?: string | null;
    metadata?: Record<string, unknown>;
  }): Promise<VisualArtifactManifest> {
    await this.ensure();
    const originalPath = path.resolve(input.path);
    const stat = await fs.stat(originalPath);
    if (!stat.isFile()) throw new Error("Visual artifact source is not a file.");

    const extension = path.extname(originalPath).toLowerCase() || ".bin";
    const id = "vis_" + Date.now() + "_" + crypto.randomUUID().replace(/-/g, "").slice(0, 12);
    const finalPath = path.join(this.artifactsDir, id + extension);
    await fs.copyFile(originalPath, finalPath);

    const manifest: VisualArtifactManifest = {
      schemaVersion: 1,
      id,
      source: input.source,
      kind: input.kind,
      label: input.label?.trim() || null,
      capturedAt: new Date().toISOString(),
      artifactPath: finalPath,
      originalPath,
      mimeType: mimeFor(extension),
      bytes: (await fs.stat(finalPath)).size,
      sha256: await sha256(finalPath),
      provenance: {
        deviceId: process.env.ZAGENT_DEVICE_ID?.trim() || null,
        deviceName: process.env.ZAGENT_DEVICE_NAME?.trim() || null,
        sessionId: input.sessionId?.trim() || process.env.ZAGENT_SESSION_ID?.trim() || null,
        requestId: input.requestId?.trim() || process.env.ZAGENT_REQUEST_ID?.trim() || null,
        traceId: input.traceId?.trim() || process.env.ZAGENT_TRACE_ID?.trim() || null,
      },
      metadata: input.metadata ?? {},
    };

    await atomicJson(path.join(this.manifestsDir, id + ".json"), manifest);
    await atomicJson(this.latestFile, manifest);
    return manifest;
  }

  async latest(source?: VisualArtifactSource): Promise<VisualArtifactManifest | null> {
    await this.ensure();
    if (!source) {
      try {
        return JSON.parse(await fs.readFile(this.latestFile, "utf8")) as VisualArtifactManifest;
      } catch {
        return null;
      }
    }
    const items = await this.list({ source, limit: 1 });
    return items[0] ?? null;
  }

  async list(options: {
    source?: VisualArtifactSource;
    limit?: number;
  } = {}): Promise<VisualArtifactManifest[]> {
    await this.ensure();
    const names = (await fs.readdir(this.manifestsDir))
      .filter((name) => name.toLowerCase().endsWith(".json"));
    const manifests: VisualArtifactManifest[] = [];
    for (const name of names) {
      try {
        const item = JSON.parse(
          await fs.readFile(path.join(this.manifestsDir, name), "utf8")
        ) as VisualArtifactManifest;
        if (!options.source || item.source === options.source) manifests.push(item);
      } catch {
        // Ignore incomplete/corrupt manifests; writer uses atomic rename.
      }
    }
    manifests.sort((a, b) => b.capturedAt.localeCompare(a.capturedAt));
    return manifests.slice(0, Math.max(1, Math.min(options.limit ?? 50, 500)));
  }

  async prune(options: {
    maxArtifacts?: number;
    maxAgeHours?: number;
  } = {}): Promise<{ removed: number; retained: number }> {
    const maxArtifacts = Math.max(1, Math.min(options.maxArtifacts ?? 200, 5000));
    const maxAgeMs = options.maxAgeHours && options.maxAgeHours > 0
      ? options.maxAgeHours * 60 * 60 * 1000
      : null;
    const all = await this.list({ limit: 5000 });
    const now = Date.now();
    const remove = all.filter((item, index) =>
      index >= maxArtifacts ||
      (maxAgeMs !== null && now - Date.parse(item.capturedAt) > maxAgeMs)
    );

    for (const item of remove) {
      await fs.unlink(item.artifactPath).catch(() => undefined);
      await fs.unlink(path.join(this.manifestsDir, item.id + ".json")).catch(() => undefined);
    }

    const retained = all.length - remove.length;
    const latest = (await this.list({ limit: 1 }))[0] ?? null;
    if (latest) await atomicJson(this.latestFile, latest);
    else await fs.unlink(this.latestFile).catch(() => undefined);
    return { removed: remove.length, retained };
  }
}

export const visualArtifactStore = new VisualArtifactStore();
