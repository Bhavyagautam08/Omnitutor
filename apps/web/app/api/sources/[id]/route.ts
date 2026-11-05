import { authorizeSource, deleteSource } from "@sc/core";
import { endpoints } from "@sc/schemas";
import { handle, json, sessionOf, sessionTokenOf } from "../../_lib/http";

export const DELETE = handle(async (req, ctx: RouteContext<"/api/sources/[id]">) => {
  const { id } = await ctx.params;
  await authorizeSource(id, sessionOf(req));
  return json(endpoints.deleteSource.response, await deleteSource(id, sessionTokenOf(req) ?? undefined));
});
