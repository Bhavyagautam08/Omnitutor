// Source uploads: stream to disk, verify content, record in the DB. Uploads never call Gemini;
// ingestion starts only on POST /api/sources/[id]/ingest (ingest/index.ts).
import { randomUUID } from "node:crypto";
import { mkdir, rename, rm, stat } from "node:fs/promises";
import path from "node:path";
import { prisma, type Source } from "@sc/db";
import {
  ACCEPTED_EXTENSIONS, ACCEPTED_UPLOADS, MAX_UPLOAD_BYTES, MAX_UPLOAD_MB, sourceTypeFromFilename,
  type DeleteSourceResponse, type SourceView, type UploadResponse,
} from "@sc/schemas";
import { requireCourse } from "./course";
import { badRequest, notFound, notReady } from "./errors";
import { deleteFile } from "./llm";
import { inspectMedia } from "./media";
import { deleteSourceFromPython } from "./pythonApi";
import { UPLOADS_REL, resolveSourcePath, saveStream, uploadsDir } from "./storage";

const toSourceView = (s: Source & { _count: { chunks: number } }): SourceView => ({
  id: s.id,
  type: s.type,
  filename: s.filename,
  status: s.status,
  error: s.error,
  progressDone: s.progressDone,
  progressTotal: s.progressTotal,
  chunkCount: s._count.chunks,
  createdAt: s.createdAt.toISOString(),
});

/** Keeps only the base name (no directories) and strips control characters. */
function cleanFilename(raw: string): string {
  const base = raw.split(/[\\/]/).pop() ?? "";
  // eslint-disable-next-line no-control-regex
  return base.replace(/[\u0000-\u001f\u007f]/g, "").trim();
}

export async function uploadSource(input: {
  body: AsyncIterable<Uint8Array> | null;
  filename: string;
  /** Content-Length header, if sent: lets oversize uploads fail before any bytes are read. */
  contentLength?: number | null;
  courseId?: string;
}): Promise<UploadResponse> {
  const courseId = await requireCourse(input.courseId);
  const filename = cleanFilename(input.filename);
  const type = filename ? sourceTypeFromFilename(filename) : null;
  if (!type) throw badRequest(`Unsupported file "${filename}". Upload a .pdf, .pptx or .mp4 file.`);
  if (input.contentLength != null && input.contentLength > MAX_UPLOAD_BYTES) throw badRequest(`File exceeds the ${MAX_UPLOAD_MB} MB limit.`);
  if (!input.body) throw badRequest("Empty upload: send the file bytes as the request body.");

  const id = randomUUID();
  const rel = `${UPLOADS_REL}/${id}${ACCEPTED_EXTENSIONS[type]}`;
  const abs = resolveSourcePath(rel);
  const part = `${abs}.part`;
  await mkdir(uploadsDir(), { recursive: true });
  try {
    const sizeBytes = await saveStream(input.body, part, MAX_UPLOAD_BYTES);
    if (sizeBytes === 0) throw badRequest("The file is empty.");
    const info = await inspectMedia(part, type);
    await rename(part, abs);
    try {
      await prisma.source.create({
        data: {
          id, courseId, type, filename, mimeType: ACCEPTED_UPLOADS[type][0], sizeBytes, path: rel,
          status: "PENDING", pageCount: info.pageCount, durationSec: info.durationSec,
        },
      });
    } catch (e) {
      await rm(abs, { force: true });
      throw e;
    }
    return { id, status: "PENDING" };
  } finally {
    await rm(part, { force: true });
  }
}

export async function listSources(courseId?: string): Promise<SourceView[]> {
  const id = await requireCourse(courseId);
  const rows = await prisma.source.findMany({
    where: { courseId: id },
    orderBy: [{ createdAt: "asc" }, { id: "asc" }],
    include: { _count: { select: { chunks: true } } },
  });
  return rows.map(toSourceView);
}

/** Deletes the source, its chunks (cascade), the stored file and any Gemini Files API copy. */
export async function deleteSource(id: string, sessionToken?: string): Promise<DeleteSourceResponse> {
  const source = await prisma.source.findUnique({ where: { id } });
  if (!source) throw notFound(`Source "${id}" not found.`);
  if (source.status === "PROCESSING") throw notReady("This source is being processed. Delete it once it is READY or FAILED.");
  if (sessionToken) {
    await deleteSourceFromPython({
      notebookId: source.courseId,
      sourceId: id,
      sessionToken,
    });
  }
  await prisma.source.delete({ where: { id } });
  await rm(resolveSourcePath(source.path), { force: true });
  if (source.geminiFileName) await deleteFile(source.geminiFileName).catch(() => {}); // remote copy expires in 48 h anyway
  return { id };
}

export type SourceFile = { absPath: string; mimeType: string; filename: string; size: number };

/** Stored original for /api/files/[id]; 404 if the row or the file is missing. */
export async function getSourceFile(id: string): Promise<SourceFile> {
  const source = await prisma.source.findUnique({ where: { id }, select: { path: true, mimeType: true, filename: true } });
  if (!source) throw notFound(`Source "${id}" not found.`);
  const absPath = resolveSourcePath(source.path);
  const info = await stat(absPath).catch(() => null);
  if (!info?.isFile()) throw notFound(`The stored file for source "${id}" is missing (${path.basename(absPath)}).`);
  return { absPath, mimeType: source.mimeType, filename: source.filename, size: info.size };
}

export type ByteRange = { kind: "full" } | { kind: "partial"; start: number; end: number } | { kind: "unsatisfiable" };

/**
 * Parses a single-range `Range: bytes=…` header (RFC 9110 §14). Malformed, multi-range or
 * non-byte ranges are ignored (full response), as the RFC allows. `end` is inclusive.
 */
export function parseByteRange(header: string | null, size: number): ByteRange {
  const m = header?.trim().match(/^bytes=(\d*)-(\d*)$/);
  if (!m || (m[1] === "" && m[2] === "")) return { kind: "full" };
  if (m[1] === "") {
    const suffix = Number(m[2]);
    if (suffix === 0 || size === 0) return { kind: "unsatisfiable" };
    return { kind: "partial", start: Math.max(0, size - suffix), end: size - 1 };
  }
  const start = Number(m[1]);
  if (m[2] !== "" && Number(m[2]) < start) return { kind: "full" }; // invalid → ignore the header
  if (start >= size) return { kind: "unsatisfiable" };
  const end = m[2] === "" ? size - 1 : Math.min(Number(m[2]), size - 1);
  return { kind: "partial", start, end };
}
