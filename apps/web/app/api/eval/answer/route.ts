import { answerForEval, courseFor } from "@sc/core";
import { endpoints } from "@sc/schemas";
import { handle, json, readBody, sessionOf, sessionTokenOf } from "../../_lib/http";

/** Tutor answer without a learner (no history, no writes), with retrieved contexts for RAGAS. */
export const POST = handle(async (req) => {
  const body = await readBody(req, endpoints.evalAnswer.request);
  return json(
    endpoints.evalAnswer.response,
    await answerForEval(body.question, await courseFor(sessionOf(req)), sessionTokenOf(req) ?? undefined),
  );
});
