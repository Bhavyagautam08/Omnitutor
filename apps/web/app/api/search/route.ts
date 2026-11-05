import { courseFor, retrieve, toCitation } from "@sc/core";
import { endpoints } from "@sc/schemas";
import { handle, json, readBody, sessionOf, sessionTokenOf } from "../_lib/http";

/** Retrieval over all READY sources: top-k chunks as citations with cosine scores. */
export const POST = handle(async (req) => {
  const body = await readBody(req, endpoints.search.request);
  const learnerId = sessionOf(req);
  const { results, topScore, candidates } = await retrieve(body.query, {
    courseId: await courseFor(learnerId, body.courseId),
    k: body.k,
    sessionToken: sessionTokenOf(req) ?? undefined,
  });
  return json(endpoints.search.response, {
    topScore,
    candidates,
    results: results.map((r) => ({ score: r.score, citation: toCitation(r) })),
  });
});
