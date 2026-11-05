import { after } from "next/server";
import { AppError, authorizeSource, requestIngestion } from "@sc/core";
import { endpoints } from "@sc/schemas";
import { handle, json, sessionOf, sessionTokenOf } from "../../../_lib/http";

/** Starts ingestion (spends Gemini quota; cached pages are free) and returns at once. Poll GET /api/sources. */
export const POST = handle(async (req, ctx: RouteContext<"/api/sources/[id]/ingest">) => {
  const { id } = await ctx.params;
  await authorizeSource(id, sessionOf(req));
  const sessionToken = sessionTokenOf(req);
  if (!sessionToken) throw new AppError("unauthorized", "Sign in before ingesting a source.");
  const { response, job } = await requestIngestion(id, {}, sessionToken);
  after(job); // keeps the job alive after the response (self-hosted Next supports this)
  return json(endpoints.ingestSource.response, response);
});
