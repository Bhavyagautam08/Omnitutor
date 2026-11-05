// Retrieval: embed the question (RETRIEVAL_QUERY), score every embedded chunk of the course's
// READY sources by cosine similarity (in JS; no vector index by design), return the top k.
import { prisma } from "@sc/db";
import type { Citation, SourceType } from "@sc/schemas";
import { requireCourse } from "./course";
import { AppError, notReady } from "./errors";
import { searchWithPython } from "./pythonApi";

export type RetrievedChunk = {
  chunkId: string;
  sourceId: string;
  sourceType: SourceType;
  filename: string;
  kind: "TEXT" | "FIGURE" | "SLIDE" | "TRANSCRIPT";
  title: string | null;
  content: string;
  page: number | null;
  slide: number | null;
  tStart: number | null;
  tEnd: number | null;
  topicId: string | null;
  score: number;
};

export function cosine(a: number[], b: number[]): number {
  let dot = 0;
  let na = 0;
  let nb = 0;
  for (let i = 0; i < a.length; i++) {
    dot += a[i] * b[i];
    na += a[i] * a[i];
    nb += b[i] * b[i];
  }
  return na && nb ? dot / Math.sqrt(na * nb) : 0;
}

export async function retrieve(
  query: string,
  opts: { courseId?: string; k?: number; sessionToken?: string } = {},
): Promise<{ results: RetrievedChunk[]; topScore: number; candidates: number }> {
  const courseId = await requireCourse(opts.courseId);
  const readyChunks = await prisma.chunk.count({
    where: { source: { courseId, status: "READY" }, NOT: { embedding: { isEmpty: true } } },
  });
  if (readyChunks === 0) throw notReady("Nothing to search yet: upload a source and run ingest first.");
  if (!opts.sessionToken) throw new AppError("unauthorized", "Sign in before searching notebook content.");

  const response = await searchWithPython({
    notebookId: courseId,
    query,
    topK: opts.k ?? 6,
    sessionToken: opts.sessionToken,
  });
  const results: RetrievedChunk[] = response.results.map((r) => ({
    chunkId: r.chunk_id,
    sourceId: r.source_id,
    sourceType: r.source_type,
    filename: r.filename,
    kind: r.kind,
    title: r.title,
    content: r.content,
    page: r.citation.page,
    slide: r.citation.slide,
    tStart: r.citation.timestamp_start,
    tEnd: r.citation.timestamp_end,
    topicId: r.topic_id,
    score: r.score,
  }));
  return {
    candidates: response.candidates,
    topScore: results[0]?.score ?? 0,
    results,
  };
}

/** Max excerpt length shown with a citation (decision Q10). */
export const MAX_EXCERPT_CHARS = 600;

export function excerptOf(content: string, max = MAX_EXCERPT_CHARS): string {
  const text = content.replace(/\s+/g, " ").trim();
  if (text.length <= max) return text;
  const cut = text.slice(0, max);
  const lastSpace = cut.lastIndexOf(" ");
  return `${cut.slice(0, lastSpace > max * 0.6 ? lastSpace : max).trimEnd()}…`;
}

/** Chunk → contract Citation: exactly the locator its source type requires. */
export function toCitation(r: Omit<RetrievedChunk, "score" | "content" | "topicId"> & { content: string }): Citation {
  const base = { chunkId: r.chunkId, sourceId: r.sourceId, filename: r.filename, kind: r.kind, title: r.title, excerpt: excerptOf(r.content) };
  switch (r.sourceType) {
    case "PDF":
      return { ...base, sourceType: "PDF", page: r.page! };
    case "PPTX":
      return { ...base, sourceType: "PPTX", slide: r.slide! };
    case "VIDEO":
      return { ...base, sourceType: "VIDEO", tStart: r.tStart!, tEnd: r.tEnd! };
  }
}
